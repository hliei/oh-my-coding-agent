from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
import json
import os
from typing import Any, Literal, NoReturn, cast

import httpx

from .._errors import LifecycleError, ModelsError
from .._canonical import _decodeJSONValue, encodeCanonical
from .._models import Model, Provider, _create_model, _create_provider
from .._values import (
    AssistantMessage,
    AssistantMessageDoneEvent,
    AssistantMessageErrorEvent,
    AssistantMessageEvent,
    AssistantMessageStartEvent,
    AssistantMessageTextDeltaEvent,
    AssistantMessageTextEndEvent,
    AssistantMessageTextStartEvent,
    AssistantMessageToolCallDeltaEvent,
    AssistantMessageToolCallEndEvent,
    AssistantMessageToolCallStartEvent,
    Context,
    JSONValue,
    SimpleStreamOptions,
    TextContent,
    Tool,
    ToolCall,
    ToolResultMessage,
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


def _tool_calls(message: AssistantMessage) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "id": block.id,
            "type": "function",
            "function": {
                "name": block.name,
                "arguments": encodeCanonical(block.arguments).decode("utf-8"),
            },
        }
        for block in message.content
        if isinstance(block, ToolCall)
    )


def _request_messages(context: Context) -> tuple[dict[str, object], ...]:
    messages: list[dict[str, object]] = []
    if context.systemPrompt is not None:
        messages.append({"role": "system", "content": context.systemPrompt})
    for message in context.messages:
        if isinstance(message, UserMessage):
            messages.append({"role": "user", "content": _input_text(message)})
        elif isinstance(message, AssistantMessage):
            text = _assistant_text(message)
            calls = _tool_calls(message)
            if text or calls:
                assistant: dict[str, object] = {
                    "role": "assistant",
                    "content": text or None,
                }
                if calls:
                    assistant["tool_calls"] = calls
                messages.append(assistant)
        elif isinstance(message, ToolResultMessage):
            text = "\n".join(block.text for block in message.content)
            messages.append(
                {
                    "role": "tool",
                    "content": text or "(no tool output)",
                    "tool_call_id": message.toolCallId,
                }
            )
    return tuple(messages)


def _request_tool(tool: Tool) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
            "strict": False,
        },
    }


def _request_body(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None,
) -> bytes:
    if model is not _DEEPSEEK_MODEL:
        raise ModelsError("model_validation", "DeepSeek model is invalid")
    if options is not None and type(options) is not SimpleStreamOptions:
        raise TypeError("options must be a SimpleStreamOptions value")
    if options is not None:
        if options.temperature is not None and options.temperature > 2.0:
            raise ModelsError(
                "model_validation", "DeepSeek temperature must be between 0 and 2"
            )
        if options.maxTokens is not None and options.maxTokens > 384_000:
            raise ModelsError(
                "model_validation", "DeepSeek maxTokens must not exceed 384000"
            )

    payload: dict[str, object] = {
        "model": _DEEPSEEK_MODEL_ID,
        "messages": _request_messages(context),
        "stream": True,
        "stream_options": {"include_usage": True},
        "thinking": {"type": "disabled"},
    }
    if context.tools:
        payload["tools"] = tuple(_request_tool(tool) for tool in context.tools)
    if options is not None and options.temperature is not None:
        payload["temperature"] = options.temperature
    if options is not None and options.maxTokens is not None:
        payload["max_completion_tokens"] = options.maxTokens
    return encodeCanonical(payload)


def _usage_from_payload(payload: Mapping[str, object]) -> Usage:
    def count(name: str) -> int:
        raw = payload.get(name)
        if type(raw) is not int or not 0 <= raw <= 2**53 - 1:
            raise ModelsError("stream", "DeepSeek usage is invalid")
        return raw

    prompt_tokens = count("prompt_tokens")
    completion_tokens = count("completion_tokens")
    total_tokens = count("total_tokens")
    cache_read = count("prompt_cache_hit_tokens")
    if cache_read > prompt_tokens:
        raise ModelsError("stream", "DeepSeek usage is invalid")
    input_tokens = prompt_tokens - cache_read
    if "prompt_cache_miss_tokens" in payload:
        cache_miss = count("prompt_cache_miss_tokens")
        if cache_miss != input_tokens:
            raise ModelsError("stream", "DeepSeek usage is invalid")
    completion_details = payload.get("completion_tokens_details")
    if isinstance(completion_details, Mapping) and "reasoning_tokens" in completion_details:
        raise ModelsError("stream", "DeepSeek usage is invalid")
    if total_tokens != input_tokens + completion_tokens + cache_read:
        raise ModelsError("stream", "DeepSeek usage is invalid")
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
                if bytes(buffer).strip():
                    raise ModelsError("stream", "DeepSeek stream is invalid")
                return
            try:
                payload = json.loads(
                    data.decode("utf-8"),
                    object_pairs_hook=_wire_object,
                    parse_constant=_reject_json_constant,
                )
            except (UnicodeDecodeError, ValueError) as error:
                raise ModelsError("stream", "DeepSeek stream is invalid") from error
            if type(payload) is not dict:
                raise ModelsError("stream", "DeepSeek stream is invalid")
            yield cast(dict[str, Any], payload)
    raise ModelsError("stream", "DeepSeek stream ended without terminal data")


def _wire_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("duplicate JSON key")
        value[name] = item
    return value


def _reject_json_constant(value: str) -> NoReturn:
    del value
    raise ValueError("invalid JSON constant")


def _emit(validator: _AssistantMessageEventValidator, event: AssistantMessageEvent) -> AssistantMessageEvent:
    validator.accept(event)
    return event


class _StreamingToolCall:
    __slots__ = (
        "arguments",
        "argumentsSeen",
        "contentIndex",
        "fragments",
        "id",
        "name",
    )

    def __init__(self, *, content_index: int, id: str, name: str) -> None:
        self.contentIndex = content_index
        self.id = id
        self.name = name
        self.fragments = ""
        self.argumentsSeen = False
        self.arguments: Mapping[str, JSONValue] = {}

    def value(self) -> ToolCall:
        return ToolCall(id=self.id, name=self.name, arguments=self.arguments)

    def append_identity(
        self,
        id_fragment: str | None,
        name_fragment: str | None,
    ) -> None:
        if id_fragment:
            self.id += id_fragment
        if name_fragment:
            self.name += name_fragment

    def append_arguments(self, fragment: str) -> None:
        self.argumentsSeen = True
        self.fragments += fragment
        parsed = _parsed_tool_arguments(self.fragments)
        if parsed is not None:
            self.arguments = parsed

    def finalize(self) -> ToolCall:
        if self.argumentsSeen:
            finalized = _parsed_tool_arguments(self.fragments)
            if finalized is None:
                raise ModelsError(
                    "stream", "DeepSeek Tool Call arguments are invalid"
                )
            self.arguments = finalized
        return self.value()


def _replace_content(
    partial: AssistantMessage,
    content_index: int,
    block: TextContent | ToolCall,
) -> AssistantMessage:
    content = list(partial.content)
    content[content_index] = block
    return replace(partial, content=tuple(content))


def _tool_delta_parts(
    raw: object,
) -> tuple[int, str | None, str | None, str]:
    if not isinstance(raw, dict):
        raise ModelsError("stream", "DeepSeek Tool Call delta is invalid")
    stream_index = raw.get("index")
    if type(stream_index) is not int or stream_index < 0:
        raise ModelsError("stream", "DeepSeek Tool Call delta is invalid")
    id_fragment = raw.get("id")
    if id_fragment is not None and type(id_fragment) is not str:
        raise ModelsError("stream", "DeepSeek Tool Call delta is invalid")
    function = raw.get("function")
    if function is None:
        function = {}
    if not isinstance(function, dict):
        raise ModelsError("stream", "DeepSeek Tool Call delta is invalid")
    name_fragment = function.get("name")
    argument_fragment = function.get("arguments", "")
    if name_fragment is not None and type(name_fragment) is not str:
        raise ModelsError("stream", "DeepSeek Tool Call delta is invalid")
    if type(argument_fragment) is not str:
        raise ModelsError("stream", "DeepSeek Tool Call delta is invalid")
    return stream_index, id_fragment, name_fragment, argument_fragment


def _parsed_tool_arguments(fragments: str) -> Mapping[str, JSONValue] | None:
    try:
        decoded = _decodeJSONValue(fragments.encode("utf-8"))
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, Mapping):
        return None
    return decoded


class _DeepSeekOperationState:
    __slots__ = ("latest",)

    def __init__(self, latest: AssistantMessage) -> None:
        self.latest = latest

    def remember(self, latest: AssistantMessage) -> AssistantMessage:
        self.latest = latest
        return latest


async def _stream_simple(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None,
) -> AsyncIterator[AssistantMessageEvent]:
    validator = _AssistantMessageEventValidator()
    state = _DeepSeekOperationState(_empty_partial(model))
    try:
        async for event in _stream_simple_operation(model, context, options, state):
            validator.accept(event)
            if isinstance(event, AssistantMessageStartEvent):
                state.remember(event.partial)
            elif isinstance(event, AssistantMessageDoneEvent):
                state.remember(event.message)
            elif isinstance(event, AssistantMessageErrorEvent):
                state.remember(event.error)
            else:
                state.remember(event.partial)
            yield event
    except asyncio.CancelledError:
        raise
    except ModelsError as classified:
        if classified.code == "model_validation":
            raise
        public_error = {
            "auth": "DeepSeek authentication failed",
            "provider": "DeepSeek request failed",
            "stream": "DeepSeek stream failed",
        }.get(classified.code, "DeepSeek request failed")
        partial = state.latest
        error = replace(
            partial,
            stopReason="error",
            errorMessage=public_error,
        )
        terminal = AssistantMessageErrorEvent(reason="error", error=error)
        validator.accept(terminal)
        yield terminal
    except Exception as cause:
        owner = asyncio.current_task()
        if owner is not None and owner.cancelling():
            raise LifecycleError(
                "cleanup",
                "DeepSeek cleanup failed",
                causes=(cause,),
            ) from cause
        transport_failure = ModelsError(
            "provider",
            "DeepSeek request failed",
            cause=cause,
        )
        partial = state.latest
        error = replace(
            partial,
            stopReason="error",
            errorMessage=str(transport_failure),
        )
        terminal = AssistantMessageErrorEvent(reason="error", error=error)
        validator.accept(terminal)
        yield terminal


async def _stream_simple_operation(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None,
    operation: _DeepSeekOperationState,
) -> AsyncIterator[AssistantMessageEvent]:
    body = _request_body(model, context, options)
    validator = _AssistantMessageEventValidator()
    partial = operation.latest
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise ModelsError("auth", "DeepSeek authentication failed")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
    }
    text_index: int | None = None
    tool_calls: dict[int, _StreamingToolCall] = {}
    finish: Literal["stop", "length", "toolUse"] | None = None
    usage = _ZERO_USAGE
    usage_seen = False

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
                raise ModelsError(
                    "auth" if response.status_code in (401, 403) else "provider",
                    (
                        "DeepSeek authentication failed"
                        if response.status_code in (401, 403)
                        else "DeepSeek request failed"
                    ),
                )
            yield _emit(validator, AssistantMessageStartEvent(partial=partial))
            async for payload in _iter_sse_payloads(response):
                if usage_seen:
                    raise ModelsError(
                        "stream", "DeepSeek usage payload must be unique and terminal"
                    )
                response_id = payload.get("id")
                response_model = payload.get("model")
                if type(response_id) is str and partial.responseId is None:
                    partial = operation.remember(
                        replace(partial, responseId=response_id)
                    )
                if type(response_model) is str and partial.responseModel is None:
                    partial = operation.remember(
                        replace(partial, responseModel=response_model)
                    )
                if "usage" in payload:
                    raw_usage = payload["usage"]
                    if not isinstance(raw_usage, Mapping):
                        raise ModelsError("stream", "DeepSeek usage is invalid")
                    usage = _usage_from_payload(raw_usage)
                    usage_seen = True
                    operation.remember(replace(partial, usage=usage))
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
                        if text_index is None:
                            text_index = len(partial.content)
                            opened = replace(
                                partial,
                                content=(*partial.content, TextContent(text="")),
                            )
                            yield _emit(
                                validator,
                                AssistantMessageTextStartEvent(
                                    contentIndex=text_index,
                                    partial=opened,
                                ),
                            )
                            partial = operation.remember(opened)
                        prior_text = partial.content[text_index]
                        assert isinstance(prior_text, TextContent)
                        partial = operation.remember(
                            _replace_content(
                                partial,
                                text_index,
                                TextContent(text=prior_text.text + fragment),
                            )
                        )
                        yield _emit(
                            validator,
                            AssistantMessageTextDeltaEvent(
                                contentIndex=text_index,
                                delta=fragment,
                                partial=partial,
                            ),
                        )
                    raw_tool_calls = delta.get("tool_calls")
                    if raw_tool_calls is not None:
                        if not isinstance(raw_tool_calls, list):
                            raise ModelsError(
                                "stream", "DeepSeek Tool Call delta is invalid"
                            )
                        for raw_tool_call in raw_tool_calls:
                            (
                                stream_index,
                                id_fragment,
                                name_fragment,
                                argument_fragment,
                            ) = _tool_delta_parts(raw_tool_call)
                            state = tool_calls.get(stream_index)
                            if state is None:
                                state = _StreamingToolCall(
                                    content_index=len(partial.content),
                                    id=id_fragment or "",
                                    name=name_fragment or "",
                                )
                                tool_calls[stream_index] = state
                                partial = operation.remember(
                                    replace(
                                        partial,
                                        content=(*partial.content, state.value()),
                                    )
                                )
                                yield _emit(
                                    validator,
                                    AssistantMessageToolCallStartEvent(
                                        contentIndex=state.contentIndex,
                                        partial=partial,
                                    ),
                                )
                            else:
                                state.append_identity(id_fragment, name_fragment)
                            state.append_arguments(argument_fragment)
                            partial = operation.remember(
                                _replace_content(
                                    partial,
                                    state.contentIndex,
                                    state.value(),
                                )
                            )
                            yield _emit(
                                validator,
                                AssistantMessageToolCallDeltaEvent(
                                    contentIndex=state.contentIndex,
                                    delta=argument_fragment,
                                    partial=partial,
                                ),
                            )
                finish_reason = choice.get("finish_reason")
                if type(finish_reason) is str:
                    mapped = _FINISH_REASONS.get(finish_reason)
                    if mapped is None:
                        raise ModelsError("stream", "DeepSeek finish reason is invalid")
                    finish = mapped

    tool_calls_by_content = {
        state.contentIndex: state for state in tool_calls.values()
    }
    for content_index, block in enumerate(partial.content):
        if isinstance(block, TextContent):
            yield _emit(
                validator,
                AssistantMessageTextEndEvent(
                    contentIndex=content_index,
                    content=block.text,
                    partial=partial,
                ),
            )
        else:
            state = tool_calls_by_content[content_index]
            tool_call = state.finalize()
            partial = operation.remember(
                _replace_content(partial, content_index, tool_call)
            )
            yield _emit(
                validator,
                AssistantMessageToolCallEndEvent(
                    contentIndex=content_index,
                    toolCall=tool_call,
                    partial=partial,
                ),
            )
    if finish is None:
        raise ModelsError("stream", "DeepSeek stream ended without a finish reason")
    if not usage_seen:
        raise ModelsError("stream", "DeepSeek stream ended without terminal usage")
    message = replace(
        partial,
        usage=usage,
        stopReason=finish,
    )
    yield _emit(
        validator,
        AssistantMessageDoneEvent(reason=finish, message=message),
    )
