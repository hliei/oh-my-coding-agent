from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import MappingProxyType
from typing import Any, cast

import pytest

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
    AssistantMessage,
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


SECRET = "SECRET_CANARY"


def _run_tool_loop(
    tools: tuple[AgentTool, ...],
    call: ToolCall,
) -> tuple[tuple[Any, ...], list[AgentEvent], AgentContext]:
    prompt = UserMessage(content="use tool", timestamp=1)
    tool_assistant = fauxAssistantMessage(call, stopReason="toolUse")
    final_assistant = fauxAssistantMessage("done")
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    faux.setResponses((tool_assistant, final_assistant))
    models = createModels()
    models.setProvider(faux.provider)
    context = AgentContext(systemPrompt="", messages=(), tools=tools)
    events: list[AgentEvent] = []

    async def emit(event: AgentEvent) -> None:
        events.append(event)

    result = asyncio.run(
        runAgentLoop(
            (prompt,),
            context,
            AgentLoopConfig(model=model),
            emit,
            _stream_fn(models),
        )
    )
    return result, events, context


def _tool_lifecycle(events: list[AgentEvent]) -> tuple[Any, ...]:
    return tuple(
        event
        for event in events
        if event.type.startswith("tool_execution_")
        or (
            event.type in {"message_start", "message_end"}
            and isinstance(
                getattr(event, "message", None),
                ToolResultMessage,
            )
        )
    )


def test_one_validated_tool_turn_completes_with_a_later_model_turn() -> None:
    order: list[str] = []
    received: dict[str, object] = {}
    original_arguments = {"n": "2"}

    def prepare(arguments: dict[str, object]) -> dict[str, object]:
        order.append("prepare")
        assert arguments == original_arguments
        return {"count": arguments["n"]}

    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        order.append("execute")
        received["id"] = tool_call_id
        received["params"] = params
        received["signal"] = signal
        received["update_return"] = on_update(
            AgentToolResult(
                content=(TextContent(text="working"),),
                details={"step": 1},
            )
        )
        params["mutated"] = True
        return AgentToolResult(
            content=(TextContent(text="echoed 2"),),
            details={"ok": True},
        )

    tool = AgentTool(
        name="echo",
        label="echo",
        description="echo",
        parameters=ECHO_SCHEMA,
        execute=execute,
        prepareArguments=prepare,
    )
    prompt = UserMessage(content="use echo", timestamp=1)
    call = fauxToolCall(id="call-1", name="echo", arguments=dict(original_arguments))
    tool_assistant = fauxAssistantMessage(call, stopReason="toolUse")
    final_assistant = fauxAssistantMessage("done")
    contexts: list[Context] = []

    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None

    def second_response(
        context: Context,
        options: object | None,
        state: object,
        current_model: Model,
    ) -> AssistantMessage:
        del options, state, current_model
        contexts.append(context)
        return final_assistant

    faux.setResponses((tool_assistant, second_response))
    models = createModels()
    models.setProvider(faux.provider)
    context = AgentContext(systemPrompt="Be concise.", messages=(), tools=(tool,))
    messages_before = context.messages
    tools_before = context.tools
    events: list[AgentEvent] = []

    async def emit(event: AgentEvent) -> None:
        events.append(event)

    result = asyncio.run(
        runAgentLoop(
            (prompt,),
            context,
            AgentLoopConfig(model=model),
            emit,
            _stream_fn(models),
        )
    )

    assert order == ["prepare", "execute"]
    assert received["id"] == "call-1"
    assert received["params"] == {"count": 2, "mutated": True}
    assert type(received["params"]) is dict
    assert received["update_return"] is None
    assert isinstance(received["signal"], AbortSignal)
    assert not received["signal"].aborted

    assert tuple(event.type for event in events) == (
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "message_start",
        "message_update",
        "message_update",
        "message_end",
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_end",
        "message_start",
        "message_end",
        "turn_end",
        "turn_start",
        "message_start",
        "message_update",
        "message_update",
        "message_update",
        "message_end",
        "turn_end",
        "agent_end",
    )

    start = events[8]
    update = events[9]
    end = events[10]
    assert isinstance(start, AgentEvent.ToolExecutionStart)
    assert start.toolCallId == "call-1"
    assert start.toolName == "echo"
    assert dict(start.args) == original_arguments
    assert isinstance(start.args, MappingProxyType)
    assert "mutated" not in start.args
    assert isinstance(update, AgentEvent.ToolExecutionUpdate)
    assert update.toolCallId == "call-1"
    assert update.partialResult.content[0].text == "working"
    assert update.partialResult.details == MappingProxyType({"step": 1})
    assert dict(update.args) == original_arguments
    assert isinstance(end, AgentEvent.ToolExecutionEnd)
    assert end.isError is False
    assert end.result.content[0].text == "echoed 2"
    assert end.result.details == MappingProxyType({"ok": True})
    assert end.result.terminate is None
    assert not hasattr(end.result, "isError")

    tool_result = events[11]
    assert isinstance(tool_result, AgentEvent.MessageStart)
    assert isinstance(tool_result.message, ToolResultMessage)
    assert tool_result.message.toolCallId == "call-1"
    assert tool_result.message.toolName == "echo"
    assert tool_result.message.content[0].text == "echoed 2"
    assert tool_result.message.isError is False
    first_turn_end = events[13]
    assert isinstance(first_turn_end, AgentEvent.TurnEnd)
    assert first_turn_end.toolResults == (tool_result.message,)
    assert isinstance(events[-1], AgentEvent.AgentEnd)
    assert events[-1].messages is result
    assert result == (prompt, tool_assistant, tool_result.message, final_assistant)
    assert context.messages is messages_before
    assert context.tools is tools_before
    assert faux.state.callCount == 2
    assert len(contexts) == 1
    assert contexts[0].messages[-1] is tool_result.message
    assert isinstance(cast(ToolCall, tool_assistant.content[0]).arguments, MappingProxyType)


async def _unused_execute(
    tool_call_id: str,
    params: dict[str, object],
    signal: Any,
    on_update: Any,
) -> AgentToolResult:
    del tool_call_id, params, signal, on_update
    raise AssertionError("execute must not run")


def test_negative_domain_result_remains_a_tool_outcome() -> None:
    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal, on_update
        return AgentToolResult(
            content=(TextContent(text='Read path "missing.txt" was not found'),),
            details={"code": "not_found", "path": "missing.txt"},
        )

    tool = AgentTool(
        name="echo",
        label="echo",
        description="echo",
        parameters=ECHO_SCHEMA,
        execute=execute,
    )
    result, events, _context = _run_tool_loop(
        (tool,),
        fauxToolCall(id="call-1", name="echo", arguments={"count": 1}),
    )
    end = next(
        event for event in events if isinstance(event, AgentEvent.ToolExecutionEnd)
    )
    message = next(
        event.message
        for event in events
        if isinstance(event, AgentEvent.MessageStart)
        and isinstance(event.message, ToolResultMessage)
    )
    assert end.isError is False
    assert message.isError is False
    assert message.content[0].text == 'Read path "missing.txt" was not found'
    assert message.details == MappingProxyType(
        {"code": "not_found", "path": "missing.txt"}
    )
    assert result[-2] is message
    assert result[-1].role == "assistant"


def test_tool_turn_projections_agree_across_public_carriers() -> None:
    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal, on_update
        return AgentToolResult(
            content=(TextContent(text="ok"),),
            details={"ok": True},
        )

    tool = AgentTool(
        name="echo",
        label="echo",
        description="echo",
        parameters=ECHO_SCHEMA,
        execute=execute,
    )
    prompt = UserMessage(content="use echo", timestamp=1)
    call = fauxToolCall(id="call-1", name="echo", arguments={"count": 1})
    context = AgentContext(systemPrompt="", messages=(), tools=(tool,))
    messages_before = context.messages

    async def run() -> None:
        sink_faux = fauxProvider()
        stream_faux = fauxProvider()
        sink_model = sink_faux.getModel()
        stream_model = stream_faux.getModel()
        assert sink_model is not None
        assert stream_model is not None
        sink_faux.setResponses(
            (
                fauxAssistantMessage(call, stopReason="toolUse"),
                fauxAssistantMessage("done"),
            )
        )
        stream_faux.setResponses(
            (
                fauxAssistantMessage(call, stopReason="toolUse"),
                fauxAssistantMessage("done"),
            )
        )
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
        assert sink_events[-1].messages is sink_result
        assert stream_events[-1].messages is stream_result
        assert [message.role for message in sink_result] == [
            "user",
            "assistant",
            "toolResult",
            "assistant",
        ]
        assert [message.role for message in stream_result] == [
            message.role for message in sink_result
        ]
        assert context.messages is messages_before
        assert context.messages == ()

    asyncio.run(run())


def _failure_tool(
    execute: Any = _unused_execute,
    prepare: Any = None,
    name: str = "echo",
) -> AgentTool:
    return AgentTool(
        name=name,
        label=name,
        description=name,
        parameters=ECHO_SCHEMA,
        execute=execute,
        prepareArguments=prepare,
    )


def test_missing_tool_is_a_redacted_recoverable_failure() -> None:
    executed = False

    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        nonlocal executed
        executed = True
        del tool_call_id, params, signal, on_update
        return AgentToolResult(content=(), details={})

    tool = _failure_tool(execute=execute)
    _result, events, _context = _run_tool_loop(
        (tool,),
        fauxToolCall(id="call-1", name="absent", arguments={"count": 1}),
    )
    assert executed is False
    _assert_tool_failure(events, 'Tool "absent" was not found')


def test_ambiguous_tool_is_a_redacted_recoverable_failure() -> None:
    tool = _failure_tool()
    duplicate = _failure_tool()
    _result, events, _context = _run_tool_loop(
        (tool, duplicate),
        fauxToolCall(id="call-1", name="echo", arguments={"count": 1}),
    )
    _assert_tool_failure(events, 'Tool "echo" matched more than once')


def test_preparation_failure_is_a_redacted_recoverable_failure() -> None:
    def prepare(arguments: dict[str, object]) -> dict[str, object]:
        raise RuntimeError(SECRET)

    _result, events, _context = _run_tool_loop(
        (_failure_tool(prepare=prepare),),
        fauxToolCall(id="call-1", name="echo", arguments={"count": 1}),
    )
    _assert_tool_failure(events, 'Tool "echo" argument preparation failed')


def test_schema_failure_keeps_the_selected_validation_text() -> None:
    _result, events, _context = _run_tool_loop(
        (_failure_tool(),),
        fauxToolCall(
            id="call-1",
            name="echo",
            arguments={"extra": SECRET},
        ),
    )
    _assert_tool_failure(
        events,
        'Validation failed for tool "echo":\n'
        "  - #/count [required]: required property is missing\n"
        "  - #/extra [additionalProperties]: additional property is not admitted",
    )


def test_raised_execute_is_a_redacted_recoverable_failure() -> None:
    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal, on_update
        raise RuntimeError(SECRET)

    _result, events, _context = _run_tool_loop(
        (_failure_tool(execute=execute),),
        fauxToolCall(id="call-1", name="echo", arguments={"count": 1}),
    )
    _assert_tool_failure(events, 'Tool "echo" execution failed')


def test_non_awaitable_execute_is_a_redacted_recoverable_failure() -> None:
    def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal, on_update
        return AgentToolResult(content=(TextContent(text="should not publish"),), details={})

    _result, events, _context = _run_tool_loop(
        (_failure_tool(execute=execute),),
        fauxToolCall(id="call-1", name="echo", arguments={"count": 1}),
    )
    _assert_tool_failure(events, 'Tool "echo" execute must return an awaitable')


def test_invalid_update_is_unpublished_and_fails_the_tool() -> None:
    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal
        on_update(
            AgentToolResult(content=(TextContent(text="working"),), details={"step": 1})
        )
        try:
            on_update(cast(Any, SECRET))
        except (TypeError, ValueError):
            pass
        return AgentToolResult(
            content=(TextContent(text="should be discarded"),),
            details={"ok": True},
        )

    _result, events, _context = _run_tool_loop(
        (_failure_tool(execute=execute),),
        fauxToolCall(id="call-1", name="echo", arguments={"count": 1}),
    )
    updates = [
        event
        for event in events
        if isinstance(event, AgentEvent.ToolExecutionUpdate)
    ]
    assert len(updates) == 1
    assert updates[0].partialResult.content[0].text == "working"
    _assert_tool_failure(events, 'Tool "echo" produced an invalid update')


def test_invalid_final_result_is_a_redacted_recoverable_failure() -> None:
    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> Any:
        del tool_call_id, params, signal, on_update
        return SECRET

    _result, events, _context = _run_tool_loop(
        (_failure_tool(execute=execute),),
        fauxToolCall(id="call-1", name="echo", arguments={"count": 1}),
    )
    _assert_tool_failure(events, 'Tool "echo" produced an invalid result')


def _assert_tool_failure(events: list[AgentEvent], text: str) -> None:
    start = next(
        event for event in events if isinstance(event, AgentEvent.ToolExecutionStart)
    )
    end = next(
        event for event in events if isinstance(event, AgentEvent.ToolExecutionEnd)
    )
    message = next(
        event.message
        for event in events
        if isinstance(event, AgentEvent.MessageStart)
        and isinstance(event.message, ToolResultMessage)
    )
    public = "\n".join(
        [
            start.toolName,
            end.result.content[0].text,
            str(end.result.details),
            str(end.result.terminate),
            message.content[0].text,
            str(message.details),
        ]
    )
    assert start.toolCallId == "call-1"
    assert end.isError is True
    assert message.isError is True
    assert end.result.content == (TextContent(text=text),)
    assert end.result.details == MappingProxyType({})
    assert end.result.terminate is None
    assert message.content == (TextContent(text=text),)
    assert message.details == MappingProxyType({})
    assert message.toolCallId == start.toolCallId
    assert message.toolName == start.toolName
    assert SECRET not in public
    assert events[-1].type == "agent_end"
    assert sum(1 for event in events if event.type == "turn_start") == 2
    assert _tool_lifecycle(events)[0].type == "tool_execution_start"
    assert _tool_lifecycle(events)[-3].type == "tool_execution_end"


def test_accepted_update_is_delivered_before_execute_settles() -> None:
    continue_execute = asyncio.Event()
    update_seen = asyncio.Event()

    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal
        on_update(
            AgentToolResult(content=(TextContent(text="working"),), details={"step": 1})
        )
        await continue_execute.wait()
        return AgentToolResult(content=(TextContent(text="done"),), details={"ok": True})

    prompt = UserMessage(content="use echo", timestamp=1)
    call = fauxToolCall(id="call-1", name="echo", arguments={"count": 1})
    tool = AgentTool(
        name="echo",
        label="echo",
        description="echo",
        parameters=ECHO_SCHEMA,
        execute=execute,
    )
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
    events: list[AgentEvent] = []

    async def emit(event: AgentEvent) -> None:
        events.append(event)
        if isinstance(event, AgentEvent.ToolExecutionUpdate):
            update_seen.set()

    async def run() -> None:
        operation = asyncio.create_task(
            runAgentLoop(
                (prompt,),
                AgentContext(systemPrompt="", messages=(), tools=(tool,)),
                AgentLoopConfig(model=model),
                emit,
                _stream_fn(models),
            )
        )
        await update_seen.wait()
        assert any(isinstance(event, AgentEvent.ToolExecutionUpdate) for event in events)
        assert not any(isinstance(event, AgentEvent.ToolExecutionEnd) for event in events)
        continue_execute.set()
        await operation

    asyncio.run(run())


def test_sync_execute_invalid_update_is_not_an_execution_failure() -> None:
    def execute(
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

    _result, events, _context = _run_tool_loop(
        (_failure_tool(execute=execute),),
        fauxToolCall(id="call-1", name="echo", arguments={"count": 1}),
    )
    _assert_tool_failure(events, 'Tool "echo" produced an invalid update')


def test_tool_execution_events_snapshot_arguments_and_results() -> None:
    source = {"count": 1}
    start = AgentEvent.ToolExecutionStart(
        toolCallId="call-1", toolName="echo", args=source
    )
    source["count"] = 2
    assert dict(start.args) == {"count": 1}
    assert isinstance(start.args, MappingProxyType)

    result = AgentToolResult(content=(TextContent(text="ok"),), details={"ok": True})
    update = AgentEvent.ToolExecutionUpdate(
        toolCallId="call-1",
        toolName="echo",
        args=source,
        partialResult=result,
    )
    end = AgentEvent.ToolExecutionEnd(
        toolCallId="call-1",
        toolName="echo",
        result=result,
        isError=False,
    )
    assert dict(update.args) == {"count": 2}
    assert update.partialResult is result
    assert end.isError is False
    with pytest.raises(TypeError):
        AgentEvent.ToolExecutionEnd(
            toolCallId="call-1",
            toolName="echo",
            result=cast(Any, "nope"),
            isError=False,
        )
