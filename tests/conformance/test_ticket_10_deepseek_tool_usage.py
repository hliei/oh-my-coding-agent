from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from oh_my_core import (
    AgentContext,
    AgentEvent,
    AgentLoopConfig,
    AgentTool,
    AgentToolResult,
    runAgentLoop,
)
from oh_my_llm import (
    AssistantMessageDoneEvent,
    AssistantMessageErrorEvent,
    AssistantMessageStartEvent,
    AssistantMessageTextDeltaEvent,
    AssistantMessageTextEndEvent,
    AssistantMessageTextStartEvent,
    AssistantMessageToolCallDeltaEvent,
    AssistantMessageToolCallEndEvent,
    AssistantMessageToolCallStartEvent,
    Context,
    ModelsError,
    SimpleStreamOptions,
    StreamOptions,
    TextContent,
    Tool,
    ToolCall,
    UserMessage,
    createModels,
)
from oh_my_llm.providers.deepseek import deepseekProvider


def _sse(*payloads: dict[str, Any] | str) -> bytes:
    chunks: list[bytes] = []
    for payload in payloads:
        if payload == "[DONE]":
            chunks.append(b"data: [DONE]\n\n")
        else:
            chunks.append(b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n")
    return b"".join(chunks)


_STOP_SSE = _sse(
    {
        "id": "chatcmpl-options",
        "model": "deepseek-v4-flash",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "prompt_cache_hit_tokens": 0,
        },
    },
    "[DONE]",
)

_NULLABLE_USAGE_SSE = _sse(
    {
        "id": "chatcmpl-nullable-usage",
        "model": "deepseek-v4-flash",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "finish_reason": None,
            }
        ],
        "usage": None,
    },
    {
        "id": "chatcmpl-nullable-usage",
        "model": "deepseek-v4-flash",
        "choices": [
            {"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}
        ],
        "usage": None,
    },
    {
        "id": "chatcmpl-nullable-usage",
        "model": "deepseek-v4-flash",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": None,
    },
    {
        "id": "chatcmpl-nullable-usage",
        "model": "deepseek-v4-flash",
        "choices": [],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 2,
            "total_tokens": 14,
            "prompt_cache_hit_tokens": 4,
        },
    },
    "[DONE]",
)


class _HttpSpy:
    def __init__(self, *bodies: bytes) -> None:
        self.bodies = list(bodies)
        self.requests: list[httpx.Request] = []
        self.client_kwargs: list[dict[str, Any]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = self

        class FakeClient(httpx.AsyncClient):
            def __init__(self, **kwargs: Any) -> None:
                spy.client_kwargs.append(dict(kwargs))
                next_body = spy.bodies.pop(0)
                kwargs = dict(kwargs)
                kwargs["transport"] = httpx.MockTransport(
                    lambda request: spy._handle(request, next_body)
                )
                super().__init__(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    def _handle(self, request: httpx.Request, body: bytes) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            content=body,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )


def _deepseek() -> tuple[Any, Any]:
    models = createModels()
    models.setProvider(deepseekProvider())
    model = models.getModel("deepseek", "deepseek-v4-flash")
    assert model is not None
    return models, model


async def _collect(stream: Any) -> list[Any]:
    return [event async for event in stream]


async def _invoke_models_helper(
    helper: str,
    models: Any,
    model: Any,
    context: Context,
    options: StreamOptions,
) -> None:
    if helper == "streamSimple":
        await _collect(models.streamSimple(model, context, options))
    elif helper == "completeSimple":
        await models.completeSimple(model, context, options)
    elif helper == "stream":
        await models.stream(model, context, options).result()
    else:
        assert helper == "complete"
        await models.complete(model, context, options)


def test_stream_options_are_one_public_value_with_portable_numeric_admission() -> None:
    assert SimpleStreamOptions is StreamOptions
    assert StreamOptions() == StreamOptions(temperature=None, maxTokens=None)
    assert StreamOptions(temperature=0.0, maxTokens=1).temperature == 0.0
    assert StreamOptions(temperature=3.0, maxTokens=2**53 - 1).maxTokens == 2**53 - 1

    for value in (-0.1, float("nan"), float("inf"), 1):
        with pytest.raises((TypeError, ValueError)):
            StreamOptions(temperature=value)
    for value in (0, -1, 1.0, True, 2**53):
        with pytest.raises((TypeError, ValueError)):
            StreamOptions(maxTokens=value)  # type: ignore[arg-type]


def test_deepseek_request_sends_tools_and_boundary_options_without_clamping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _HttpSpy(_STOP_SSE)
    spy.install(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    models, model = _deepseek()
    tool = Tool(
        name="echo",
        description="Echo a count",
        parameters={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ("count",),
            "additionalProperties": False,
        },
    )
    context = Context(
        messages=(UserMessage(content="Use echo", timestamp=0),),
        tools=(tool,),
    )

    asyncio.run(
        _collect(
            models.streamSimple(
                model,
                context,
                StreamOptions(temperature=2.0, maxTokens=384_000),
            )
        )
    )

    assert len(spy.requests) == 1
    body = json.loads(spy.requests[0].content)
    assert body["temperature"] == 2.0
    assert body["max_completion_tokens"] == 384_000
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo a count",
                "parameters": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                    "additionalProperties": False,
                },
                "strict": False,
            },
        }
    ]
    assert "tool_choice" not in body


@pytest.mark.parametrize(
    "options",
    (
        StreamOptions(temperature=2.000_001),
        StreamOptions(maxTokens=384_001),
    ),
)
@pytest.mark.parametrize(
    "helper",
    ("streamSimple", "completeSimple", "stream", "complete"),
)
def test_deepseek_rejects_provider_specific_option_limits_before_transport(
    monkeypatch: pytest.MonkeyPatch,
    options: StreamOptions,
    helper: str,
) -> None:
    spy = _HttpSpy(_STOP_SSE)
    spy.install(monkeypatch)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    models, model = _deepseek()
    context = Context(messages=(UserMessage(content="Hello", timestamp=0),))

    with pytest.raises(ModelsError) as caught:
        asyncio.run(_invoke_models_helper(helper, models, model, context, options))

    assert caught.value.code == "model_validation"
    assert spy.client_kwargs == []
    assert spy.requests == []


def test_all_four_models_helpers_share_identical_deepseek_request_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _HttpSpy(*(_STOP_SSE for _ in range(8)))
    spy.install(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    models, model = _deepseek()
    context = Context(messages=(UserMessage(content="Hello", timestamp=0),))
    helpers = ("streamSimple", "completeSimple", "stream", "complete")
    boundaries = (
        StreamOptions(temperature=0.0, maxTokens=1),
        StreamOptions(temperature=2.0, maxTokens=384_000),
    )

    async def invoke_all() -> None:
        for options in boundaries:
            for helper in helpers:
                await _invoke_models_helper(helper, models, model, context, options)

    asyncio.run(invoke_all())

    assert len(spy.requests) == 8
    low_bodies = {request.content for request in spy.requests[:4]}
    high_bodies = {request.content for request in spy.requests[4:]}
    assert len(low_bodies) == 1
    assert len(high_bodies) == 1
    low = json.loads(next(iter(low_bodies)))
    high = json.loads(next(iter(high_bodies)))
    assert (low["temperature"], low["max_completion_tokens"]) == (0.0, 1)
    assert (high["temperature"], high["max_completion_tokens"]) == (
        2.0,
        384_000,
    )


def test_all_helpers_accept_null_usage_until_one_usage_only_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "SECRET_NULLABLE_USAGE_CANARY"
    spy = _HttpSpy(*(_NULLABLE_USAGE_SSE for _ in range(4)))
    spy.install(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    models, model = _deepseek()
    context = Context(messages=(UserMessage(content="Hello", timestamp=0),))

    async def invoke_all() -> tuple[list[list[Any]], list[Any]]:
        observed_events: list[list[Any]] = []
        terminals: list[Any] = []
        simple_events = await _collect(models.streamSimple(model, context))
        observed_events.append(simple_events)
        simple_done = simple_events[-1]
        assert isinstance(simple_done, AssistantMessageDoneEvent)
        terminals.append(simple_done.message)

        terminals.append(await models.completeSimple(model, context))

        stream = models.stream(model, context)
        terminals.append(await stream.result())
        observed_events.append([event async for event in stream])

        terminals.append(await models.complete(model, context))
        return observed_events, terminals

    observed_events, terminals = asyncio.run(invoke_all())

    assert len(spy.requests) == 4
    assert len({request.content for request in spy.requests}) == 1
    assert [type(event) for event in observed_events[0]] == [
        AssistantMessageStartEvent,
        AssistantMessageTextStartEvent,
        AssistantMessageTextDeltaEvent,
        AssistantMessageTextEndEvent,
        AssistantMessageDoneEvent,
    ]
    assert [type(event) for event in observed_events[1]] == [
        AssistantMessageStartEvent,
        AssistantMessageTextStartEvent,
        AssistantMessageTextDeltaEvent,
        AssistantMessageTextEndEvent,
        AssistantMessageDoneEvent,
    ]
    for events, terminal in zip(observed_events, terminals[::2], strict=True):
        assert all(event.partial.usage.totalTokens == 0 for event in events[:-1])
        done = events[-1]
        assert isinstance(done, AssistantMessageDoneEvent)
        assert done.message is terminal
    assert all(terminal == terminals[0] for terminal in terminals)
    message = terminals[0]
    assert message.content == (TextContent(text="Hello"),)
    assert message.stopReason == "stop"
    assert message.usage.input == 8
    assert message.usage.output == 2
    assert message.usage.cacheRead == 4
    assert message.usage.totalTokens == 14
    assert message.usage.cost.total == 1.6912e-6
    assert secret not in f"{observed_events!s}{observed_events!r}{terminals!s}{terminals!r}"


_TOOL_SSE = _sse(
    {
        "id": "chatcmpl-tool",
        "model": "deepseek-v4-flash",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-",
                            "type": "function",
                            "function": {"name": "ec", "arguments": '{"count":'},
                        }
                    ]
                },
                "finish_reason": None,
            }
        ],
    },
    {
        "id": "chatcmpl-tool",
        "model": "deepseek-v4-flash",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "1",
                            "function": {"name": "ho", "arguments": '"2"}'},
                        }
                    ]
                },
                "finish_reason": None,
            }
        ],
    },
    {
        "id": "chatcmpl-tool",
        "model": "deepseek-v4-flash",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 5,
            "total_tokens": 25,
            "prompt_cache_hit_tokens": 4,
            "prompt_cache_miss_tokens": 16,
        },
    },
    "[DONE]",
)


def test_fragmented_tool_call_streams_cumulative_partials_and_one_exact_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _HttpSpy(_TOOL_SSE)
    spy.install(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    models, model = _deepseek()
    context = Context(messages=(UserMessage(content="Use echo", timestamp=0),))

    async def observe() -> tuple[list[Any], Any]:
        stream = models.stream(model, context)
        result = await stream.result()
        return [event async for event in stream], result

    events, result = asyncio.run(observe())

    assert [type(event) for event in events] == [
        AssistantMessageStartEvent,
        AssistantMessageToolCallStartEvent,
        AssistantMessageToolCallDeltaEvent,
        AssistantMessageToolCallDeltaEvent,
        AssistantMessageToolCallEndEvent,
        AssistantMessageDoneEvent,
    ]
    start = events[1]
    first_delta = events[2]
    second_delta = events[3]
    end = events[4]
    done = events[5]
    assert isinstance(start, AssistantMessageToolCallStartEvent)
    assert isinstance(first_delta, AssistantMessageToolCallDeltaEvent)
    assert isinstance(second_delta, AssistantMessageToolCallDeltaEvent)
    assert isinstance(end, AssistantMessageToolCallEndEvent)
    assert isinstance(done, AssistantMessageDoneEvent)
    assert start.partial.content == (
        ToolCall(id="call-", name="ec", arguments={}),
    )
    assert first_delta.delta == '{"count":'
    assert first_delta.partial.content == start.partial.content
    exact_call = ToolCall(id="call-1", name="echo", arguments={"count": "2"})
    assert second_delta.delta == '"2"}'
    assert second_delta.partial.content == (exact_call,)
    assert end.toolCall is end.partial.content[0]
    assert end.toolCall == exact_call
    assert done.reason == "toolUse"
    assert done.message is result
    assert done.message.content[0] is end.toolCall
    assert all(event.partial.usage.totalTokens == 0 for event in events[0:5])
    assert done.message.usage.input == 16
    assert done.message.usage.output == 5
    assert done.message.usage.cacheRead == 4
    assert done.message.usage.totalTokens == 25
    cost = done.message.usage.cost
    assert cost.input == 16 / 1_000_000 * 0.14
    assert cost.output == 5 / 1_000_000 * 0.28
    assert cost.cacheRead == 4 / 1_000_000 * 0.0028
    assert cost.cacheWrite == 0.0
    assert cost.total == cost.input + cost.output + cost.cacheRead + cost.cacheWrite


def test_length_finish_reason_maps_without_changing_terminal_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _HttpSpy(
        _sse(
            {
                "id": "chatcmpl-length",
                "model": "deepseek-v4-flash",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 3,
                    "total_tokens": 5,
                    "prompt_cache_hit_tokens": 0,
                },
            },
            "[DONE]",
        )
    )
    spy.install(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    models, model = _deepseek()

    message = asyncio.run(
        models.completeSimple(
            model,
            Context(messages=(UserMessage(content="Continue", timestamp=0),)),
        )
    )

    assert message.stopReason == "length"
    assert message.usage.totalTokens == 5


@pytest.mark.parametrize(
    "function_payload",
    (
        {"name": "echo", "arguments": '{"count":'},
        {"name": "echo"},
    ),
)
def test_missing_or_incomplete_tool_arguments_fail_without_default_or_repair(
    monkeypatch: pytest.MonkeyPatch,
    function_payload: dict[str, str],
) -> None:
    spy = _HttpSpy(
        _sse(
            {
                "id": "chatcmpl-broken-tool",
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "function": function_payload,
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                    "prompt_cache_hit_tokens": 0,
                },
            },
            "[DONE]",
        )
    )
    spy.install(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    models, model = _deepseek()

    events = asyncio.run(
        _collect(
            models.streamSimple(
                model,
                Context(messages=(UserMessage(content="Use echo", timestamp=0),)),
            )
        )
    )

    terminal = events[-1]
    assert isinstance(terminal, AssistantMessageErrorEvent)
    assert terminal.error.stopReason == "error"
    assert terminal.error.errorMessage == "DeepSeek stream failed"


_MISSING_USAGE = object()


def _usage_sse(usage: object = _MISSING_USAGE) -> bytes:
    payload: dict[str, Any] = {
        "id": "chatcmpl-usage",
        "model": "deepseek-v4-flash",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    if usage is not _MISSING_USAGE:
        payload["usage"] = usage
    return _sse(payload, "[DONE]")


@pytest.mark.parametrize(
    "usage",
    (
        _MISSING_USAGE,
        None,
        {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "prompt_cache_hit_tokens": 0,
        },
        {
            "prompt_tokens": True,
            "completion_tokens": 1,
            "total_tokens": 2,
            "prompt_cache_hit_tokens": 0,
        },
        {
            "prompt_tokens": 1,
            "completion_tokens": -1,
            "total_tokens": 0,
            "prompt_cache_hit_tokens": 0,
        },
        {
            "prompt_tokens": 2,
            "completion_tokens": 1,
            "total_tokens": 3,
            "prompt_cache_hit_tokens": 3,
        },
        {
            "prompt_tokens": 2,
            "completion_tokens": 1,
            "total_tokens": 3,
            "prompt_cache_hit_tokens": 1,
            "prompt_cache_miss_tokens": 2,
        },
        {
            "prompt_tokens": 2,
            "completion_tokens": 1,
            "total_tokens": 4,
            "prompt_cache_hit_tokens": 0,
        },
        {
            "prompt_tokens": 2,
            "completion_tokens": 1,
            "total_tokens": 3,
            "prompt_cache_hit_tokens": 0,
            "completion_tokens_details": {"reasoning_tokens": 1},
        },
    ),
)
def test_terminal_usage_fails_closed_on_missing_or_contradictory_counts(
    monkeypatch: pytest.MonkeyPatch,
    usage: object,
) -> None:
    spy = _HttpSpy(_usage_sse(usage))
    spy.install(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    models, model = _deepseek()
    context = Context(messages=(UserMessage(content="Hello", timestamp=0),))

    events = asyncio.run(_collect(models.streamSimple(model, context)))

    terminal = events[-1]
    assert isinstance(terminal, AssistantMessageErrorEvent)
    assert terminal.error.stopReason == "error"
    assert terminal.error.errorMessage == "DeepSeek stream failed"


_FINAL_TEXT_SSE = _sse(
    {
        "id": "chatcmpl-final",
        "model": "deepseek-v4-flash",
        "choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": None}],
    },
    {
        "id": "chatcmpl-final",
        "model": "deepseek-v4-flash",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 30,
            "completion_tokens": 1,
            "total_tokens": 31,
            "prompt_cache_hit_tokens": 0,
        },
    },
    "[DONE]",
)


def test_deepseek_terminal_tool_call_drives_the_existing_core_tool_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _HttpSpy(_TOOL_SSE, _FINAL_TEXT_SSE)
    spy.install(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    models, model = _deepseek()
    executed: list[tuple[str, dict[str, object]]] = []

    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del signal, on_update
        executed.append((tool_call_id, dict(params)))
        return AgentToolResult(
            content=(TextContent(text=f"echoed {params['count']}"),),
            details={"ok": True},
        )

    tool = AgentTool(
        name="echo",
        label="echo",
        description="Echo a count",
        parameters={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ("count",),
            "additionalProperties": False,
        },
        execute=execute,
    )

    async def stream_fn(model: Any, context: Any, options: Any, signal: Any) -> Any:
        del signal
        async for event in models.streamSimple(model, context, options):
            yield event

    events: list[AgentEvent] = []
    result = asyncio.run(
        runAgentLoop(
            (UserMessage(content="Use echo", timestamp=0),),
            AgentContext(systemPrompt="", messages=(), tools=(tool,)),
            AgentLoopConfig(model=model, temperature=0.0, maxTokens=1),
            events.append,
            stream_fn,
        )
    )

    assert [message.role for message in result] == [
        "user",
        "assistant",
        "toolResult",
        "assistant",
    ]
    assert executed == [("call-1", {"count": 2})]
    assert result[1].stopReason == "toolUse"  # type: ignore[union-attr]
    assert result[-1].content == (TextContent(text="done"),)
    first_assistant_end = next(
        event
        for event in events
        if isinstance(event, AgentEvent.MessageEnd)
        and getattr(event.message, "stopReason", None) == "toolUse"
    )
    assert first_assistant_end.message is result[1]
    assert len(spy.requests) == 2
    first_body, second_body = [json.loads(request.content) for request in spy.requests]
    assert first_body["temperature"] == 0.0
    assert first_body["max_completion_tokens"] == 1
    assert second_body["messages"] == [
        {"role": "system", "content": ""},
        {"role": "user", "content": "Use echo"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "arguments": '{"count":"2"}',
                    },
                }
            ],
        },
        {"role": "tool", "content": "echoed 2", "tool_call_id": "call-1"},
    ]


def test_usage_payload_is_unique_and_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage = {
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
        "prompt_cache_hit_tokens": 0,
    }
    spy = _HttpSpy(
        _sse(
            {
                "id": "chatcmpl-usage",
                "model": "deepseek-v4-flash",
                "choices": [],
                "usage": usage,
            },
            {
                "id": "chatcmpl-usage",
                "model": "deepseek-v4-flash",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": usage,
            },
            "[DONE]",
        )
    )
    spy.install(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    models, model = _deepseek()

    events = asyncio.run(
        _collect(
            models.streamSimple(
                model,
                Context(messages=(UserMessage(content="Hello", timestamp=0),)),
            )
        )
    )

    terminal = events[-1]
    assert isinstance(terminal, AssistantMessageErrorEvent)
    assert terminal.error.stopReason == "error"
    assert terminal.error.errorMessage == "DeepSeek stream failed"
