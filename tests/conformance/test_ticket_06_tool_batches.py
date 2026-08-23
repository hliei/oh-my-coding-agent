from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal

from oh_my_core import (
    AgentContext,
    AgentEvent,
    AgentLoopConfig,
    AgentTool,
    AgentToolResult,
    StreamFn,
    ToolExecutionMode,
    agentLoop,
    runAgentLoop,
)
from oh_my_llm import (
    AbortSignal,
    AssistantMessage,
    AssistantMessageEvent,
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


TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ("value",),
    "additionalProperties": False,
}


def _stream_fn(models: Any) -> StreamFn:
    async def stream_fn(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del signal
        async for event in models.streamSimple(model, context, options):
            yield event

    return stream_fn


async def _run(
    responses: tuple[AssistantMessage, ...],
    tools: tuple[AgentTool, ...],
    *,
    tool_execution: ToolExecutionMode = "parallel",
    events: list[AgentEvent] | None = None,
) -> tuple[tuple[Any, ...], list[AgentEvent], int]:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    faux.setResponses(responses)
    models = createModels()
    models.setProvider(faux.provider)
    emitted = events if events is not None else []
    result = await runAgentLoop(
        (UserMessage(content="use tools", timestamp=1),),
        AgentContext(systemPrompt="", messages=(), tools=tools),
        AgentLoopConfig(model=model, toolExecution=tool_execution),
        emitted.append,
        _stream_fn(models),
    )
    return result, emitted, faux.state.callCount


def _tool(
    name: str,
    execute: Callable[..., Any],
    *,
    prepare: Callable[[dict[str, object]], dict[str, object]] | None = None,
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


def _tool_results(events: list[AgentEvent]) -> list[ToolResultMessage]:
    return [
        event.message
        for event in events
        if isinstance(event, AgentEvent.MessageStart)
        and isinstance(event.message, ToolResultMessage)
    ]


def test_correlation_scan_rejects_all_bad_ids_without_blocking_unique_calls() -> None:
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

    async def run() -> None:
        _result, events, provider_calls = await _run(
            (
                fauxAssistantMessage(calls, stopReason="toolUse"),
                fauxAssistantMessage("done"),
            ),
            (_tool("work", execute, prepare=prepare),),
            tool_execution="sequential",
        )
        starts = [
            event
            for event in events
            if isinstance(event, AgentEvent.ToolExecutionStart)
        ]
        results = _tool_results(events)
        assert [event.toolCallId for event in starts] == [
            "duplicate",
            "unique",
            "duplicate",
            "",
        ]
        assert prepared == [2]
        assert executed == ["unique"]
        assert provider_calls == 2
        assert [result.isError for result in results] == [True, False, True, True]
        assert [result.content[0].text for result in results] == [
            'Tool call id "duplicate" is duplicated in one assistant message',
            "ok:unique",
            'Tool call id "duplicate" is duplicated in one assistant message',
            "Tool call id must not be empty",
        ]

    asyncio.run(run())


def test_global_or_called_tool_sequential_mode_serializes_the_complete_batch() -> None:
    async def exercise(
        global_mode: ToolExecutionMode,
        called_tool_mode: Literal["sequential", "parallel"] | None,
    ) -> None:
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        release_first = asyncio.Event()
        trace: list[str] = []

        async def execute_first(
            tool_call_id: str,
            params: dict[str, object],
            signal: Any,
            on_update: Any,
        ) -> AgentToolResult:
            del tool_call_id, params, signal, on_update
            trace.append("first:start")
            first_started.set()
            await release_first.wait()
            trace.append("first:end")
            return AgentToolResult(content=(), details={})

        async def execute_second(
            tool_call_id: str,
            params: dict[str, object],
            signal: Any,
            on_update: Any,
        ) -> AgentToolResult:
            del tool_call_id, params, signal, on_update
            trace.append("second:start")
            second_started.set()
            return AgentToolResult(content=(), details={})

        calls = (
            fauxToolCall(id="first", name="first", arguments={"value": 1}),
            fauxToolCall(id="second", name="second", arguments={"value": 2}),
        )
        operation = asyncio.create_task(
            _run(
                (
                    fauxAssistantMessage(calls, stopReason="toolUse"),
                    fauxAssistantMessage("done"),
                ),
                (
                    _tool("first", execute_first),
                    _tool(
                        "second",
                        execute_second,
                        execution_mode=called_tool_mode,
                    ),
                ),
                tool_execution=global_mode,
            )
        )
        try:
            await first_started.wait()
            scheduler_barrier = asyncio.Event()
            asyncio.get_running_loop().call_soon(scheduler_barrier.set)
            await scheduler_barrier.wait()
            assert second_started.is_set() is False
        finally:
            release_first.set()
        await operation
        assert trace == ["first:start", "first:end", "second:start"]

    async def run() -> None:
        await exercise("sequential", None)
        await exercise("parallel", "sequential")

    asyncio.run(run())


def test_correlation_rejected_sequential_call_still_serializes_eligible_calls() -> None:
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()

    async def rejected_execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal, on_update
        raise AssertionError("correlation-rejected Tool must not execute")

    async def execute_first(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal, on_update
        first_started.set()
        await release_first.wait()
        return AgentToolResult(content=(), details={})

    async def execute_second(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal, on_update
        second_started.set()
        return AgentToolResult(content=(), details={})

    calls = (
        fauxToolCall(id="duplicate", name="guard", arguments={"value": 1}),
        fauxToolCall(id="first", name="first", arguments={"value": 2}),
        fauxToolCall(id="duplicate", name="guard", arguments={"value": 3}),
        fauxToolCall(id="second", name="second", arguments={"value": 4}),
    )

    async def run() -> None:
        operation = asyncio.create_task(
            _run(
                (
                    fauxAssistantMessage(calls, stopReason="toolUse"),
                    fauxAssistantMessage("done"),
                ),
                (
                    _tool("guard", rejected_execute, execution_mode="sequential"),
                    _tool("first", execute_first),
                    _tool("second", execute_second),
                ),
            )
        )
        try:
            await first_started.wait()
            scheduler_barrier = asyncio.Event()
            asyncio.get_running_loop().call_soon(scheduler_barrier.set)
            await scheduler_barrier.wait()
            assert second_started.is_set() is False
        finally:
            release_first.set()
        await operation

    asyncio.run(run())


def test_ambiguous_sequential_definition_does_not_serialize_approved_calls() -> None:
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()

    async def rejected_execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal, on_update
        raise AssertionError("ambiguous Tool must not execute")

    async def execute_first(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal, on_update
        first_started.set()
        await release_first.wait()
        return AgentToolResult(content=(), details={})

    async def execute_second(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal, on_update
        second_started.set()
        return AgentToolResult(content=(), details={})

    calls = (
        fauxToolCall(id="ambiguous", name="guard", arguments={"value": 1}),
        fauxToolCall(id="first", name="first", arguments={"value": 2}),
        fauxToolCall(id="second", name="second", arguments={"value": 3}),
    )

    async def run() -> None:
        operation = asyncio.create_task(
            _run(
                (
                    fauxAssistantMessage(calls, stopReason="toolUse"),
                    fauxAssistantMessage("done"),
                ),
                (
                    _tool("guard", rejected_execute, execution_mode="sequential"),
                    _tool("guard", rejected_execute),
                    _tool("first", execute_first),
                    _tool("second", execute_second),
                ),
            )
        )
        try:
            await first_started.wait()
            scheduler_barrier = asyncio.Event()
            asyncio.get_running_loop().call_soon(scheduler_barrier.set)
            await scheduler_barrier.wait()
            assert second_started.is_set() is True
        finally:
            release_first.set()
        await operation

    asyncio.run(run())


def test_default_parallel_batch_preflights_before_effects_and_projects_in_source_order() -> None:
    trace: list[str] = []
    first_started = asyncio.Event()
    second_settled = asyncio.Event()
    release_first = asyncio.Event()

    def prepare_first(arguments: dict[str, object]) -> dict[str, object]:
        trace.append("prepare:first")
        return arguments

    def prepare_second(arguments: dict[str, object]) -> dict[str, object]:
        trace.append("prepare:second")
        return arguments

    async def execute_first(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal
        trace.append("execute:first")
        first_started.set()
        on_update(AgentToolResult(content=(TextContent(text="first:update"),), details={}))
        await release_first.wait()
        return AgentToolResult(content=(TextContent(text="first:done"),), details={})

    async def execute_second(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal, on_update
        trace.append("execute:second")
        second_settled.set()
        return AgentToolResult(content=(TextContent(text="second:done"),), details={})

    calls = (
        fauxToolCall(id="first", name="first", arguments={"value": 1}),
        fauxToolCall(id="second", name="second", arguments={"value": 2}),
    )

    async def run() -> None:
        events: list[AgentEvent] = []

        async def execute() -> tuple[tuple[Any, ...], list[AgentEvent], int]:
            result, emitted, provider_calls = await _run(
                (
                    fauxAssistantMessage(calls, stopReason="toolUse"),
                    fauxAssistantMessage("done"),
                ),
                (
                    _tool("first", execute_first, prepare=prepare_first),
                    _tool("second", execute_second, prepare=prepare_second),
                ),
                events=events,
            )
            return result, emitted, provider_calls

        operation = asyncio.create_task(execute())
        try:
            await first_started.wait()
            scheduler_barrier = asyncio.Event()
            asyncio.get_running_loop().call_soon(scheduler_barrier.set)
            await scheduler_barrier.wait()
            assert trace == [
                "prepare:first",
                "prepare:second",
                "execute:first",
                "execute:second",
            ]
            await second_settled.wait()
            scheduler_barrier.clear()
            asyncio.get_running_loop().call_soon(scheduler_barrier.set)
            await scheduler_barrier.wait()
            assert any(
                isinstance(event, AgentEvent.ToolExecutionEnd)
                and event.toolCallId == "second"
                for event in events
            )
            assert _tool_results(events) == []
        finally:
            release_first.set()
        _result, emitted, provider_calls = await operation
        ends = [
            event.toolCallId
            for event in emitted
            if isinstance(event, AgentEvent.ToolExecutionEnd)
        ]
        assert ends == ["second", "first"]
        assert [result.toolCallId for result in _tool_results(emitted)] == [
            "first",
            "second",
        ]
        assert provider_calls == 2

    asyncio.run(run())


def test_length_truncated_batch_projects_recoverable_failures_without_tool_effects() -> None:
    prepared = False
    executed = False

    def prepare(arguments: dict[str, object]) -> dict[str, object]:
        nonlocal prepared
        prepared = True
        return arguments

    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        nonlocal executed
        del tool_call_id, params, signal, on_update
        executed = True
        return AgentToolResult(content=(), details={})

    calls = (
        fauxToolCall(id="first", name="work", arguments={"value": 1}),
        fauxToolCall(id="second", name="work", arguments={"value": 2}),
    )

    async def run() -> None:
        _result, events, provider_calls = await _run(
            (
                fauxAssistantMessage(calls, stopReason="length"),
                fauxAssistantMessage("reissued"),
            ),
            (_tool("work", execute, prepare=prepare),),
        )
        results = _tool_results(events)
        assert prepared is False
        assert executed is False
        assert provider_calls == 2
        assert [result.toolCallId for result in results] == ["first", "second"]
        assert all(result.isError for result in results)
        assert [result.content[0].text for result in results] == [
            'Tool call "work" was not executed: the response hit the output token '
            "limit, so its arguments may be truncated. Re-issue the tool call with "
            "complete arguments.",
            'Tool call "work" was not executed: the response hit the output token '
            "limit, so its arguments may be truncated. Re-issue the tool call with "
            "complete arguments.",
        ]

    asyncio.run(run())


def test_nonempty_batch_terminates_only_after_all_true_results_are_projected() -> None:
    def terminating_tool(name: str) -> AgentTool:
        async def execute(
            tool_call_id: str,
            params: dict[str, object],
            signal: Any,
            on_update: Any,
        ) -> AgentToolResult:
            del tool_call_id, params, signal, on_update
            return AgentToolResult(
                content=(TextContent(text=f"{name}:done"),),
                details={},
                terminate=True,
            )

        return _tool(name, execute)

    calls = (
        fauxToolCall(id="first", name="first", arguments={"value": 1}),
        fauxToolCall(id="second", name="second", arguments={"value": 2}),
    )

    async def run() -> None:
        result, events, provider_calls = await _run(
            (fauxAssistantMessage(calls, stopReason="toolUse"),),
            (terminating_tool("first"), terminating_tool("second")),
        )
        tool_results = _tool_results(events)
        assert provider_calls == 1
        assert [message.role for message in result] == [
            "user",
            "assistant",
            "toolResult",
            "toolResult",
        ]
        assert [message.toolCallId for message in tool_results] == ["first", "second"]
        turn_end = events[-2]
        assert isinstance(turn_end, AgentEvent.TurnEnd)
        assert turn_end.toolResults == tuple(tool_results)
        assert isinstance(events[-1], AgentEvent.AgentEnd)

    asyncio.run(run())


def test_false_or_absent_termination_intent_continues_the_run() -> None:
    def result_tool(name: str, terminate: bool | None) -> AgentTool:
        async def execute(
            tool_call_id: str,
            params: dict[str, object],
            signal: Any,
            on_update: Any,
        ) -> AgentToolResult:
            del tool_call_id, params, signal, on_update
            return AgentToolResult(content=(), details={}, terminate=terminate)

        return _tool(name, execute)

    calls = (
        fauxToolCall(id="true", name="true", arguments={"value": 1}),
        fauxToolCall(id="false", name="false", arguments={"value": 2}),
        fauxToolCall(id="absent", name="absent", arguments={"value": 3}),
    )

    async def run() -> None:
        result, _events, provider_calls = await _run(
            (
                fauxAssistantMessage(calls, stopReason="toolUse"),
                fauxAssistantMessage("continued"),
            ),
            (
                result_tool("true", True),
                result_tool("false", False),
                result_tool("absent", None),
            ),
        )
        assert provider_calls == 2
        assert result[-1].role == "assistant"

    asyncio.run(run())


def test_tool_call_id_can_be_reused_after_its_turn_settles() -> None:
    executions: list[str] = []

    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del params, signal, on_update
        executions.append(tool_call_id)
        return AgentToolResult(
            content=(TextContent(text=f"run:{len(executions)}"),),
            details={},
            terminate=len(executions) == 2,
        )

    call = fauxToolCall(id="reused", name="work", arguments={"value": 1})

    async def run() -> None:
        result, events, provider_calls = await _run(
            (
                fauxAssistantMessage(call, stopReason="toolUse"),
                fauxAssistantMessage(call, stopReason="toolUse"),
            ),
            (_tool("work", execute),),
        )
        assert executions == ["reused", "reused"]
        assert provider_calls == 2
        assert [message.toolCallId for message in _tool_results(events)] == [
            "reused",
            "reused",
        ]
        assert [message.role for message in result] == [
            "user",
            "assistant",
            "toolResult",
            "assistant",
            "toolResult",
        ]

    asyncio.run(run())


def test_parallel_batch_cancellation_settles_started_tools_and_correlated_results() -> None:
    second_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def execute_first(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal, on_update
        return AgentToolResult(
            content=(TextContent(text="first:done"),), details={}
        )

    async def execute_second(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal
        second_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            on_update(
                AgentToolResult(
                    content=(TextContent(text="late:update"),), details={}
                )
            )
            await release_cleanup.wait()
            return AgentToolResult(
                content=(TextContent(text="must be discarded"),), details={}
            )
        raise AssertionError("unreachable")

    calls = (
        fauxToolCall(id="first", name="first", arguments={"value": 1}),
        fauxToolCall(id="second", name="second", arguments={"value": 2}),
    )

    async def run() -> None:
        faux = fauxProvider()
        model = faux.getModel()
        assert model is not None
        faux.setResponses((fauxAssistantMessage(calls, stopReason="toolUse"),))
        models = createModels()
        models.setProvider(faux.provider)
        stream = agentLoop(
            (UserMessage(content="use tools", timestamp=1),),
            AgentContext(
                systemPrompt="",
                messages=(),
                tools=(
                    _tool("first", execute_first),
                    _tool("second", execute_second),
                ),
            ),
            AgentLoopConfig(model=model),
            _stream_fn(models),
        )
        events: list[AgentEvent] = []
        first_ended = asyncio.Event()

        async def consume() -> None:
            async for event in stream:
                events.append(event)
                if (
                    isinstance(event, AgentEvent.ToolExecutionEnd)
                    and event.toolCallId == "first"
                ):
                    first_ended.set()

        consumer = asyncio.create_task(consume())
        result_waiter = asyncio.create_task(stream.result())
        await second_started.wait()
        await first_ended.wait()
        result_waiter.cancel()
        await cleanup_started.wait()
        scheduler_barrier = asyncio.Event()
        asyncio.get_running_loop().call_soon(scheduler_barrier.set)
        await scheduler_barrier.wait()
        assert result_waiter.done() is False
        release_cleanup.set()
        try:
            await result_waiter
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("the cancelled result waiter must re-raise cancellation")
        await consumer
        result = await stream.result()
        tool_results = _tool_results(events)
        assert faux.state.callCount == 1
        assert [message.toolCallId for message in tool_results] == ["first", "second"]
        assert [message.isError for message in tool_results] == [False, True]
        assert not any(
            isinstance(event, AgentEvent.ToolExecutionUpdate)
            and event.toolCallId == "second"
            for event in events
        )
        assert tool_results[1].content[0].text == (
            'Tool "second" execution was cancelled'
        )
        assert [message.role for message in result] == [
            "user",
            "assistant",
            "toolResult",
            "toolResult",
            "assistant",
        ]
        terminal = result[-1]
        assert isinstance(terminal, AssistantMessage)
        assert terminal.stopReason == "aborted"
        assert terminal.errorMessage == "Operation aborted"
        assert isinstance(events[-1], AgentEvent.AgentEnd)
        assert events[-1].messages is result

    asyncio.run(run())


def test_sequential_batch_cancellation_prevents_later_tool_and_model_effects() -> None:
    first_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    second_executed = False

    async def execute_first(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        del tool_call_id, params, signal, on_update
        first_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            await release_cleanup.wait()
            raise
        raise AssertionError("unreachable")

    async def execute_second(
        tool_call_id: str,
        params: dict[str, object],
        signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        nonlocal second_executed
        del tool_call_id, params, signal, on_update
        second_executed = True
        return AgentToolResult(content=(), details={})

    calls = (
        fauxToolCall(id="first", name="first", arguments={"value": 1}),
        fauxToolCall(id="second", name="second", arguments={"value": 2}),
    )

    async def run() -> None:
        faux = fauxProvider()
        model = faux.getModel()
        assert model is not None
        faux.setResponses((fauxAssistantMessage(calls, stopReason="toolUse"),))
        models = createModels()
        models.setProvider(faux.provider)
        events: list[AgentEvent] = []
        operation = asyncio.create_task(
            runAgentLoop(
                (UserMessage(content="use tools", timestamp=1),),
                AgentContext(
                    systemPrompt="",
                    messages=(),
                    tools=(
                        _tool("first", execute_first),
                        _tool("second", execute_second),
                    ),
                ),
                AgentLoopConfig(model=model, toolExecution="sequential"),
                events.append,
                _stream_fn(models),
            )
        )
        await first_started.wait()
        operation.cancel()
        await cleanup_started.wait()
        scheduler_barrier = asyncio.Event()
        asyncio.get_running_loop().call_soon(scheduler_barrier.set)
        await scheduler_barrier.wait()
        assert operation.done() is False
        release_cleanup.set()
        try:
            await operation
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("the cancelled awaited Run must re-raise cancellation")
        tool_results = _tool_results(events)
        assert second_executed is False
        assert faux.state.callCount == 1
        assert [message.toolCallId for message in tool_results] == ["first", "second"]
        assert all(message.isError for message in tool_results)
        assert [message.content[0].text for message in tool_results] == [
            'Tool "first" execution was cancelled',
            'Tool "second" execution was cancelled',
        ]
        assert isinstance(events[-1], AgentEvent.AgentEnd)
        assert events[-2].type == "message_end"

    asyncio.run(run())
