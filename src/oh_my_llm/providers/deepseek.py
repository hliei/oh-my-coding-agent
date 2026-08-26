from __future__ import annotations

import asyncio as _asyncio
from collections.abc import AsyncIterator as _AsyncIterator, Mapping as _Mapping
from contextlib import asynccontextmanager as _asynccontextmanager
from dataclasses import replace as _replace
import json as _json
import os as _os
from typing import (
    Any as _Any,
    Literal as _Literal,
    NoReturn as _NoReturn,
    Protocol as _Protocol,
    TypeVar as _TypeVar,
    cast as _cast,
)

import httpx as _httpx

from .._errors import LifecycleError as _LifecycleError, ModelsError as _ModelsError
from .._canonical import _decodeJSONValue, encodeCanonical as _encodeCanonical
from .._models import (
    Model as _Model,
    Provider as _Provider,
    _create_model,
    _create_provider,
)
from .._streams import _active_abort_signal
from .._values import (
    AssistantMessage as _AssistantMessage,
    AssistantMessageDoneEvent as _AssistantMessageDoneEvent,
    AssistantMessageErrorEvent as _AssistantMessageErrorEvent,
    AssistantMessageEvent as _AssistantMessageEvent,
    AssistantMessageStartEvent as _AssistantMessageStartEvent,
    AssistantMessageTextDeltaEvent as _AssistantMessageTextDeltaEvent,
    AssistantMessageTextEndEvent as _AssistantMessageTextEndEvent,
    AssistantMessageTextStartEvent as _AssistantMessageTextStartEvent,
    AssistantMessageToolCallDeltaEvent as _AssistantMessageToolCallDeltaEvent,
    AssistantMessageToolCallEndEvent as _AssistantMessageToolCallEndEvent,
    AssistantMessageToolCallStartEvent as _AssistantMessageToolCallStartEvent,
    Context as _Context,
    JSONValue as _JSONValue,
    SimpleStreamOptions as _SimpleStreamOptions,
    TextContent as _TextContent,
    Tool as _Tool,
    ToolCall as _ToolCall,
    ToolResultMessage as _ToolResultMessage,
    Usage as _Usage,
    UsageCost as _UsageCost,
    UserMessage as _UserMessage,
    _AssistantMessageEventValidator,
)

__all__ = ("deepseekProvider",)

# The future-feature binding is not part of the selected child-module surface.
globals().pop("annotations", None)


_DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
_DEEPSEEK_MODEL_ID = "deepseek-v4-flash"
_CONTEXT_OVERFLOW_ERROR = "DeepSeek context window exceeded"
_ZERO_COST = _UsageCost(
    input=0.0,
    output=0.0,
    cacheRead=0.0,
    cacheWrite=0.0,
    total=0.0,
)
_ZERO_USAGE = _Usage(
    input=0,
    output=0,
    cacheRead=0,
    cacheWrite=0,
    totalTokens=0,
    cost=_ZERO_COST,
)
_FINISH_REASONS: dict[str, _Literal["stop", "length", "toolUse"]] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "toolUse",
}
_ClosableT = _TypeVar("_ClosableT", bound="_AsyncClosable")
_DEEPSEEK_MODEL = _create_model(
    id=_DEEPSEEK_MODEL_ID,
    name="DeepSeek V4 Flash",
    api="openai-completions",
    provider="deepseek",
)


def deepseekProvider() -> _Provider:
    return _create_provider(
        id="deepseek",
        name="DeepSeek",
        models=(_DEEPSEEK_MODEL,),
        streamSimple=_stream_simple,
    )


# Keep the runtime annotation resolvable without exporting Provider here.
deepseekProvider.__annotations__["return"] = _Provider


def _input_text(message: _UserMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(block.text for block in content)


def _assistant_text(message: _AssistantMessage) -> str:
    return "".join(
        block.text for block in message.content if isinstance(block, _TextContent)
    )


def _tool_calls(message: _AssistantMessage) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "id": block.id,
            "type": "function",
            "function": {
                "name": block.name,
                "arguments": _encodeCanonical(block.arguments).decode("utf-8"),
            },
        }
        for block in message.content
        if isinstance(block, _ToolCall)
    )


def _request_messages(context: _Context) -> tuple[dict[str, object], ...]:
    messages: list[dict[str, object]] = []
    if context.systemPrompt is not None:
        messages.append({"role": "system", "content": context.systemPrompt})
    for message in context.messages:
        if isinstance(message, _UserMessage):
            messages.append({"role": "user", "content": _input_text(message)})
        elif isinstance(message, _AssistantMessage):
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
        elif isinstance(message, _ToolResultMessage):
            text = "\n".join(block.text for block in message.content)
            messages.append(
                {
                    "role": "tool",
                    "content": text or "(no tool output)",
                    "tool_call_id": message.toolCallId,
                }
            )
    return tuple(messages)


def _request_tool(tool: _Tool) -> dict[str, object]:
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
    model: _Model,
    context: _Context,
    options: _SimpleStreamOptions | None,
) -> bytes:
    if model is not _DEEPSEEK_MODEL:
        raise _ModelsError("model_validation", "DeepSeek model is invalid")
    if options is not None and type(options) is not _SimpleStreamOptions:
        raise TypeError("options must be a SimpleStreamOptions value")
    if options is not None:
        if options.temperature is not None and options.temperature > 2.0:
            raise _ModelsError(
                "model_validation", "DeepSeek temperature must be between 0 and 2"
            )
        if options.maxTokens is not None and options.maxTokens > 384_000:
            raise _ModelsError(
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
    return _encodeCanonical(payload)


def _usage_from_payload(payload: _Mapping[str, object]) -> _Usage:
    def count(name: str) -> int:
        raw = payload.get(name)
        if type(raw) is not int or not 0 <= raw <= 2**53 - 1:
            raise _ModelsError("stream", "DeepSeek usage is invalid")
        return raw

    prompt_tokens = count("prompt_tokens")
    completion_tokens = count("completion_tokens")
    total_tokens = count("total_tokens")
    cache_read = count("prompt_cache_hit_tokens")
    if cache_read > prompt_tokens:
        raise _ModelsError("stream", "DeepSeek usage is invalid")
    input_tokens = prompt_tokens - cache_read
    if "prompt_cache_miss_tokens" in payload:
        cache_miss = count("prompt_cache_miss_tokens")
        if cache_miss != input_tokens:
            raise _ModelsError("stream", "DeepSeek usage is invalid")
    completion_details = payload.get("completion_tokens_details")
    if isinstance(completion_details, _Mapping) and "reasoning_tokens" in completion_details:
        raise _ModelsError("stream", "DeepSeek usage is invalid")
    if total_tokens != input_tokens + completion_tokens + cache_read:
        raise _ModelsError("stream", "DeepSeek usage is invalid")
    input_cost = input_tokens / 1_000_000 * 0.14
    output_cost = completion_tokens / 1_000_000 * 0.28
    cache_read_cost = cache_read / 1_000_000 * 0.0028
    return _Usage(
        input=input_tokens,
        output=completion_tokens,
        cacheRead=cache_read,
        cacheWrite=0,
        totalTokens=total_tokens,
        cost=_UsageCost(
            input=input_cost,
            output=output_cost,
            cacheRead=cache_read_cost,
            cacheWrite=0.0,
            total=input_cost + output_cost + cache_read_cost,
        ),
    )


def _empty_partial(model: _Model) -> _AssistantMessage:
    return _AssistantMessage(
        content=(),
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=_ZERO_USAGE,
        stopReason="stop",
        timestamp=0,
    )


async def _iter_sse_payloads(response: _httpx.Response) -> _AsyncIterator[dict[str, _Any]]:
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
                    raise _ModelsError("stream", "DeepSeek stream is invalid")
                return
            try:
                payload = _json.loads(
                    data.decode("utf-8"),
                    object_pairs_hook=_wire_object,
                    parse_constant=_reject_json_constant,
                )
            except (UnicodeDecodeError, ValueError) as error:
                raise _ModelsError("stream", "DeepSeek stream is invalid") from error
            if type(payload) is not dict:
                raise _ModelsError("stream", "DeepSeek stream is invalid")
            yield _cast(dict[str, _Any], payload)
    raise _ModelsError("stream", "DeepSeek stream ended without terminal data")


def _wire_object(pairs: list[tuple[str, _Any]]) -> dict[str, _Any]:
    value: dict[str, _Any] = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("duplicate JSON key")
        value[name] = item
    return value


def _reject_json_constant(value: str) -> _NoReturn:
    del value
    raise ValueError("invalid JSON constant")


def _emit(validator: _AssistantMessageEventValidator, event: _AssistantMessageEvent) -> _AssistantMessageEvent:
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
        self.arguments: _Mapping[str, _JSONValue] = {}

    def value(self) -> _ToolCall:
        return _ToolCall(id=self.id, name=self.name, arguments=self.arguments)

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

    def finalize(self) -> _ToolCall:
        if self.argumentsSeen:
            finalized = _parsed_tool_arguments(self.fragments)
            if finalized is None:
                raise _ModelsError(
                    "stream", "DeepSeek Tool Call arguments are invalid"
                )
            self.arguments = finalized
        return self.value()


def _replace_content(
    partial: _AssistantMessage,
    content_index: int,
    block: _TextContent | _ToolCall,
) -> _AssistantMessage:
    content = list(partial.content)
    content[content_index] = block
    return _replace(partial, content=tuple(content))


def _tool_delta_parts(
    raw: object,
) -> tuple[int, str | None, str | None, str]:
    if not isinstance(raw, dict):
        raise _ModelsError("stream", "DeepSeek Tool Call delta is invalid")
    stream_index = raw.get("index")
    if type(stream_index) is not int or stream_index < 0:
        raise _ModelsError("stream", "DeepSeek Tool Call delta is invalid")
    id_fragment = raw.get("id")
    if id_fragment is not None and type(id_fragment) is not str:
        raise _ModelsError("stream", "DeepSeek Tool Call delta is invalid")
    function = raw.get("function")
    if function is None:
        function = {}
    if not isinstance(function, dict):
        raise _ModelsError("stream", "DeepSeek Tool Call delta is invalid")
    name_fragment = function.get("name")
    argument_fragment = function.get("arguments", "")
    if name_fragment is not None and type(name_fragment) is not str:
        raise _ModelsError("stream", "DeepSeek Tool Call delta is invalid")
    if type(argument_fragment) is not str:
        raise _ModelsError("stream", "DeepSeek Tool Call delta is invalid")
    return stream_index, id_fragment, name_fragment, argument_fragment


def _parsed_tool_arguments(fragments: str) -> _Mapping[str, _JSONValue] | None:
    try:
        decoded = _decodeJSONValue(fragments.encode("utf-8"))
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, _Mapping):
        return None
    return decoded


class _DeepSeekOperationState:
    __slots__ = ("latest",)

    def __init__(self, latest: _AssistantMessage) -> None:
        self.latest = latest

    def remember(self, latest: _AssistantMessage) -> _AssistantMessage:
        self.latest = latest
        return latest


def _terminal_error(
    validator: _AssistantMessageEventValidator,
    state: _DeepSeekOperationState,
    *,
    reason: _Literal["error", "aborted"],
    message: str,
) -> _AssistantMessageErrorEvent:
    error = _replace(
        state.latest,
        stopReason=reason,
        errorMessage=message,
    )
    terminal = _AssistantMessageErrorEvent(reason=reason, error=error)
    validator.accept(terminal)
    return terminal


async def _stream_simple(
    model: _Model,
    context: _Context,
    options: _SimpleStreamOptions | None,
) -> _AsyncIterator[_AssistantMessageEvent]:
    validator = _AssistantMessageEventValidator()
    state = _DeepSeekOperationState(_empty_partial(model))
    try:
        async for event in _stream_simple_operation(
            model, context, options, state, validator
        ):
            if isinstance(event, _AssistantMessageStartEvent):
                state.remember(event.partial)
            elif isinstance(event, _AssistantMessageDoneEvent):
                state.remember(event.message)
            elif isinstance(event, _AssistantMessageErrorEvent):
                state.remember(event.error)
            else:
                state.remember(event.partial)
            yield event
    except _asyncio.CancelledError:
        signal = _active_abort_signal()
        if signal is None or not signal.aborted:
            raise
        yield _terminal_error(
            validator,
            state,
            reason="aborted",
            message="Operation aborted",
        )
    except _LifecycleError:
        raise
    except _ModelsError as classified:
        if classified.code == "model_validation":
            raise
        public_error = (
            _CONTEXT_OVERFLOW_ERROR
            if str(classified) == _CONTEXT_OVERFLOW_ERROR
            else {
                "auth": "DeepSeek authentication failed",
                "provider": "DeepSeek request failed",
                "stream": "DeepSeek stream failed",
            }.get(classified.code, "DeepSeek request failed")
        )
        yield _terminal_error(
            validator,
            state,
            reason="error",
            message=public_error,
        )
    except Exception as cause:
        transport_failure = _ModelsError(
            "provider",
            "DeepSeek request failed",
            cause=cause,
        )
        yield _terminal_error(
            validator,
            state,
            reason="error",
            message=str(transport_failure),
        )


def _raise_cleanup_failure(
    pending: BaseException | None,
    failure: BaseException,
) -> _NoReturn:
    prior_carrier: BaseException | None = pending
    if isinstance(pending, _asyncio.CancelledError):
        prior_carrier = pending.__cause__
    prior = (
        prior_carrier.causes
        if isinstance(prior_carrier, _LifecycleError)
        and prior_carrier.code == "cleanup"
        else ()
    )
    cleanup = _LifecycleError(
        "cleanup",
        "DeepSeek cleanup failed",
        causes=(*prior, failure),
    )
    signal = _active_abort_signal()
    if (
        isinstance(pending, _asyncio.CancelledError)
        and (signal is None or not signal.aborted)
    ):
        pending.__cause__ = cleanup
        raise pending
    raise cleanup from failure


class _AsyncClosable(_Protocol):
    async def aclose(self) -> None: ...


@_asynccontextmanager
async def _owned_resource(resource: _ClosableT) -> _AsyncIterator[_ClosableT]:
    pending: BaseException | None = None
    try:
        yield resource
    except BaseException as failure:
        pending = failure
    try:
        await resource.aclose()
    except BaseException as failure:
        _raise_cleanup_failure(pending, failure)
    if pending is not None:
        raise pending


@_asynccontextmanager
async def _deepseek_client() -> _AsyncIterator[_httpx.AsyncClient]:
    client = _httpx.AsyncClient(
        transport=_httpx.AsyncHTTPTransport(retries=0),
        trust_env=False,
        follow_redirects=False,
        timeout=None,
    )
    async with _owned_resource(client):
        yield client


@_asynccontextmanager
async def _deepseek_response(
    client: _httpx.AsyncClient,
    *,
    headers: _Mapping[str, str],
    body: bytes,
) -> _AsyncIterator[_httpx.Response]:
    request = client.build_request(
        "POST",
        _DEEPSEEK_URL,
        headers=headers,
        content=body,
    )
    response = await client.send(request, stream=True)
    async with _owned_resource(response):
        yield response


async def _is_context_overflow_response(response: _httpx.Response) -> bool:
    if response.status_code != 400:
        return False
    try:
        payload = _json.loads(await response.aread())
    except (_json.JSONDecodeError, UnicodeDecodeError):
        return False
    if type(payload) is not dict or type(payload.get("error")) is not dict:
        return False
    error = _cast(dict[str, object], payload["error"])
    return error.get("code") in {
        "context_length_exceeded",
        "context_window_exceeded",
    }


async def _stream_simple_operation(
    model: _Model,
    context: _Context,
    options: _SimpleStreamOptions | None,
    operation: _DeepSeekOperationState,
    validator: _AssistantMessageEventValidator,
) -> _AsyncIterator[_AssistantMessageEvent]:
    body = _request_body(model, context, options)
    partial = operation.latest
    api_key = _os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise _ModelsError("auth", "DeepSeek authentication failed")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
    }
    text_index: int | None = None
    tool_calls: dict[int, _StreamingToolCall] = {}
    finish: _Literal["stop", "length", "toolUse"] | None = None
    usage = _ZERO_USAGE
    usage_seen = False

    async with _deepseek_client() as client:
        async with _deepseek_response(client, headers=headers, body=body) as response:
            if response.status_code != 200:
                context_overflow = await _is_context_overflow_response(response)
                raise _ModelsError(
                    "auth" if response.status_code in (401, 403) else "provider",
                    (
                        "DeepSeek authentication failed"
                        if response.status_code in (401, 403)
                        else _CONTEXT_OVERFLOW_ERROR
                        if context_overflow
                        else "DeepSeek request failed"
                    ),
                )
            yield _emit(validator, _AssistantMessageStartEvent(partial=partial))
            async for payload in _iter_sse_payloads(response):
                if usage_seen:
                    raise _ModelsError(
                        "stream", "DeepSeek usage payload must be unique and terminal"
                    )
                response_id = payload.get("id")
                response_model = payload.get("model")
                if type(response_id) is str and partial.responseId is None:
                    partial = operation.remember(
                        _replace(partial, responseId=response_id)
                    )
                if type(response_model) is str and partial.responseModel is None:
                    partial = operation.remember(
                        _replace(partial, responseModel=response_model)
                    )
                if "usage" in payload:
                    raw_usage = payload["usage"]
                    if not isinstance(raw_usage, _Mapping):
                        raise _ModelsError("stream", "DeepSeek usage is invalid")
                    usage = _usage_from_payload(raw_usage)
                    usage_seen = True
                    operation.remember(_replace(partial, usage=usage))
                choices = payload.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, dict):
                    raise _ModelsError("stream", "DeepSeek stream is invalid")
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    if delta.get("reasoning_content") is not None:
                        raise _ModelsError(
                            "stream", "DeepSeek stream included reasoning content"
                        )
                    fragment = delta.get("content")
                    if type(fragment) is str and fragment:
                        if text_index is None:
                            text_index = len(partial.content)
                            opened = _replace(
                                partial,
                                content=(*partial.content, _TextContent(text="")),
                            )
                            yield _emit(
                                validator,
                                _AssistantMessageTextStartEvent(
                                    contentIndex=text_index,
                                    partial=opened,
                                ),
                            )
                            partial = operation.remember(opened)
                        prior_text = partial.content[text_index]
                        assert isinstance(prior_text, _TextContent)
                        partial = operation.remember(
                            _replace_content(
                                partial,
                                text_index,
                                _TextContent(text=prior_text.text + fragment),
                            )
                        )
                        yield _emit(
                            validator,
                            _AssistantMessageTextDeltaEvent(
                                contentIndex=text_index,
                                delta=fragment,
                                partial=partial,
                            ),
                        )
                    raw_tool_calls = delta.get("tool_calls")
                    if raw_tool_calls is not None:
                        if not isinstance(raw_tool_calls, list):
                            raise _ModelsError(
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
                                    _replace(
                                        partial,
                                        content=(*partial.content, state.value()),
                                    )
                                )
                                yield _emit(
                                    validator,
                                    _AssistantMessageToolCallStartEvent(
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
                                _AssistantMessageToolCallDeltaEvent(
                                    contentIndex=state.contentIndex,
                                    delta=argument_fragment,
                                    partial=partial,
                                ),
                            )
                finish_reason = choice.get("finish_reason")
                if type(finish_reason) is str:
                    mapped = _FINISH_REASONS.get(finish_reason)
                    if mapped is None:
                        raise _ModelsError("stream", "DeepSeek finish reason is invalid")
                    finish = mapped

    tool_calls_by_content = {
        state.contentIndex: state for state in tool_calls.values()
    }
    for content_index, block in enumerate(partial.content):
        if isinstance(block, _TextContent):
            yield _emit(
                validator,
                _AssistantMessageTextEndEvent(
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
                _AssistantMessageToolCallEndEvent(
                    contentIndex=content_index,
                    toolCall=tool_call,
                    partial=partial,
                ),
            )
    if finish is None:
        raise _ModelsError("stream", "DeepSeek stream ended without a finish reason")
    if not usage_seen:
        raise _ModelsError("stream", "DeepSeek stream ended without terminal usage")
    message = _replace(
        partial,
        usage=usage,
        stopReason=finish,
    )
    yield _emit(
        validator,
        _AssistantMessageDoneEvent(reason=finish, message=message),
    )
