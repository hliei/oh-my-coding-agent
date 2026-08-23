from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
from typing import Any, Literal

from oh_my_core import (
    AgentContext,
    AgentEvent,
    AgentLoopConfig,
    AgentTool,
    AgentToolResult,
    StreamFn,
    ToolExecutionMode,
    runAgentLoop,
)
from oh_my_llm import (
    AbortSignal,
    AssistantMessage,
    AssistantMessageEvent,
    Context,
    Model,
    TextContent,
    ToolResultMessage,
    UserMessage,
    createModels,
    fauxAssistantMessage,
    fauxProvider,
    fauxToolCall,
)


TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ("value",),
    "additionalProperties": False,
}


def _stream_fn(models: Any) -> StreamFn:
    async def stream_fn(
        model: Model,
        context: Context,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del signal
        async for event in models.streamSimple(model, context, options):
            yield event

    return stream_fn


def _tool(
    name: str,
    execute: Any,
    *,
    prepare: Any = None,
    execution_mode: Literal["sequential", "parallel"] | None = None,
) -> AgentTool:
    return AgentTool(
        name=name,
        label=name,
        description=name,
        parameters=TOOL_SCHEMA,
        execute=execute,
        prepareArguments=prepare,
        executionMode=execution_mode,
    )


async def _run(
    responses: tuple[AssistantMessage, ...],
    tools: tuple[AgentTool, ...],
    *,
    tool_execution: ToolExecutionMode = "parallel",
) -> tuple[tuple[Any, ...], list[AgentEvent], int]:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    faux.setResponses(responses)
    models = createModels()
    models.setProvider(faux.provider)
    events: list[AgentEvent] = []
    result = await runAgentLoop(
        (UserMessage(content="use tools", timestamp=1),),
        AgentContext(systemPrompt="", messages=(), tools=tools),
        AgentLoopConfig(model=model, toolExecution=tool_execution),
        events.append,
        _stream_fn(models),
    )
    return result, events, faux.state.callCount


def _tool_results(events: list[AgentEvent]) -> list[ToolResultMessage]:
    return [
        event.message
        for event in events
        if isinstance(event, AgentEvent.MessageStart)
        and isinstance(event.message, ToolResultMessage)
    ]


async def _mixed_mode(mode: ToolExecutionMode) -> dict[str, object]:
    prepared: list[int] = []
    executed: list[str] = []

    def prepare(arguments: dict[str, object]) -> dict[str, object]:
        value = arguments["value"]
        assert type(value) is int
        prepared.append(value)
        return arguments

    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del params, signal, on_update
        executed.append(tool_call_id)
        return AgentToolResult(
            content=(TextContent(text=f"ok:{tool_call_id}"),), details={}
        )

    calls = (
        fauxToolCall(id="duplicate", name="work", arguments={"value": 1}),
        fauxToolCall(id="unique", name="work", arguments={"value": 2}),
        fauxToolCall(id="duplicate", name="work", arguments={"value": 3}),
        fauxToolCall(id="", name="work", arguments={"value": 4}),
    )
    _result, events, provider_calls = await _run(
        (
            fauxAssistantMessage(calls, stopReason="toolUse"),
            fauxAssistantMessage("done"),
        ),
        (_tool("work", execute, prepare=prepare),),
        tool_execution=mode,
    )
    results = _tool_results(events)
    return {
        "preparedValues": prepared,
        "executedIds": executed,
        "resultIds": [result.toolCallId for result in results],
        "resultErrors": [result.isError for result in results],
        "resultTexts": [result.content[0].text for result in results],
        "providerCalls": provider_calls,
    }


async def _batch_control() -> dict[str, object]:
    truncated_effects = 0

    async def forbidden(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        nonlocal truncated_effects
        del tool_call_id, params, signal, on_update
        truncated_effects += 1
        return AgentToolResult(content=(), details={})

    truncated_calls = (
        fauxToolCall(id="first", name="work", arguments={"value": 1}),
        fauxToolCall(id="second", name="work", arguments={"value": 2}),
    )
    _truncated_result, truncated_events, truncated_provider_calls = await _run(
        (
            fauxAssistantMessage(truncated_calls, stopReason="length"),
            fauxAssistantMessage("reissued"),
        ),
        (_tool("work", forbidden),),
    )

    async def terminate(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del params, signal, on_update
        return AgentToolResult(
            content=(TextContent(text=f"done:{tool_call_id}"),),
            details={},
            terminate=True,
        )

    terminating_calls = (
        fauxToolCall(id="first", name="first", arguments={"value": 1}),
        fauxToolCall(id="second", name="second", arguments={"value": 2}),
    )
    terminating_result, terminating_events, terminating_provider_calls = await _run(
        (fauxAssistantMessage(terminating_calls, stopReason="toolUse"),),
        (_tool("first", terminate), _tool("second", terminate)),
    )
    truncated_results = _tool_results(truncated_events)
    terminating_results = _tool_results(terminating_events)
    return {
        "truncated": {
            "toolEffects": truncated_effects,
            "resultIds": [result.toolCallId for result in truncated_results],
            "allErrors": all(result.isError for result in truncated_results),
            "providerCalls": truncated_provider_calls,
        },
        "unanimousTermination": {
            "providerCalls": terminating_provider_calls,
            "resultIds": [result.toolCallId for result in terminating_results],
            "roles": [message.role for message in terminating_result],
            "turnEndedBeforeAgent": isinstance(
                terminating_events[-2], AgentEvent.TurnEnd
            )
            and isinstance(terminating_events[-1], AgentEvent.AgentEnd),
        },
    }


async def main() -> None:
    print(
        json.dumps(
            {
                "reference.strict-tool-call-correlation": {
                    "parallel": await _mixed_mode("parallel"),
                    "sequential": await _mixed_mode("sequential"),
                },
                "reference.deterministic-tool-batch-control": await _batch_control(),
            },
            sort_keys=True,
        )
    )


asyncio.run(main())
