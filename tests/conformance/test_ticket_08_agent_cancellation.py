from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
import json
from pathlib import Path
from typing import Any

from oh_my_core import (
    Agent,
    AgentContext,
    AgentEvent,
    AgentLoopConfig,
    AgentOptions,
    AgentState,
    AgentTool,
    AgentToolResult,
    runAgentLoop,
)
from oh_my_llm import (
    AbortSignal,
    AssistantMessage,
    AssistantMessageDoneEvent,
    AssistantMessageEvent,
    AssistantMessageTextDeltaEvent,
    LifecycleError,
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
    "properties": {},
    "additionalProperties": False,
}
ROOT = Path(__file__).parents[2]


def test_model_abort_preserves_latest_assistant_and_waits_for_cleanup() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    faux.setResponses((fauxAssistantMessage("confirmed"),))
    models = createModels()
    models.setProvider(faux.provider)
    model_blocked = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleaned = asyncio.Event()
    events: list[AgentEvent] = []

    async def blocked_stream(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del options, signal
        async for event in models.streamSimple(model, context, None):
            yield event
            if isinstance(event, AssistantMessageTextDeltaEvent):
                model_blocked.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup_started.set()
                    await allow_cleanup.wait()
                    cleaned.set()

    agent = Agent(
        AgentOptions(initialState=AgentState(model=model), streamFn=blocked_stream)
    )
    agent.subscribe(lambda event, signal: events.append(event))

    async def run() -> None:
        operation = asyncio.create_task(agent.prompt("go"))
        await model_blocked.wait()
        captured_signal = agent.signal
        assert captured_signal is not None
        agent.abort()
        assert captured_signal.aborted
        await cleanup_started.wait()
        assert operation.done() is False
        assert agent.state.isStreaming
        allow_cleanup.set()
        await operation
        assert cleaned.is_set()
        assert agent.signal is None
        assert agent.state.isStreaming is False
        assistant = agent.state.messages[-1]
        assert type(assistant) is AssistantMessage
        assert assistant.content[0].text == "confirmed"
        assert assistant.stopReason == "aborted"
        assert assistant.errorMessage == "Operation aborted"
        assert agent.state.errorMessage == "Operation aborted"
        assert [event.type for event in events].count("agent_end") == 1
        assert events[-1].type == "agent_end"

    asyncio.run(run())


def test_repeated_abort_does_not_interrupt_owned_model_cleanup() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    model_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_interrupted = False
    cleaned = False

    async def blocked_stream(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        nonlocal cleaned, cleanup_interrupted
        del model, context, options, signal
        model_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            try:
                await allow_cleanup.wait()
            except asyncio.CancelledError:
                cleanup_interrupted = True
                raise
            cleaned = True
        if False:
            yield

    agent = Agent(
        AgentOptions(initialState=AgentState(model=model), streamFn=blocked_stream)
    )

    async def run() -> None:
        operation = asyncio.create_task(agent.prompt("go"))
        await model_started.wait()
        agent.abort()
        await cleanup_started.wait()
        agent.abort()
        allow_cleanup.set()
        await operation
        assert cleanup_interrupted is False
        assert cleaned
        terminal = agent.state.messages[-1]
        assert type(terminal) is AssistantMessage
        assert terminal.stopReason == "aborted"
        assert agent.state.isStreaming is False

    asyncio.run(run())


def test_abort_after_model_done_preserves_the_final_cumulative_value() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    faux.setResponses((fauxAssistantMessage("confirmed"),))
    models = createModels()
    models.setProvider(faux.provider)
    eof_wait_started = asyncio.Event()
    cleaned = False
    final_usage = None

    async def blocked_after_done(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        nonlocal cleaned, final_usage
        del options, signal
        try:
            async for event in models.streamSimple(model, context, None):
                if isinstance(event, AssistantMessageDoneEvent):
                    final_usage = replace(
                        event.message.usage,
                        output=15,
                        totalTokens=15,
                    )
                    event = AssistantMessageDoneEvent(
                        reason=event.reason,
                        message=replace(event.message, usage=final_usage),
                    )
                    yield event
                    eof_wait_started.set()
                    await asyncio.Event().wait()
                else:
                    yield event
        finally:
            cleaned = True

    agent = Agent(
        AgentOptions(initialState=AgentState(model=model), streamFn=blocked_after_done)
    )

    async def run() -> None:
        operation = asyncio.create_task(agent.prompt("go"))
        await eof_wait_started.wait()
        agent.abort()
        await operation
        assert cleaned
        assert final_usage is not None
        terminal = agent.state.messages[-1]
        assert type(terminal) is AssistantMessage
        assert terminal.content == (TextContent(text="confirmed"),)
        assert terminal.usage == final_usage
        assert terminal.stopReason == "aborted"

    asyncio.run(run())


def test_operation_task_cancellation_settles_the_same_run_before_reraising() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    faux.setResponses((fauxAssistantMessage("partial"),))
    models = createModels()
    models.setProvider(faux.provider)
    model_blocked = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()

    async def blocked_stream(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del options, signal
        async for event in models.streamSimple(model, context, None):
            yield event
            if isinstance(event, AssistantMessageTextDeltaEvent):
                model_blocked.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup_started.set()
                    await allow_cleanup.wait()

    agent = Agent(
        AgentOptions(initialState=AgentState(model=model), streamFn=blocked_stream)
    )

    async def run() -> None:
        operation = asyncio.create_task(agent.prompt("cancel me"))
        await model_blocked.wait()
        captured_signal = agent.signal
        operation.cancel()
        await cleanup_started.wait()
        assert operation.done() is False
        assert captured_signal is not None and captured_signal.aborted
        allow_cleanup.set()
        try:
            await operation
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("operation cancellation must be re-raised")
        assert agent.state.isStreaming is False
        assert agent.signal is None
        terminal = agent.state.messages[-1]
        assert type(terminal) is AssistantMessage
        assert type(terminal.content[0]) is TextContent
        assert terminal.content[0].text == "partial"
        assert terminal.stopReason == "aborted"

    asyncio.run(run())


def test_listener_failure_attempts_the_snapshot_and_stops_later_effects() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None

    async def never_stream(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del model, context, options, signal
        if False:
            yield

    agent = Agent(
        AgentOptions(
            initialState=AgentState(model=model),
            streamFn=never_stream,
        )
    )
    first = RuntimeError("first listener")
    second = RuntimeError("second listener")
    attempted: list[str] = []
    event_types: list[str] = []

    def fail_first(event: AgentEvent, signal: AbortSignal) -> None:
        del signal
        attempted.append("first")
        event_types.append(event.type)
        raise first

    async def fail_second(event: AgentEvent, signal: AbortSignal) -> None:
        del event, signal
        attempted.append("second")
        raise second

    def final_member(event: AgentEvent, signal: AbortSignal) -> None:
        del event, signal
        attempted.append("third")

    agent.subscribe(fail_first)
    agent.subscribe(fail_second)
    agent.subscribe(final_member)

    async def run() -> None:
        try:
            await agent.prompt("go")
        except LifecycleError as error:
            assert error.code == "listener"
            assert error.causes == (first, second)
        else:
            raise AssertionError("listener failure must fail closed")
        assert attempted == ["first", "second", "third"]
        assert event_types == ["agent_start"]
        assert faux.state.callCount == 0
        assert agent.state.isStreaming is False
        assert agent.signal is None
        assert agent.state.messages == ()

    asyncio.run(run())


def test_model_listener_causes_precede_model_cleanup_causes() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    faux.setResponses((fauxAssistantMessage("partial"),))
    models = createModels()
    models.setProvider(faux.provider)
    listener_error = RuntimeError("model listener")
    cleanup_error = RuntimeError("model cleanup")

    async def stream_fn(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del options, signal
        try:
            async for event in models.streamSimple(model, context, None):
                yield event
        finally:
            raise cleanup_error

    agent = Agent(
        AgentOptions(initialState=AgentState(model=model), streamFn=stream_fn)
    )

    def listener(event: AgentEvent, signal: AbortSignal) -> None:
        del signal
        if isinstance(event, AgentEvent.MessageUpdate):
            raise listener_error

    agent.subscribe(listener)

    async def run() -> None:
        try:
            await agent.prompt("go")
        except LifecycleError as error:
            assert error.code == "listener"
            assert error.causes == (listener_error, cleanup_error)
        else:
            raise AssertionError("listener plus cleanup must fail closed")
        assert agent.state.isStreaming is False
        assert agent.state.errorMessage is None
        assert all(
            not isinstance(message, AssistantMessage)
            for message in agent.state.messages
        )

    asyncio.run(run())


def test_abort_during_tool_preflight_correlates_every_call_without_later_effects() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    calls = tuple(
        fauxToolCall(id=call_id, name="work", arguments={})
        for call_id in ("first", "second", "third")
    )
    faux.setResponses((fauxAssistantMessage(calls, stopReason="toolUse"),))
    models = createModels()
    models.setProvider(faux.provider)
    effects: list[str] = []
    events: list[AgentEvent] = []

    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del params, signal, on_update
        effects.append(tool_call_id)
        return AgentToolResult(content=(), details={})

    async def stream_fn(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del options, signal
        async for event in models.streamSimple(model, context, None):
            yield event

    tool = AgentTool(
        name="work",
        label="work",
        description="work",
        parameters=TOOL_SCHEMA,
        execute=execute,
    )
    agent = Agent(
        AgentOptions(
            initialState=AgentState(model=model, tools=(tool,)),
            streamFn=stream_fn,
        )
    )

    def abort_first_start(event: AgentEvent, signal: AbortSignal) -> None:
        del signal
        events.append(event)
        if (
            isinstance(event, AgentEvent.ToolExecutionStart)
            and event.toolCallId == "first"
        ):
            agent.abort()

    agent.subscribe(abort_first_start)

    async def run() -> None:
        await agent.prompt("go")
        starts = [
            event.toolCallId
            for event in events
            if isinstance(event, AgentEvent.ToolExecutionStart)
        ]
        ends = [
            event.toolCallId
            for event in events
            if isinstance(event, AgentEvent.ToolExecutionEnd)
        ]
        results = [
            message
            for message in agent.state.messages
            if isinstance(message, ToolResultMessage)
        ]
        assert starts == ["first"]
        assert ends == ["first"]
        assert effects == []
        assert [result.toolCallId for result in results] == [
            "first",
            "second",
            "third",
        ]
        assert all(result.isError for result in results)
        assert [result.content[0].text for result in results] == [
            'Tool "work" execution was cancelled',
            'Tool "work" execution was cancelled',
            'Tool "work" execution was cancelled',
        ]
        assert [message.role for message in agent.state.messages] == [
            "user",
            "assistant",
            "toolResult",
            "toolResult",
            "toolResult",
            "assistant",
        ]
        terminal = agent.state.messages[-1]
        assert type(terminal) is AssistantMessage
        assert terminal.stopReason == "aborted"
        assert events[-1].type == "agent_end"

    asyncio.run(run())


def test_listener_abort_before_model_start_skips_the_model_effect() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    effects = 0
    event_types: list[str] = []

    async def forbidden_stream(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        nonlocal effects
        del model, context, options, signal
        effects += 1
        raise AssertionError("Model effect began after cancellation cutoff")
        if False:
            yield

    agent = Agent(
        AgentOptions(initialState=AgentState(model=model), streamFn=forbidden_stream)
    )

    def abort_turn_start(event: AgentEvent, signal: AbortSignal) -> None:
        del signal
        event_types.append(event.type)
        if isinstance(event, AgentEvent.TurnStart):
            agent.abort()

    agent.subscribe(abort_turn_start)

    async def run() -> None:
        await agent.prompt("go")
        assert effects == 0
        assert [message.role for message in agent.state.messages] == [
            "user",
            "assistant",
        ]
        terminal = agent.state.messages[-1]
        assert type(terminal) is AssistantMessage
        assert terminal.stopReason == "aborted"
        assert event_types[-3:] == ["message_end", "turn_end", "agent_end"]

    asyncio.run(run())


def test_message_listener_abort_does_not_resume_the_model_after_cutoff() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    faux.setResponses((fauxAssistantMessage("unused"),))
    models = createModels()
    models.setProvider(faux.provider)
    resumed_after_start = False
    cleaned = False

    async def observed_stream(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        nonlocal cleaned, resumed_after_start
        del options, signal
        first = True
        try:
            async for event in models.streamSimple(model, context, None):
                yield event
                if first:
                    resumed_after_start = True
                    first = False
        finally:
            cleaned = True

    agent = Agent(
        AgentOptions(initialState=AgentState(model=model), streamFn=observed_stream)
    )

    def abort_message_start(event: AgentEvent, signal: AbortSignal) -> None:
        del signal
        if isinstance(event, AgentEvent.MessageStart) and isinstance(
            event.message, AssistantMessage
        ):
            agent.abort()

    agent.subscribe(abort_message_start)

    async def run() -> None:
        await agent.prompt("go")
        assert resumed_after_start is False
        assert cleaned
        terminal = agent.state.messages[-1]
        assert type(terminal) is AssistantMessage
        assert terminal.stopReason == "aborted"
        assert faux.state.callCount == 1

    asyncio.run(run())


def test_external_abort_during_listener_settles_snapshot_then_aborts_normally() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    listener_started = asyncio.Event()
    listener_cleanup = asyncio.Event()
    allow_cleanup = asyncio.Event()
    later_attempted = False
    effects = 0

    async def forbidden_stream(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        nonlocal effects
        del model, context, options, signal
        effects += 1
        if False:
            yield

    agent = Agent(
        AgentOptions(initialState=AgentState(model=model), streamFn=forbidden_stream)
    )

    async def blocked_listener(event: AgentEvent, signal: AbortSignal) -> None:
        del signal
        if isinstance(event, AgentEvent.AgentStart):
            listener_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                listener_cleanup.set()
                await allow_cleanup.wait()

    def later_listener(event: AgentEvent, signal: AbortSignal) -> None:
        nonlocal later_attempted
        del signal
        if isinstance(event, AgentEvent.AgentStart):
            later_attempted = True

    agent.subscribe(blocked_listener)
    agent.subscribe(later_listener)

    async def run() -> None:
        operation = asyncio.create_task(agent.prompt("go"))
        await listener_started.wait()
        captured_signal = agent.signal
        agent.abort()
        await listener_cleanup.wait()
        assert captured_signal is not None and captured_signal.aborted
        assert operation.done() is False
        allow_cleanup.set()
        await operation
        assert later_attempted
        assert effects == 0
        terminal = agent.state.messages[-1]
        assert type(terminal) is AssistantMessage
        assert terminal.stopReason == "aborted"
        assert agent.state.isStreaming is False

    asyncio.run(run())


def test_listener_abort_at_turn_end_adds_one_aborted_tail() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    completed = fauxAssistantMessage("completed")
    faux.setResponses((completed,))
    models = createModels()
    models.setProvider(faux.provider)
    event_types: list[str] = []

    async def stream_fn(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del options, signal
        async for event in models.streamSimple(model, context, None):
            yield event

    agent = Agent(
        AgentOptions(initialState=AgentState(model=model), streamFn=stream_fn)
    )

    def abort_turn_end(event: AgentEvent, signal: AbortSignal) -> None:
        del signal
        event_types.append(event.type)
        if isinstance(event, AgentEvent.TurnEnd):
            agent.abort()

    agent.subscribe(abort_turn_end)

    async def run() -> None:
        await agent.prompt("go")
        assistants = [
            message
            for message in agent.state.messages
            if isinstance(message, AssistantMessage)
        ]
        assert assistants[0] == completed
        assert [message.stopReason for message in assistants] == ["stop", "aborted"]
        assert event_types.count("agent_end") == 1
        assert event_types[-3:] == ["message_start", "message_end", "agent_end"]
        assert faux.state.callCount == 1

    asyncio.run(run())


def test_listener_failure_cancels_and_joins_parallel_tool_cleanup() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    calls = (
        fauxToolCall(id="updating", name="updating", arguments={}),
        fauxToolCall(id="blocked", name="blocked", arguments={}),
    )
    faux.setResponses((fauxAssistantMessage(calls, stopReason="toolUse"),))
    models = createModels()
    models.setProvider(faux.provider)
    failure_seen = asyncio.Event()
    blocked_started = asyncio.Event()
    release_normally = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleaned = asyncio.Event()
    callback_error = RuntimeError("listener canary")
    cleanup_error = RuntimeError("cleanup canary")
    events_after_failure: list[str] = []

    async def updating(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal
        on_update(AgentToolResult(content=(TextContent(text="update"),), details={}))
        return AgentToolResult(content=(), details={})

    async def blocked(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal, on_update
        blocked_started.set()
        try:
            await release_normally.wait()
            return AgentToolResult(content=(), details={})
        finally:
            cleanup_started.set()
            await allow_cleanup.wait()
            cleaned.set()
            raise cleanup_error

    async def stream_fn(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del options, signal
        async for event in models.streamSimple(model, context, None):
            yield event

    tools = tuple(
        AgentTool(
            name=name,
            label=name,
            description=name,
            parameters=TOOL_SCHEMA,
            execute=execute,
        )
        for name, execute in (("updating", updating), ("blocked", blocked))
    )
    agent = Agent(
        AgentOptions(
            initialState=AgentState(model=model, tools=tools),
            streamFn=stream_fn,
        )
    )

    def listener(event: AgentEvent, signal: AbortSignal) -> None:
        del signal
        if failure_seen.is_set():
            events_after_failure.append(event.type)
        if isinstance(event, AgentEvent.ToolExecutionUpdate):
            failure_seen.set()
            raise callback_error

    agent.subscribe(listener)

    async def run() -> None:
        operation = asyncio.create_task(agent.prompt("go"))
        await blocked_started.wait()
        captured_signal = agent.signal
        assert captured_signal is not None
        await failure_seen.wait()
        await cleanup_started.wait()
        try:
            assert captured_signal.aborted
            assert operation.done() is False
            assert agent.state.isStreaming
        finally:
            release_normally.set()
            allow_cleanup.set()
            await cleaned.wait()
            try:
                await operation
            except LifecycleError:
                pass
        try:
            await operation
        except LifecycleError as error:
            assert error.code == "listener"
            assert error.causes == (callback_error, cleanup_error)
        else:
            raise AssertionError("listener failure must be retained")
        assert events_after_failure == []
        assert agent.state.isStreaming is False
        assert agent.signal is None

    asyncio.run(run())


def test_update_listener_failure_interrupts_the_still_running_tool() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    call = fauxToolCall(id="blocked", name="blocked", arguments={})
    faux.setResponses((fauxAssistantMessage(call, stopReason="toolUse"),))
    models = createModels()
    models.setProvider(faux.provider)
    update_seen = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    callback_error = RuntimeError("update listener")
    event_types: list[str] = []

    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal
        on_update(AgentToolResult(content=(), details={}))
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await allow_cleanup.wait()
        raise AssertionError("listener cutoff must cancel Tool execution")

    async def stream_fn(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del options, signal
        async for event in models.streamSimple(model, context, None):
            yield event

    tool = AgentTool(
        name="blocked",
        label="blocked",
        description="blocked",
        parameters=TOOL_SCHEMA,
        execute=execute,
    )
    agent = Agent(
        AgentOptions(
            initialState=AgentState(model=model, tools=(tool,)),
            streamFn=stream_fn,
            toolExecution="sequential",
        )
    )

    def listener(event: AgentEvent, signal: AbortSignal) -> None:
        del signal
        event_types.append(event.type)
        if isinstance(event, AgentEvent.ToolExecutionUpdate):
            update_seen.set()
            raise callback_error

    agent.subscribe(listener)

    async def run() -> None:
        operation = asyncio.create_task(agent.prompt("go"))
        await update_seen.wait()
        captured_signal = agent.signal
        await cleanup_started.wait()
        assert captured_signal is not None and captured_signal.aborted
        assert operation.done() is False
        assert agent.state.isStreaming
        allow_cleanup.set()
        try:
            await operation
        except LifecycleError as error:
            assert error.code == "listener"
            assert error.causes == (callback_error,)
        else:
            raise AssertionError("listener failure must reach the operation")
        assert agent.state.isStreaming is False
        assert agent.state.pendingToolCalls == frozenset()
        assert agent.signal is None
        assert "agent_end" not in event_types

    asyncio.run(run())


def test_event_sink_failure_has_no_partial_result_or_synthetic_agent_end() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    call = fauxToolCall(id="blocked", name="blocked", arguments={})
    faux.setResponses((fauxAssistantMessage(call, stopReason="toolUse"),))
    models = createModels()
    models.setProvider(faux.provider)
    update_seen = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    sink_error = RuntimeError("sink canary")
    event_types: list[str] = []
    captured_signal: AbortSignal | None = None

    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        nonlocal captured_signal
        del tool_call_id, params
        captured_signal = signal
        on_update(AgentToolResult(content=(), details={}))
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await allow_cleanup.wait()
        raise AssertionError("sink cutoff must cancel Tool execution")

    async def stream_fn(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del options, signal
        async for event in models.streamSimple(model, context, None):
            yield event

    tool = AgentTool(
        name="blocked",
        label="blocked",
        description="blocked",
        parameters=TOOL_SCHEMA,
        execute=execute,
    )

    def sink(event: AgentEvent) -> None:
        event_types.append(event.type)
        if isinstance(event, AgentEvent.ToolExecutionUpdate):
            update_seen.set()
            raise sink_error

    async def run() -> None:
        operation = asyncio.create_task(
            runAgentLoop(
                (UserMessage(content="go", timestamp=1),),
                AgentContext(systemPrompt="", messages=(), tools=(tool,)),
                AgentLoopConfig(model=model, toolExecution="sequential"),
                sink,
                stream_fn,
            )
        )
        await update_seen.wait()
        await cleanup_started.wait()
        assert captured_signal is not None and captured_signal.aborted
        assert operation.done() is False
        allow_cleanup.set()
        try:
            await operation
        except LifecycleError as error:
            assert error.code == "event_sink"
            assert error.causes == (sink_error,)
        else:
            raise AssertionError("event-sink failure cannot return a result tuple")
        assert "agent_end" not in event_types

    asyncio.run(run())


def test_tool_start_sink_failure_uses_the_typed_lifecycle_carrier() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    call = fauxToolCall(id="never", name="never", arguments={})
    faux.setResponses((fauxAssistantMessage(call, stopReason="toolUse"),))
    models = createModels()
    models.setProvider(faux.provider)
    sink_error = RuntimeError("tool-start sink canary")
    event_types: list[str] = []
    effects = 0

    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        nonlocal effects
        del tool_call_id, params, signal, on_update
        effects += 1
        return AgentToolResult(content=(), details={})

    async def stream_fn(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del options, signal
        async for event in models.streamSimple(model, context, None):
            yield event

    tool = AgentTool(
        name="never",
        label="never",
        description="never",
        parameters=TOOL_SCHEMA,
        execute=execute,
    )

    def sink(event: AgentEvent) -> None:
        event_types.append(event.type)
        if isinstance(event, AgentEvent.ToolExecutionStart):
            raise sink_error

    async def run() -> None:
        try:
            await runAgentLoop(
                (UserMessage(content="go", timestamp=1),),
                AgentContext(systemPrompt="", messages=(), tools=(tool,)),
                AgentLoopConfig(model=model),
                sink,
                stream_fn,
            )
        except LifecycleError as error:
            assert error.code == "event_sink"
            assert error.causes == (sink_error,)
        else:
            raise AssertionError("Tool-start sink failure must use LifecycleError")
        assert effects == 0
        assert event_types.count("tool_execution_start") == 1
        assert "agent_end" not in event_types

    asyncio.run(run())


def test_operation_cancel_during_tool_start_still_emits_the_matching_end() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    call = fauxToolCall(id="paired", name="paired", arguments={})
    faux.setResponses((fauxAssistantMessage(call, stopReason="toolUse"),))
    models = createModels()
    models.setProvider(faux.provider)
    start_seen = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    event_types: list[str] = []
    effects = 0

    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        nonlocal effects
        del tool_call_id, params, signal, on_update
        effects += 1
        return AgentToolResult(content=(), details={})

    async def stream_fn(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del options, signal
        async for event in models.streamSimple(model, context, None):
            yield event

    tool = AgentTool(
        name="paired",
        label="paired",
        description="paired",
        parameters=TOOL_SCHEMA,
        execute=execute,
    )

    async def sink(event: AgentEvent) -> None:
        event_types.append(event.type)
        if isinstance(event, AgentEvent.ToolExecutionStart):
            start_seen.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await allow_cleanup.wait()

    async def run() -> None:
        operation = asyncio.create_task(
            runAgentLoop(
                (UserMessage(content="go", timestamp=1),),
                AgentContext(systemPrompt="", messages=(), tools=(tool,)),
                AgentLoopConfig(model=model),
                sink,
                stream_fn,
            )
        )
        await start_seen.wait()
        operation.cancel()
        await cleanup_started.wait()
        assert operation.done() is False
        allow_cleanup.set()
        try:
            await operation
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("operation cancellation must be re-raised")
        assert effects == 0
        assert event_types.count("tool_execution_start") == 1
        assert event_types.count("tool_execution_end") == 1
        assert event_types.index("tool_execution_start") < event_types.index(
            "tool_execution_end"
        )

    asyncio.run(run())


def test_confirmed_model_cleanup_failure_normalizes_idle_without_aborted_history() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    recovered = fauxAssistantMessage("recovered")
    faux.setResponses((recovered,))
    models = createModels()
    models.setProvider(faux.provider)
    model_started = asyncio.Event()
    cleanup_error = RuntimeError("model cleanup canary")
    calls = 0
    event_types: list[str] = []

    async def fail_cleanup_then_recover(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        nonlocal calls
        del options, signal
        calls += 1
        if calls == 1:
            model_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                raise cleanup_error
            if False:
                yield
        async for event in models.streamSimple(model, context, None):
            yield event

    agent = Agent(
        AgentOptions(
            initialState=AgentState(model=model),
            streamFn=fail_cleanup_then_recover,
        )
    )
    agent.subscribe(lambda event, signal: event_types.append(event.type))

    async def run() -> None:
        operation = asyncio.create_task(agent.prompt("first"))
        await model_started.wait()
        waiter = asyncio.create_task(agent.waitForIdle())
        captured_signal = agent.signal
        agent.abort()
        operation_error: LifecycleError | None = None
        try:
            await operation
        except LifecycleError as error:
            operation_error = error
        assert operation_error is not None
        try:
            await waiter
        except LifecycleError as error:
            assert error is operation_error
        else:
            raise AssertionError("idle observer must receive cleanup failure")
        assert operation_error.code == "cleanup"
        assert operation_error.causes == (cleanup_error,)
        assert captured_signal is not None and captured_signal.aborted
        assert agent.signal is None
        assert agent.state.isStreaming is False
        assert agent.state.errorMessage is None
        assert [message.role for message in agent.state.messages] == ["user"]
        assert "agent_end" not in event_types
        await agent.prompt("second")
        assert agent.state.messages[-1] == recovered

    asyncio.run(run())


def test_unconfirmed_cleanup_keeps_agent_aborted_busy_until_real_settlement() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    model_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()

    async def blocked_cleanup(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del model, context, options, signal
        model_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await allow_cleanup.wait()
        if False:
            yield

    agent = Agent(
        AgentOptions(initialState=AgentState(model=model), streamFn=blocked_cleanup)
    )

    async def run() -> None:
        operation = asyncio.create_task(agent.prompt("held"))
        await model_started.wait()
        captured_signal = agent.signal
        waiter = asyncio.create_task(agent.waitForIdle())
        agent.abort()
        await cleanup_started.wait()
        assert captured_signal is not None and captured_signal.aborted
        assert operation.done() is False
        assert waiter.done() is False
        assert agent.signal is captured_signal
        assert agent.state.isStreaming
        assert agent.state.errorMessage is None
        assert [message.role for message in agent.state.messages] == ["user"]
        for rejected in (
            agent.prompt("later"),
            agent.continue_(),
        ):
            try:
                await rejected
            except LifecycleError as error:
                assert error.code == "busy"
            else:
                raise AssertionError("unconfirmed Agent must reject new work")
        try:
            agent.state.systemPrompt = "mutated"
        except LifecycleError as error:
            assert error.code == "busy"
        else:
            raise AssertionError("unconfirmed Agent must reject mutation")
        try:
            agent.reset()
        except LifecycleError as error:
            assert error.code == "busy"
        else:
            raise AssertionError("unconfirmed Agent must reject reset")
        allow_cleanup.set()
        await operation
        await waiter
        terminal = agent.state.messages[-1]
        assert type(terminal) is AssistantMessage
        assert terminal.stopReason == "aborted"
        assert agent.signal is None
        assert agent.state.isStreaming is False

    asyncio.run(run())


def test_tool_abort_keeps_completed_outcome_and_cancels_remaining_calls_in_order() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    calls = tuple(
        fauxToolCall(id=call_id, name=call_id, arguments={})
        for call_id in ("first", "second", "third")
    )
    faux.setResponses((fauxAssistantMessage(calls, stopReason="toolUse"),))
    models = createModels()
    models.setProvider(faux.provider)
    effects: list[str] = []
    second_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()

    async def first(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del params, signal, on_update
        effects.append(tool_call_id)
        return AgentToolResult(
            content=(TextContent(text="first outcome"),), details={}
        )

    async def second(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del params, signal, on_update
        effects.append(tool_call_id)
        second_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await allow_cleanup.wait()
        raise AssertionError("cancelled Tool cannot complete normally")

    async def third(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del params, signal, on_update
        effects.append(tool_call_id)
        return AgentToolResult(content=(), details={})

    async def stream_fn(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del options, signal
        async for event in models.streamSimple(model, context, None):
            yield event

    tools = tuple(
        AgentTool(
            name=name,
            label=name,
            description=name,
            parameters=TOOL_SCHEMA,
            execute=execute,
        )
        for name, execute in (("first", first), ("second", second), ("third", third))
    )
    agent = Agent(
        AgentOptions(
            initialState=AgentState(model=model, tools=tools),
            streamFn=stream_fn,
            toolExecution="sequential",
        )
    )

    async def run() -> None:
        operation = asyncio.create_task(agent.prompt("tools"))
        await second_started.wait()
        captured_signal = agent.signal
        waiter = asyncio.create_task(agent.waitForIdle())
        agent.abort()
        await cleanup_started.wait()
        assert captured_signal is not None and captured_signal.aborted
        assert operation.done() is False
        assert waiter.done() is False
        assert agent.state.pendingToolCalls == frozenset({"second", "third"})
        allow_cleanup.set()
        await operation
        await waiter
        results = [
            message
            for message in agent.state.messages
            if isinstance(message, ToolResultMessage)
        ]
        assert effects == ["first", "second"]
        assert [result.toolCallId for result in results] == [
            "first",
            "second",
            "third",
        ]
        assert [result.isError for result in results] == [False, True, True]
        assert [result.content[0].text for result in results] == [
            "first outcome",
            'Tool "second" execution was cancelled',
            'Tool "third" execution was cancelled',
        ]
        assert agent.state.pendingToolCalls == frozenset()
        terminal = agent.state.messages[-1]
        assert type(terminal) is AssistantMessage
        assert terminal.stopReason == "aborted"
        assert faux.state.callCount == 1

    asyncio.run(run())


def test_terminal_listener_failure_changes_only_the_awaiting_carrier() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    first_response = fauxAssistantMessage("committed")
    second_response = fauxAssistantMessage("reused")
    faux.setResponses((first_response, second_response))
    models = createModels()
    models.setProvider(faux.provider)
    terminal_error = RuntimeError("terminal listener")
    terminal_signal: AbortSignal | None = None
    later_attempted = False

    async def stream_fn(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del options, signal
        async for event in models.streamSimple(model, context, None):
            yield event

    agent = Agent(
        AgentOptions(initialState=AgentState(model=model), streamFn=stream_fn)
    )

    def fail_terminal(event: AgentEvent, signal: AbortSignal) -> None:
        nonlocal terminal_signal
        if isinstance(event, AgentEvent.AgentEnd):
            terminal_signal = signal
            assert agent.state.messages[-1] == first_response
            raise terminal_error

    def later_listener(event: AgentEvent, signal: AbortSignal) -> None:
        nonlocal later_attempted
        del signal
        if isinstance(event, AgentEvent.AgentEnd):
            later_attempted = True

    unsubscribe = agent.subscribe(fail_terminal)
    agent.subscribe(later_listener)

    async def run() -> None:
        try:
            await agent.prompt("first")
        except LifecycleError as error:
            assert error.code == "listener"
            assert error.causes == (terminal_error,)
        else:
            raise AssertionError("terminal listener failure must reach the waiter")
        assert later_attempted
        assert terminal_signal is not None and terminal_signal.aborted is False
        assert [message.role for message in agent.state.messages] == [
            "user",
            "assistant",
        ]
        assert agent.state.messages[-1] == first_response
        assert agent.state.errorMessage is None
        assert agent.state.isStreaming is False
        assert agent.signal is None
        unsubscribe()
        await agent.prompt("second")
        assert agent.state.messages[-1] == second_response

    asyncio.run(run())


def test_external_abort_during_agent_end_listener_is_a_no_op() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    completed = fauxAssistantMessage("committed")
    faux.setResponses((completed,))
    models = createModels()
    models.setProvider(faux.provider)
    terminal_started = asyncio.Event()
    release_terminal = asyncio.Event()
    terminal_signal: AbortSignal | None = None
    terminal_cancelled = False

    async def stream_fn(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del options, signal
        async for event in models.streamSimple(model, context, None):
            yield event

    agent = Agent(
        AgentOptions(initialState=AgentState(model=model), streamFn=stream_fn)
    )

    async def terminal_listener(event: AgentEvent, signal: AbortSignal) -> None:
        nonlocal terminal_cancelled, terminal_signal
        if isinstance(event, AgentEvent.AgentEnd):
            terminal_signal = signal
            terminal_started.set()
            try:
                await release_terminal.wait()
            except asyncio.CancelledError:
                terminal_cancelled = True
                raise

    agent.subscribe(terminal_listener)

    async def run() -> None:
        operation = asyncio.create_task(agent.prompt("go"))
        await terminal_started.wait()
        assert terminal_signal is not None and terminal_signal.aborted is False
        agent.abort()
        assert terminal_signal.aborted is False
        assert operation.done() is False
        release_terminal.set()
        await operation
        assert terminal_cancelled is False
        assert agent.state.messages[-1] == completed
        assert agent.state.isStreaming is False

    asyncio.run(run())


def test_ticket_08_conformance_distinguishes_every_terminal_carrier() -> None:
    matrix = json.loads((ROOT / "conformance/obligation-matrix.json").read_text())
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    required = {
        "omh-v0.agent-clean-cancellation": "reference.agent-clean-cancellation",
        "omh-v0.agent-fail-closed-lifecycle": (
            "reference.agent-fail-closed-lifecycle"
        ),
    }
    rows = {row["id"]: row for row in matrix["obligations"]}
    cases = {case["id"]: case for case in corpus["cases"]}
    assert required.keys() <= rows.keys()
    assert set(required.values()) <= cases.keys()
    for obligation, corpus_case in required.items():
        assert rows[obligation]["corpusCase"] == corpus_case
        assert "ticket-08-agent-cancellation" in rows[obligation]["executableCases"]
        assert "ticket-08-installed" in rows[obligation]["executableCases"]
        assert cases[corpus_case]["obligation"] == obligation
    carriers = cases["reference.agent-fail-closed-lifecycle"]["omhExpectation"][
        "T"
    ]
    assert carriers == {
        "listener": "listener",
        "eventSink": "event_sink",
        "cleanup": "cleanup",
        "model": "error",
    }
