from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
from typing import Any

from oh_my_core import (
    AgentContext,
    AgentEvent,
    AgentLoopConfig,
    AgentTool,
    AgentToolResult,
    StreamFn,
    agentLoop,
    runAgentLoop,
)
from oh_my_llm import (
    AbortSignal,
    AssistantMessageEvent,
    Context,
    Model,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    createModels,
    fauxAssistantMessage,
    fauxProvider,
    fauxToolCall,
)


ECHO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"count": {"type": "integer"}},
    "required": ("count",),
    "additionalProperties": False,
}
SECRET = "SECRET_CANARY"


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


async def _unused_execute(
    tool_call_id: str,
    params: dict[str, object],
    signal: Any,
    on_update: Any,
) -> AgentToolResult:
    del tool_call_id, params, signal, on_update
    raise AssertionError("execute must not run")


def _echo_tool(
    execute: Any = _unused_execute,
    prepare: Any = None,
) -> AgentTool:
    return AgentTool(
        name="echo",
        label="echo",
        description="echo",
        parameters=ECHO_SCHEMA,
        execute=execute,
        prepareArguments=prepare,
    )


def _tool_result_text(events: list[AgentEvent]) -> str:
    for event in events:
        if isinstance(event, AgentEvent.MessageStart) and isinstance(
            event.message, ToolResultMessage
        ):
            return event.message.content[0].text
    raise AssertionError("missing Tool Result Message")


def _end_event(events: list[AgentEvent]) -> AgentEvent:
    for event in events:
        if isinstance(event, AgentEvent.ToolExecutionEnd):
            return event
    raise AssertionError("missing ToolExecutionEnd")


def _later_model_turn(events: list[AgentEvent]) -> bool:
    return sum(1 for event in events if event.type == "turn_start") == 2


async def _run(
    tools: tuple[AgentTool, ...],
    call: ToolCall,
) -> tuple[tuple[Any, ...], list[AgentEvent], AgentContext]:
    prompt = UserMessage(content="use tool", timestamp=1)
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    faux.setResponses(
        (
            fauxAssistantMessage(call, stopReason="toolUse"),
            fauxAssistantMessage("done"),
        )
    )
    models = createModels()
    models.setProvider(faux.provider)
    context = AgentContext(systemPrompt="", messages=(), tools=tools)
    events: list[AgentEvent] = []
    result = await runAgentLoop(
        (prompt,),
        context,
        AgentLoopConfig(model=model),
        events.append,
        _stream_fn(models),
    )
    return result, events, context


async def _success() -> dict[str, object]:
    order: list[str] = []
    received: dict[str, object] = {}
    original_arguments = {"n": "2"}

    def prepare(arguments: dict[str, object]) -> dict[str, object]:
        order.append("prepare")
        return {"count": arguments["n"]}

    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        order.append("execute")
        received["params"] = dict(params)
        received["signalAborted"] = signal.aborted
        on_update(
            AgentToolResult(content=(TextContent(text="working"),), details={"step": 1})
        )
        return AgentToolResult(
            content=(TextContent(text="echoed 2"),), details={"ok": True}
        )

    tool = _echo_tool(execute=execute, prepare=prepare)
    prompt = UserMessage(content="use echo", timestamp=1)
    call = fauxToolCall(id="call-1", name="echo", arguments=dict(original_arguments))
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    faux.setResponses(
        (
            fauxAssistantMessage(call, stopReason="toolUse"),
            fauxAssistantMessage("done"),
        )
    )
    models = createModels()
    models.setProvider(faux.provider)
    context = AgentContext(systemPrompt="", messages=(), tools=(tool,))
    messages_before = context.messages
    events: list[AgentEvent] = []
    result = await runAgentLoop(
        (prompt,),
        context,
        AgentLoopConfig(model=model),
        events.append,
        _stream_fn(models),
    )
    start = next(
        event for event in events if isinstance(event, AgentEvent.ToolExecutionStart)
    )
    end = _end_event(events)
    assert isinstance(end, AgentEvent.ToolExecutionEnd)
    return {
        "order": order,
        "roles": [message.role for message in result],
        "lifecycle": [event.type for event in events],
        "startArgs": dict(start.args),
        "convertedParams": received["params"],
        "signalAborted": received["signalAborted"],
        "isError": end.isError,
        "providerCalls": faux.state.callCount,
        "contextUnchanged": context.messages is messages_before,
        "agentEndIsResult": isinstance(events[-1], AgentEvent.AgentEnd)
        and events[-1].messages is result,
        "laterModelTurn": result[-1].role == "assistant",
    }


async def _negative_outcome() -> dict[str, object]:
    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal, on_update
        return AgentToolResult(
            content=(TextContent(text='Read path "missing.txt" was not found'),),
            details={"code": "not_found"},
        )

    result, events, _context = await _run(
        (_echo_tool(execute=execute),),
        fauxToolCall(id="call-1", name="echo", arguments={"count": 1}),
    )
    end = _end_event(events)
    assert isinstance(end, AgentEvent.ToolExecutionEnd)
    message = next(
        event.message
        for event in events
        if isinstance(event, AgentEvent.MessageStart)
        and isinstance(event.message, ToolResultMessage)
    )
    return {
        "isError": end.isError,
        "messageIsError": message.isError,
        "text": message.content[0].text,
        "roles": [item.role for item in result],
    }


async def _projections() -> dict[str, object]:
    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal, on_update
        return AgentToolResult(content=(TextContent(text="ok"),), details={"ok": True})

    tool = _echo_tool(execute=execute)
    prompt = UserMessage(content="use echo", timestamp=1)
    call = fauxToolCall(id="call-1", name="echo", arguments={"count": 1})
    context = AgentContext(systemPrompt="", messages=(), tools=(tool,))
    messages_before = context.messages
    sink_faux = fauxProvider()
    stream_faux = fauxProvider()
    sink_model = sink_faux.getModel()
    stream_model = stream_faux.getModel()
    assert sink_model is not None and stream_model is not None
    responses = (
        fauxAssistantMessage(call, stopReason="toolUse"),
        fauxAssistantMessage("done"),
    )
    sink_faux.setResponses(responses)
    stream_faux.setResponses(responses)
    sink_models = createModels()
    stream_models = createModels()
    sink_models.setProvider(sink_faux.provider)
    stream_models.setProvider(stream_faux.provider)
    sink_events: list[AgentEvent] = []
    sink_result = await runAgentLoop(
        (prompt,),
        context,
        AgentLoopConfig(model=sink_model),
        sink_events.append,
        _stream_fn(sink_models),
    )
    stream = agentLoop(
        (prompt,),
        context,
        AgentLoopConfig(model=stream_model),
        _stream_fn(stream_models),
    )
    stream_result = await stream.result()
    stream_events = [event async for event in stream]
    assert isinstance(sink_events[-1], AgentEvent.AgentEnd)
    assert isinstance(stream_events[-1], AgentEvent.AgentEnd)
    return {
        "sinkAgentEndIsResult": sink_events[-1].messages is sink_result,
        "streamAgentEndIsResult": stream_events[-1].messages is stream_result,
        "sinkRoles": [message.role for message in sink_result],
        "streamRoles": [message.role for message in stream_result],
        "contextUnchanged": context.messages is messages_before,
    }


async def _failures() -> dict[str, object]:
    preflight_executed = False

    async def forbidden(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        nonlocal preflight_executed
        preflight_executed = True
        del tool_call_id, params, signal, on_update
        return AgentToolResult(content=(), details={})

    async def boom(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal, on_update
        raise RuntimeError(SECRET)

    def not_awaitable(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal, on_update
        return AgentToolResult(content=(), details={})

    async def bad_update(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal
        try:
            on_update(SECRET)
        except (TypeError, ValueError):
            pass
        return AgentToolResult(content=(TextContent(text="discard"),), details={})

    async def bad_final(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> Any:
        del tool_call_id, params, signal, on_update
        return SECRET

    def prepare(arguments: dict[str, object]) -> dict[str, object]:
        raise RuntimeError(SECRET)

    call = fauxToolCall(id="call-1", name="echo", arguments={"count": 1})
    missing_events = (
        await _run(
            (_echo_tool(execute=forbidden),),
            fauxToolCall(id="call-1", name="absent", arguments={"count": 1}),
        )
    )[1]
    ambiguous_events = (await _run((_echo_tool(), _echo_tool()), call))[1]
    preparation_events = (
        await _run((_echo_tool(prepare=prepare),), call)
    )[1]
    schema_events = (
        await _run(
            (_echo_tool(),),
            fauxToolCall(id="call-1", name="echo", arguments={"extra": SECRET}),
        )
    )[1]
    raised_events = (await _run((_echo_tool(execute=boom),), call))[1]
    non_awaitable_events = (
        await _run((_echo_tool(execute=not_awaitable),), call)
    )[1]
    invalid_update_events = (
        await _run((_echo_tool(execute=bad_update),), call)
    )[1]
    invalid_result_events = (
        await _run((_echo_tool(execute=bad_final),), call)
    )[1]
    public = "\n".join(
        _tool_result_text(events)
        for events in (
            missing_events,
            ambiguous_events,
            preparation_events,
            schema_events,
            raised_events,
            non_awaitable_events,
            invalid_update_events,
            invalid_result_events,
        )
    )
    return {
        "templates": {
            "missing": _tool_result_text(missing_events),
            "ambiguous": _tool_result_text(ambiguous_events),
            "preparation": _tool_result_text(preparation_events),
            "schema": _tool_result_text(schema_events),
            "raised": _tool_result_text(raised_events),
            "nonAwaitable": _tool_result_text(non_awaitable_events),
            "invalidUpdate": _tool_result_text(invalid_update_events),
            "invalidResult": _tool_result_text(invalid_result_events),
        },
        "allIsError": all(
            isinstance(event, AgentEvent.ToolExecutionEnd) and event.isError
            for events in (
                missing_events,
                ambiguous_events,
                preparation_events,
                schema_events,
                raised_events,
                non_awaitable_events,
                invalid_update_events,
                invalid_result_events,
            )
            for event in events
            if isinstance(event, AgentEvent.ToolExecutionEnd)
        ),
        "secretLeaked": SECRET in public,
        "preflightExecuted": preflight_executed,
        "laterModelTurn": all(
            _later_model_turn(events)
            for events in (
                missing_events,
                ambiguous_events,
                preparation_events,
                schema_events,
                raised_events,
                non_awaitable_events,
                invalid_update_events,
                invalid_result_events,
            )
        ),
        "emptyDetails": all(
            isinstance(event, AgentEvent.ToolExecutionEnd) and event.result.details == {}
            for events in (
                missing_events,
                ambiguous_events,
                preparation_events,
                schema_events,
                raised_events,
                non_awaitable_events,
                invalid_update_events,
                invalid_result_events,
            )
            for event in events
            if isinstance(event, AgentEvent.ToolExecutionEnd)
        ),
    }


def main() -> None:
    success = asyncio.run(_success())
    negative = asyncio.run(_negative_outcome())
    projections = asyncio.run(_projections())
    failures = asyncio.run(_failures())
    actual = {
        "reference.one-validated-tool-turn": {
            "A": "admitted",
            "L": success["lifecycle"],
            "T": {
                "roles": success["roles"],
                "startArgs": success["startArgs"],
                "convertedParams": success["convertedParams"],
                "order": success["order"],
                "isError": success["isError"],
            },
            "E": {"providerCalls": success["providerCalls"]},
            "C": {
                "laterModelTurn": success["laterModelTurn"],
                "signalUnaborted": success["signalAborted"] is False,
            },
        },
        "reference.tool-outcome-versus-failure": {
            "A": "admitted",
            "L": "start_then_end",
            "T": {
                "isError": negative["isError"],
                "messageIsError": negative["messageIsError"],
                "negativeDomainText": negative["text"],
            },
            "E": "callable_returned",
            "C": {"roles": negative["roles"]},
        },
        "reference.redacted-tool-runtime-errors": {
            "A": "recoverable_tool_failure",
            "L": "start_then_end_then_result",
            "T": {
                "templates": failures["templates"],
                "allIsError": failures["allIsError"],
                "emptyDetails": failures["emptyDetails"],
                "secretLeaked": failures["secretLeaked"],
            },
            "E": {"preflightExecuted": failures["preflightExecuted"]},
            "C": {"laterModelTurn": failures["laterModelTurn"]},
        },
        "reference.tool-turn-projection-identity": {
            "A": "admitted",
            "L": {
                "sinkAgentEndIsResult": projections["sinkAgentEndIsResult"],
                "streamAgentEndIsResult": projections["streamAgentEndIsResult"],
            },
            "T": {
                "carrier": "tuple",
                "sinkRoles": projections["sinkRoles"],
                "streamRoles": projections["streamRoles"],
            },
            "E": {"contextUnchanged": projections["contextUnchanged"]},
            "C": "stream_and_sink_agree",
        },
    }
    print(json.dumps(actual, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
