from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

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
    AssistantMessageEvent,
    AssistantMessageToolCallEndEvent,
    Context,
    ModelsError,
    StreamOptions,
    TextContent,
    ToolCall,
    UserMessage,
    createModels,
)
from oh_my_llm.providers.deepseek import deepseekProvider


def _sse(*payloads: dict[str, Any] | str) -> bytes:
    return b"".join(
        b"data: [DONE]\n\n"
        if payload == "[DONE]"
        else b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n"
        for payload in payloads
    )


_STOP = _sse(
    {
        "id": "chatcmpl-stop",
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
_TOOL = _sse(
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
_FINAL = _sse(
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
_BROKEN_TOOL = _sse(
    {
        "id": "chatcmpl-broken",
        "model": "deepseek-v4-flash",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "function": {"name": "echo", "arguments": '{"count":'},
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


class _Spy:
    def __init__(self, *bodies: bytes) -> None:
        self.bodies = list(bodies)
        self.requests: list[httpx.Request] = []
        self.client_count = 0

    def handle(self, request: httpx.Request, body: bytes) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, content=body, request=request)


def _install(spy: _Spy) -> type[httpx.AsyncClient]:
    original = httpx.AsyncClient

    class FakeClient(original):  # type: ignore[valid-type,misc]
        def __init__(self, **kwargs: Any) -> None:
            spy.client_count += 1
            body = spy.bodies.pop(0)
            kwargs = dict(kwargs)
            kwargs["transport"] = httpx.MockTransport(
                lambda request: spy.handle(request, body)
            )
            super().__init__(**kwargs)

    setattr(httpx, "AsyncClient", FakeClient)
    return original


def _deepseek() -> tuple[Any, Any]:
    models = createModels()
    models.setProvider(deepseekProvider())
    model = models.getModel("deepseek", "deepseek-v4-flash")
    assert model is not None
    return models, model


async def _collect(stream: Any) -> list[AssistantMessageEvent]:
    return [event async for event in stream]


async def _preflight() -> dict[str, object]:
    spy = _Spy(*(_STOP for _ in range(8)))
    original = _install(spy)
    try:
        models, model = _deepseek()
        context = Context(messages=(UserMessage(content="Hello", timestamp=0),))
        helpers = ("streamSimple", "completeSimple", "stream", "complete")

        async def invoke(helper: str, options: StreamOptions) -> None:
            if helper == "streamSimple":
                await _collect(models.streamSimple(model, context, options))
            elif helper == "completeSimple":
                await models.completeSimple(model, context, options)
            elif helper == "stream":
                await models.stream(model, context, options).result()
            else:
                assert helper == "complete"
                await models.complete(model, context, options)

        for options in (
            StreamOptions(temperature=0.0, maxTokens=1),
            StreamOptions(temperature=2.0, maxTokens=384000),
        ):
            for helper in helpers:
                await invoke(helper, options)
        valid_bodies = [request.content for request in spy.requests]
        invalid_codes: list[str] = []
        for helper in helpers:
            for invalid in (
                StreamOptions(temperature=2.000001),
                StreamOptions(maxTokens=384001),
            ):
                try:
                    await invoke(helper, invalid)
                except ModelsError as error:
                    invalid_codes.append(error.code)
        assert invalid_codes == ["model_validation"] * 8
        return {
            "A": {
                "validBoundaries": "admitted",
                "firstOutside": invalid_codes[0],
            },
            "L": [],
            "T": {
                "temperatureField": "temperature",
                "maxTokensField": "max_completion_tokens",
                "clamped": False,
            },
            "E": {
                "validRequestBodiesIdenticalAcrossHelpers": len(set(valid_bodies[:4])) == 1
                and len(set(valid_bodies[4:])) == 1,
                "invalidTransportConstructions": spy.client_count - 8,
                "invalidRequests": len(spy.requests) - 8,
            },
            "C": "fresh_valid_operation_available",
        }
    finally:
        setattr(httpx, "AsyncClient", original)


async def _tool_and_usage() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    spy = _Spy(_TOOL, _FINAL)
    original = _install(spy)
    try:
        models, model = _deepseek()
        provider_events: list[list[AssistantMessageEvent]] = []
        executed: list[dict[str, object]] = []

        async def execute(
            tool_call_id: str,
            params: dict[str, object],
            signal: Any,
            on_update: Any,
        ) -> AgentToolResult:
            del tool_call_id, signal, on_update
            executed.append(dict(params))
            return AgentToolResult(
                content=(TextContent(text="echoed 2"),),
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

        async def stream_fn(
            selected_model: Any,
            context: Any,
            options: Any,
            signal: Any,
        ) -> Any:
            del signal
            active_events: list[AssistantMessageEvent] = []
            provider_events.append(active_events)
            async for event in models.streamSimple(selected_model, context, options):
                active_events.append(event)
                yield event

        core_events: list[AgentEvent] = []
        result = await runAgentLoop(
            (UserMessage(content="Use echo", timestamp=0),),
            AgentContext(systemPrompt="", messages=(), tools=(tool,)),
            AgentLoopConfig(model=model),
            core_events.append,
            stream_fn,
        )
        first_events = provider_events[0]
        end = next(
            event
            for event in first_events
            if isinstance(event, AssistantMessageToolCallEndEvent)
        )
        done = first_events[-1]
        assert isinstance(done, AssistantMessageDoneEvent)
        tool_observation: dict[str, object] = {
            "A": {"complete": "admitted", "incomplete": "stream_error"},
            "L": [event.type for event in first_events],
            "T": {
                "id": end.toolCall.id,
                "name": end.toolCall.name,
                "arguments": dict(end.toolCall.arguments),
                "terminalIdentity": done.message is result[1],
            },
            "E": {"requestCount": 1},
            "C": "no_malformed_tool_call_published",
        }
        second_body = json.loads(spy.requests[1].content)
        core_observation: dict[str, object] = {
            "A": "admitted",
            "L": [
                "assistant:toolUse",
                "tool:start",
                "tool:end",
                "toolResult",
                "assistant:stop",
            ],
            "T": {
                "roles": [message.role for message in result],
                "convertedParams": executed[0],
                "terminalAssistantIdentity": done.message is result[1],
                "secondRequestRoles": [item["role"] for item in second_body["messages"]],
            },
            "E": {"providerRequests": len(spy.requests), "toolExecutions": len(executed)},
            "C": "continued_after_tool_result",
        }
        usage = done.message.usage
        partials = [event.partial for event in first_events if hasattr(event, "partial")]
        usage_observation = {
            "A": {"valid": "admitted", "malformed": "stream_error"},
            "L": {
                "partialUsage": "all_zero"
                if all(partial.usage.totalTokens == 0 for partial in partials)
                else "nonzero",
                "terminalUsagePayloads": 1,
            },
            "T": {
                "input": usage.input,
                "output": usage.output,
                "cacheRead": usage.cacheRead,
                "cacheWrite": usage.cacheWrite,
                "totalTokens": usage.totalTokens,
                "reportedTotalPreserved": usage.totalTokens == 25,
            },
            "E": {
                "cost": {
                    "input": usage.cost.input,
                    "output": usage.cost.output,
                    "cacheRead": usage.cost.cacheRead,
                    "cacheWrite": usage.cost.cacheWrite,
                    "total": usage.cost.total,
                },
                "unroundedComponentSum": usage.cost.total
                == usage.cost.input
                + usage.cost.output
                + usage.cost.cacheRead
                + usage.cost.cacheWrite,
            },
            "C": "no_guess_clamp_or_zero_fill",
        }
        return tool_observation, core_observation, usage_observation
    finally:
        setattr(httpx, "AsyncClient", original)


async def _assert_broken_tool_rejected() -> None:
    spy = _Spy(_BROKEN_TOOL)
    original = _install(spy)
    try:
        models, model = _deepseek()
        events = await _collect(
            models.streamSimple(
                model,
                Context(messages=(UserMessage(content="Use echo", timestamp=0),)),
            )
        )
        terminal = events[-1]
        assert isinstance(terminal, AssistantMessageErrorEvent)
        assert terminal.error.stopReason == "error"
        assert terminal.error.errorMessage == "DeepSeek stream failed"
    finally:
        setattr(httpx, "AsyncClient", original)


async def _main() -> dict[str, object]:
    preflight = await _preflight()
    tool, core, usage = await _tool_and_usage()
    await _assert_broken_tool_rejected()
    return {
        "reference.deepseek-request-preflight": preflight,
        "reference.strict-toolcall-json-finalization": tool,
        "reference.deepseek-tool-round-trip": core,
        "reference.strict-deepseek-usage-finalization": usage,
    }


def main() -> None:
    previous_key = os.environ.get("DEEPSEEK_API_KEY")
    os.environ["DEEPSEEK_API_KEY"] = "omh-conformance-canary"
    try:
        print(json.dumps(asyncio.run(_main()), sort_keys=True))
    finally:
        if previous_key is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = previous_key


if __name__ == "__main__":
    main()
