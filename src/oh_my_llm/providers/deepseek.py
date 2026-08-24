from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
import json
import os
from typing import Any, Literal, cast

import httpx

from .._errors import ModelsError
from .._models import Model, Provider, _create_model, _create_provider
from .._values import (
    AssistantMessage,
    AssistantMessageDoneEvent,
    AssistantMessageEvent,
    AssistantMessageStartEvent,
    AssistantMessageTextDeltaEvent,
    AssistantMessageTextEndEvent,
    AssistantMessageTextStartEvent,
    Context,
    TextContent,
    Usage,
    UsageCost,
    UserMessage,
    _AssistantMessageEventValidator,
)


__all__ = ("deepseekProvider",)


_DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
_DEEPSEEK_MODEL_ID = "deepseek-v4-flash"
_ZERO_COST = UsageCost(
    input=0.0,
    output=0.0,
    cacheRead=0.0,
    cacheWrite=0.0,
    total=0.0,
)
_ZERO_USAGE = Usage(
    input=0,
    output=0,
    cacheRead=0,
    cacheWrite=0,
    totalTokens=0,
    cost=_ZERO_COST,
)
_FINISH_REASONS: dict[str, Literal["stop", "length", "toolUse"]] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "toolUse",
}
_DEEPSEEK_MODEL = _create_model(
    id=_DEEPSEEK_MODEL_ID,
    name="DeepSeek V4 Flash",
    api="openai-completions",
    provider="deepseek",
)


def deepseekProvider() -> Provider:
    return _create_provider(
        id="deepseek",
        name="DeepSeek",
        models=(_DEEPSEEK_MODEL,),
        streamSimple=_stream_simple,
    )


def _input_text(message: UserMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(block.text for block in content)


def _assistant_text(message: AssistantMessage) -> str:
    return "".join(
        block.text for block in message.content if isinstance(block, TextContent)
    )


def _request_messages(context: Context) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if context.systemPrompt is not None:
        messages.append({"role": "system", "content": context.systemPrompt})
    for message in context.messages:
        if isinstance(message, UserMessage):
            messages.append({"role": "user", "content": _input_text(message)})
        elif isinstance(message, AssistantMessage):
            messages.append({"role": "assistant", "content": _assistant_text(message)})
    return messages


def _request_body(context: Context) -> bytes:
    payload = {
        "model": _DEEPSEEK_MODEL_ID,
        "messages": _request_messages(context),
        "stream": True,
        "stream_options": {"include_usage": True},
        "thinking": {"type": "disabled"},
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _usage_from_payload(payload: Mapping[str, object]) -> Usage:
    prompt_tokens = payload["prompt_tokens"]
    completion_tokens = payload["completion_tokens"]
    total_tokens = payload["total_tokens"]
    cache_read = payload.get("prompt_cache_hit_tokens", 0)
    if (
        type(prompt_tokens) is not int
        or type(completion_tokens) is not int
        or type(total_tokens) is not int
        or type(cache_read) is not int
    ):
        raise ModelsError("stream", "DeepSeek usage is invalid")
    input_tokens = prompt_tokens - cache_read
    input_cost = input_tokens / 1_000_000 * 0.14
    output_cost = completion_tokens / 1_000_000 * 0.28
    cache_read_cost = cache_read / 1_000_000 * 0.0028
    return Usage(
        input=input_tokens,
        output=completion_tokens,
        cacheRead=cache_read,
        cacheWrite=0,
        totalTokens=total_tokens,
        cost=UsageCost(
            input=input_cost,
            output=output_cost,
            cacheRead=cache_read_cost,
            cacheWrite=0.0,
            total=input_cost + output_cost + cache_read_cost,
        ),
    )


def _empty_partial(model: Model) -> AssistantMessage:
    return AssistantMessage(
        content=(),
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=_ZERO_USAGE,
        stopReason="stop",
        timestamp=0,
    )


async def _iter_sse_payloads(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    buffer = bytearray(await response.aread())
    while True:
        newline = buffer.find(b"\n\n")
        crlf = buffer.find(b"\r\n\r\n")
        if newline < 0 and crlf < 0:
            break
        if crlf >= 0 and (newline < 0 or crlf < newline):
            event = bytes(buffer[:crlf])
            del buffer[: crlf + 4]
        else:
            event = bytes(buffer[:newline])
            del buffer[: newline + 2]
        for line in event.replace(b"\r\n", b"\n").split(b"\n"):
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                return
            try:
                payload = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ModelsError("stream", "DeepSeek stream is invalid") from error
            if type(payload) is not dict:
                raise ModelsError("stream", "DeepSeek stream is invalid")
            yield cast(dict[str, Any], payload)


def _emit(validator: _AssistantMessageEventValidator, event: AssistantMessageEvent) -> AssistantMessageEvent:
    validator.accept(event)
    return event


async def _stream_simple(
    model: Model,
    context: Context,
) -> AsyncIterator[AssistantMessageEvent]:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise ModelsError("auth", "DeepSeek authentication is not configured")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
    }
    body = _request_body(context)
    validator = _AssistantMessageEventValidator()
    partial = _empty_partial(model)
    text = ""
    text_open = False
    finish: Literal["stop", "length", "toolUse"] | None = None
    usage = _ZERO_USAGE

    async with httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(retries=0),
        trust_env=False,
        follow_redirects=False,
        timeout=None,
    ) as client:
        async with client.stream(
            "POST",
            _DEEPSEEK_URL,
            headers=headers,
            content=body,
        ) as response:
            if response.status_code != 200:
                raise ModelsError("provider", "DeepSeek request failed")
            yield _emit(validator, AssistantMessageStartEvent(partial=partial))
            async for payload in _iter_sse_payloads(response):
                response_id = payload.get("id")
                response_model = payload.get("model")
                if type(response_id) is str and partial.responseId is None:
                    partial = replace(partial, responseId=response_id)
                if type(response_model) is str and partial.responseModel is None:
                    partial = replace(partial, responseModel=response_model)
                raw_usage = payload.get("usage")
                if isinstance(raw_usage, Mapping):
                    usage = _usage_from_payload(raw_usage)
                choices = payload.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, dict):
                    raise ModelsError("stream", "DeepSeek stream is invalid")
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    if delta.get("reasoning_content") is not None:
                        raise ModelsError(
                            "stream", "DeepSeek stream included reasoning content"
                        )
                    fragment = delta.get("content")
                    if type(fragment) is str and fragment:
                        if not text_open:
                            opened = replace(
                                partial,
                                content=(TextContent(text=""),),
                            )
                            yield _emit(
                                validator,
                                AssistantMessageTextStartEvent(
                                    contentIndex=0,
                                    partial=opened,
                                ),
                            )
                            partial = opened
                            text_open = True
                        text += fragment
                        partial = replace(
                            partial,
                            content=(TextContent(text=text),),
                        )
                        yield _emit(
                            validator,
                            AssistantMessageTextDeltaEvent(
                                contentIndex=0,
                                delta=fragment,
                                partial=partial,
                            ),
                        )
                finish_reason = choice.get("finish_reason")
                if type(finish_reason) is str:
                    mapped = _FINISH_REASONS.get(finish_reason)
                    if mapped is None:
                        raise ModelsError("stream", "DeepSeek finish reason is invalid")
                    finish = mapped

    if text_open:
        yield _emit(
            validator,
            AssistantMessageTextEndEvent(
                contentIndex=0,
                content=text,
                partial=partial,
            ),
        )
    if finish is None:
        raise ModelsError("stream", "DeepSeek stream ended without a finish reason")
    message = replace(
        partial,
        usage=usage,
        stopReason=finish,
    )
    yield _emit(
        validator,
        AssistantMessageDoneEvent(reason=finish, message=message),
    )
