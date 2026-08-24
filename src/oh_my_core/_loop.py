from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
import asyncio
import builtins
from dataclasses import dataclass, field, replace
import inspect
import json
import sys
from typing import ClassVar, Literal, NoReturn, TypeAlias, cast, final

from oh_my_llm import (
    AbortSignal,
    AssistantMessage,
    AssistantMessageDoneEvent,
    AssistantMessageErrorEvent,
    AssistantMessageEvent,
    AssistantMessageStartEvent,
    Context,
    EventStream,
    JSONValue,
    LifecycleError,
    Message,
    Model,
    SimpleStreamOptions,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
    validateToolArguments,
)
from oh_my_llm._tool_validation import _mutable_copy
from oh_my_llm._values import (
    _bool,
    _finite_float,
    _safe_integer,
    _snapshot_json,
    _string,
)
from oh_my_llm._streams import (
    _AbortController,
    _abort_signal,
    _bind_abort_signal,
    _create_event_stream,
)

from ._tools import AgentTool, AgentToolResult


AgentMessage: TypeAlias = Message
StreamFn: TypeAlias = Callable[
    [Model, Context, SimpleStreamOptions | None, AbortSignal],
    AsyncIterator[AssistantMessageEvent],
]
ToolExecutionMode: TypeAlias = Literal["sequential", "parallel"]


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class AgentContext:
    messages: tuple[AgentMessage, ...]
    systemPrompt: str
    tools: tuple[AgentTool, ...] | None = None

    def __post_init__(self) -> None:
        if type(self.systemPrompt) is not str:
            raise TypeError("AgentContext.systemPrompt: must be a string")
        if type(self.messages) not in (list, tuple):
            raise TypeError("AgentContext.messages: must be a list or tuple")
        messages = tuple(self.messages)
        if any(
            not isinstance(message, (UserMessage, AssistantMessage, ToolResultMessage))
            for message in messages
        ):
            raise TypeError("AgentContext.messages: must contain AgentMessage values")
        object.__setattr__(self, "messages", messages)
        if self.tools is not None:
            if type(self.tools) not in (list, tuple):
                raise TypeError("AgentContext.tools: must be a list or tuple")
            tools = tuple(self.tools)
            if any(type(tool) is not AgentTool for tool in tools):
                raise TypeError("AgentContext.tools: must contain AgentTool values")
            object.__setattr__(self, "tools", tools)


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class AgentLoopConfig:
    model: Model
    toolExecution: ToolExecutionMode = "parallel"
    temperature: float | None = None
    maxTokens: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.model, Model):
            raise TypeError("AgentLoopConfig.model must be a Model")
        if self.temperature is not None:
            _finite_float(
                self.temperature,
                "AgentLoopConfig",
                "temperature",
                nonnegative=True,
            )
        if self.maxTokens is not None:
            max_tokens = _safe_integer(
                self.maxTokens,
                "AgentLoopConfig",
                "maxTokens",
                nonnegative=True,
            )
            if max_tokens == 0:
                raise ValueError("AgentLoopConfig.maxTokens: must be positive")


class AgentEvent:
    __slots__ = ()
    type: str
    AgentStart: ClassVar[builtins.type[AgentStart]]
    AgentEnd: ClassVar[builtins.type[AgentEnd]]
    TurnStart: ClassVar[builtins.type[TurnStart]]
    TurnEnd: ClassVar[builtins.type[TurnEnd]]
    MessageStart: ClassVar[builtins.type[MessageStart]]
    MessageUpdate: ClassVar[builtins.type[MessageUpdate]]
    MessageEnd: ClassVar[builtins.type[MessageEnd]]
    ToolExecutionStart: ClassVar[builtins.type[ToolExecutionStart]]
    ToolExecutionUpdate: ClassVar[builtins.type[ToolExecutionUpdate]]
    ToolExecutionEnd: ClassVar[builtins.type[ToolExecutionEnd]]

    def __new__(cls, *args: object, **kwargs: object) -> AgentEvent:
        del args, kwargs
        if cls is AgentEvent:
            raise TypeError("AgentEvent is a sealed event base")
        return super().__new__(cls)

    def __init_subclass__(cls) -> None:
        if cls.__module__ != __name__:
            raise TypeError("AgentEvent variants are sealed")
        super().__init_subclass__()


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class AgentStart(AgentEvent):
    type: Literal["agent_start"] = field(init=False, default="agent_start")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class AgentEnd(AgentEvent):
    messages: tuple[AgentMessage, ...]
    type: Literal["agent_end"] = field(init=False, default="agent_end")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class TurnStart(AgentEvent):
    type: Literal["turn_start"] = field(init=False, default="turn_start")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class TurnEnd(AgentEvent):
    message: AssistantMessage
    toolResults: tuple[Message, ...]
    type: Literal["turn_end"] = field(init=False, default="turn_end")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class MessageStart(AgentEvent):
    message: AgentMessage
    type: Literal["message_start"] = field(init=False, default="message_start")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class MessageUpdate(AgentEvent):
    message: AssistantMessage
    assistantMessageEvent: AssistantMessageEvent
    type: Literal["message_update"] = field(init=False, default="message_update")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class MessageEnd(AgentEvent):
    message: AgentMessage
    type: Literal["message_end"] = field(init=False, default="message_end")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class ToolExecutionStart(AgentEvent):
    toolCallId: str
    toolName: str
    args: Mapping[str, JSONValue]
    type: Literal["tool_execution_start"] = field(
        init=False, default="tool_execution_start"
    )

    def __post_init__(self) -> None:
        _bind_tool_event_identity(self)
        object.__setattr__(
            self, "args", _snapshot_tool_args(self.args, type(self).__name__)
        )


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class ToolExecutionUpdate(AgentEvent):
    toolCallId: str
    toolName: str
    args: Mapping[str, JSONValue]
    partialResult: AgentToolResult
    type: Literal["tool_execution_update"] = field(
        init=False, default="tool_execution_update"
    )

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        _bind_tool_event_identity(self)
        object.__setattr__(self, "args", _snapshot_tool_args(self.args, type_name))
        if type(self.partialResult) is not AgentToolResult:
            raise TypeError(f"{type_name}.partialResult: must be an AgentToolResult")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class ToolExecutionEnd(AgentEvent):
    toolCallId: str
    toolName: str
    result: AgentToolResult
    isError: bool
    type: Literal["tool_execution_end"] = field(
        init=False, default="tool_execution_end"
    )

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        _bind_tool_event_identity(self)
        if type(self.result) is not AgentToolResult:
            raise TypeError(f"{type_name}.result: must be an AgentToolResult")
        object.__setattr__(self, "isError", _bool(self.isError, type_name, "isError"))


def _bind_tool_event_identity(
    event: ToolExecutionStart | ToolExecutionUpdate | ToolExecutionEnd,
) -> None:
    type_name = type(event).__name__
    object.__setattr__(
        event, "toolCallId", _string(event.toolCallId, type_name, "toolCallId")
    )
    object.__setattr__(
        event, "toolName", _string(event.toolName, type_name, "toolName")
    )


def _snapshot_tool_args(args: object, type_name: str) -> Mapping[str, JSONValue]:
    snapshotted = _snapshot_json(args, type_name, "args")
    if not isinstance(snapshotted, Mapping):
        raise TypeError(f"{type_name}.args: must be a mapping")
    return snapshotted


AgentEvent.AgentStart = AgentStart
AgentEvent.AgentEnd = AgentEnd
AgentEvent.TurnStart = TurnStart
AgentEvent.TurnEnd = TurnEnd
AgentEvent.MessageStart = MessageStart
AgentEvent.MessageUpdate = MessageUpdate
AgentEvent.MessageEnd = MessageEnd
AgentEvent.ToolExecutionStart = ToolExecutionStart
AgentEvent.ToolExecutionUpdate = ToolExecutionUpdate
AgentEvent.ToolExecutionEnd = ToolExecutionEnd

AgentEventSink: TypeAlias = Callable[[AgentEvent], None | Awaitable[None]]


@final
class _RunControl:
    __slots__ = ("signal", "terminalCommitted")

    def __init__(self, signal: AbortSignal) -> None:
        self.signal = signal
        self.terminalCommitted = False

    def requestCancellation(self) -> bool:
        if self.terminalCommitted or self.signal.aborted:
            return False
        _abort_signal(self.signal)
        return True


class _ListenerFailure(BaseException):
    __slots__ = ("causes",)

    def __init__(self, causes: tuple[BaseException, ...]) -> None:
        self.causes = causes


async def _emit(sink: AgentEventSink, event: AgentEvent) -> None:
    try:
        await _invoke_sink(sink, event)
    except _ListenerFailure as error:
        raise LifecycleError(
            "listener",
            "Agent listener failed",
            causes=error.causes,
        ) from error.causes[0]
    except asyncio.CancelledError as error:
        owner = asyncio.current_task()
        if owner is not None and owner.cancelling():
            raise
        raise LifecycleError(
            "event_sink",
            "Agent event sink failed",
            causes=(error,),
        ) from error
    except BaseException as error:
        raise LifecycleError(
            "event_sink",
            "Agent event sink failed",
            causes=(error,),
        ) from error


async def _invoke_sink(sink: AgentEventSink, event: AgentEvent) -> None:
    settled = sink(event)
    if inspect.isawaitable(settled):
        await settled
    elif settled is not None:
        raise TypeError("AgentEventSink must return None or an awaitable")


async def runAgentLoop(
    prompts: Sequence[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    streamFn: StreamFn,
) -> tuple[AgentMessage, ...]:
    controller = _AbortController()
    control = _RunControl(controller.signal)
    task = asyncio.create_task(
        _run_agent_loop(
            _snapshot_prompts(prompts),
            context,
            config,
            emit,
            streamFn,
            control,
            continuation=False,
            cancellationResult=False,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        control.requestCancellation()
        if not task.done():
            task.cancel()
        await _settle_owned_task(task, cancellation)
    except LifecycleError:
        control.requestCancellation()
        raise


def agentLoop(
    prompts: Sequence[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    streamFn: StreamFn,
) -> EventStream[AgentEvent, tuple[AgentMessage, ...]]:
    prompt_messages = _snapshot_prompts(prompts)
    _validate_run(prompt_messages, context, config, streamFn, continuation=False)
    return _create_event_stream(
        lambda emit, signal: _run_agent_loop(
            prompt_messages,
            context,
            config,
            emit,
            streamFn,
            _RunControl(signal),
            continuation=False,
            cancellationResult=True,
        )
    )


def agentLoopContinue(
    context: AgentContext,
    config: AgentLoopConfig,
    streamFn: StreamFn,
) -> EventStream[AgentEvent, tuple[AgentMessage, ...]]:
    _validate_run((), context, config, streamFn, continuation=True)
    return _create_event_stream(
        lambda emit, signal: _run_agent_loop(
            (),
            context,
            config,
            emit,
            streamFn,
            _RunControl(signal),
            continuation=True,
            cancellationResult=True,
        )
    )


async def runAgentLoopContinue(
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    streamFn: StreamFn,
) -> tuple[AgentMessage, ...]:
    controller = _AbortController()
    control = _RunControl(controller.signal)
    task = asyncio.create_task(
        _run_agent_loop(
            (),
            context,
            config,
            emit,
            streamFn,
            control,
            continuation=True,
            cancellationResult=False,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        control.requestCancellation()
        if not task.done():
            task.cancel()
        await _settle_owned_task(task, cancellation)
    except LifecycleError:
        control.requestCancellation()
        raise


async def _settle_owned_task(
    task: asyncio.Task[tuple[AgentMessage, ...]],
    cancellation: asyncio.CancelledError,
) -> NoReturn:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    try:
        task.result()
    except LifecycleError as cleanup_failure:
        if cleanup_failure.code == "cleanup":
            cancellation.__cause__ = cleanup_failure
    except asyncio.CancelledError:
        pass
    raise cancellation


async def _run_agent_loop(
    prompt_messages: tuple[AgentMessage, ...],
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    streamFn: StreamFn,
    control: _RunControl,
    *,
    continuation: bool,
    cancellationResult: bool,
) -> tuple[AgentMessage, ...]:
    _validate_run(prompt_messages, context, config, streamFn, continuation=continuation)
    if not callable(emit):
        raise TypeError("emit must be callable")
    return await _run_agent_loop_body(
        prompt_messages,
        context,
        config,
        emit,
        streamFn,
        control,
        cancellationResult=cancellationResult,
    )


def _validate_run(
    prompt_messages: tuple[AgentMessage, ...],
    context: AgentContext,
    config: AgentLoopConfig,
    streamFn: StreamFn,
    *,
    continuation: bool,
) -> None:
    if not continuation and not prompt_messages:
        raise ValueError("prompts must not be empty")
    if not isinstance(context, AgentContext):
        raise TypeError("context must be an AgentContext")
    if not isinstance(config, AgentLoopConfig) or not isinstance(config.model, Model):
        raise TypeError("config must contain a Model")
    if not callable(streamFn):
        raise TypeError("streamFn must be callable")
    if not all(
        isinstance(message, (UserMessage, AssistantMessage, ToolResultMessage))
        for message in prompt_messages
    ):
        raise TypeError("prompts must contain AgentMessage values")
    effective_messages = (*context.messages, *prompt_messages)
    if not effective_messages or not isinstance(
        effective_messages[-1], (UserMessage, ToolResultMessage)
    ):
        raise ValueError("the effective prompt tail must be a User or Tool Result Message")


def _snapshot_prompts(prompts: object) -> tuple[AgentMessage, ...]:
    if type(prompts) not in (list, tuple):
        raise TypeError("prompts must be a list or tuple")
    return tuple(cast(Sequence[AgentMessage], prompts))


async def _run_agent_loop_body(
    prompt_messages: tuple[AgentMessage, ...],
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    streamFn: StreamFn,
    control: _RunControl,
    *,
    cancellationResult: bool,
) -> tuple[AgentMessage, ...]:
    await _emit(emit, AgentStart())
    produced: list[AgentMessage] = list(prompt_messages)
    working_messages: tuple[AgentMessage, ...] = (
        *context.messages,
        *prompt_messages,
    )
    seed_emitted = False
    signal = control.signal

    while True:
        await _emit(emit, TurnStart())
        if not seed_emitted:
            for prompt in prompt_messages:
                await _emit(emit, MessageStart(message=prompt))
                await _emit(emit, MessageEnd(message=prompt))
            seed_emitted = True

        working = Context(
            systemPrompt=context.systemPrompt,
            messages=working_messages,
            tools=context.tools,
        )
        if signal.aborted:
            response = _terminal_assistant(
                config.model,
                None,
                reason="aborted",
                error_message="Operation aborted",
            )
            await _emit(emit, MessageStart(message=response))
        else:
            with _bind_abort_signal(signal):
                response_events = streamFn(
                    config.model,
                    working,
                    SimpleStreamOptions(
                        temperature=config.temperature,
                        maxTokens=config.maxTokens,
                    ),
                    signal,
                )
                response = await _consume_response_events(
                    response_events,
                    emit,
                    config.model,
                    control,
                    cancellationResult=cancellationResult,
                )

        produced.append(response)
        working_messages = (*working_messages, response)
        await _emit(emit, MessageEnd(message=response))

        if response.stopReason in ("error", "aborted"):
            await _emit(emit, TurnEnd(message=response, toolResults=()))
            break

        tool_calls = [
            block for block in response.content if isinstance(block, ToolCall)
        ]
        if not tool_calls:
            await _emit(emit, TurnEnd(message=response, toolResults=()))
            if signal.aborted:
                await _append_aborted_tail(produced, config.model, emit)
            break

        tool_results, terminate = await _process_tool_calls(
            tool_calls,
            context.tools,
            config.toolExecution,
            emit,
            signal,
            truncated=response.stopReason == "length",
        )
        for tool_result in tool_results:
            await _emit(emit, MessageStart(message=tool_result))
            await _emit(emit, MessageEnd(message=tool_result))
            produced.append(tool_result)
        working_messages = (*working_messages, *tool_results)
        await _emit(emit, TurnEnd(message=response, toolResults=tool_results))
        if signal.aborted:
            await _append_aborted_tail(produced, config.model, emit)
            break
        if terminate:
            break

    result = tuple(produced)
    control.terminalCommitted = True
    await _emit(emit, AgentEnd(messages=result))
    return result


async def _append_aborted_tail(
    produced: list[AgentMessage],
    model: Model,
    emit: AgentEventSink,
) -> None:
    aborted = _terminal_assistant(
        model,
        None,
        reason="aborted",
        error_message="Operation aborted",
    )
    await _emit(emit, MessageStart(message=aborted))
    await _emit(emit, MessageEnd(message=aborted))
    produced.append(aborted)


@dataclass(frozen=True, slots=True, kw_only=True)
class _ToolAttempt:
    call: ToolCall
    tool: AgentTool | None
    params: dict[str, object] | None
    failure: AgentToolResult | None


class _ToolEventGate:
    __slots__ = ("_failed", "_lock", "_sink")

    def __init__(self, sink: AgentEventSink) -> None:
        self._sink = sink
        self._lock = asyncio.Lock()
        self._failed = False

    async def emit(self, event: AgentEvent) -> None:
        async with self._lock:
            if self._failed:
                return
            try:
                await _invoke_sink(self._sink, event)
            except asyncio.CancelledError:
                owner = asyncio.current_task()
                if owner is not None and owner.cancelling():
                    raise
                self._failed = True
                raise
            except BaseException:
                self._failed = True
                raise

    def close(self) -> None:
        self._failed = True


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _tool_failure_result(text: str) -> AgentToolResult:
    return AgentToolResult(content=(TextContent(text=text),), details={})


async def _process_tool_calls(
    calls: list[ToolCall],
    tools: tuple[AgentTool, ...] | None,
    execution_mode: ToolExecutionMode,
    emit: AgentEventSink,
    signal: AbortSignal,
    *,
    truncated: bool,
) -> tuple[tuple[ToolResultMessage, ...], bool]:
    correlation_failures = _scan_tool_call_ids(calls)
    attempts: list[_ToolAttempt] = []
    started_count = 0
    event_gate = _ToolEventGate(emit)
    for call, correlation_failure in zip(calls, correlation_failures, strict=True):
        if signal.aborted:
            break
        started_count += 1
        try:
            await _emit(
                event_gate.emit,
                ToolExecutionStart(
                    toolCallId=call.id,
                    toolName=call.name,
                    args=call.arguments,
                ),
            )
        except asyncio.CancelledError:
            if not signal.aborted:
                raise
            owner = asyncio.current_task()
            if owner is not None and owner.cancelling():
                owner.uncancel()
            attempts.append(_cancelled_tool_attempt(call))
            break
        if signal.aborted:
            attempts.append(_cancelled_tool_attempt(call))
            break
        if correlation_failure is None:
            attempts.append(
                _truncated_tool_attempt(call)
                if truncated
                else _preflight_tool_call(call, tools)
            )
        else:
            attempts.append(
                _ToolAttempt(
                    call=call,
                    tool=None,
                    params=None,
                    failure=correlation_failure,
                )
            )
    attempts.extend(_cancelled_tool_attempt(call) for call in calls[len(attempts) :])

    sequential = execution_mode == "sequential" or _has_sequential_tool(calls, tools)
    if sequential:
        sequential_settlements: list[tuple[AgentToolResult, bool]] = []
        for index, attempt in enumerate(attempts):
            sequential_settlements.append(
                await _settle_tool_attempt(
                    attempt,
                    signal,
                    event_gate.emit,
                    emit_end=index < started_count,
                )
            )
        settlements = tuple(sequential_settlements)
    else:
        tasks = tuple(
            asyncio.create_task(
                _settle_tool_attempt(
                    attempt,
                    signal,
                    event_gate.emit,
                    emit_end=index < started_count,
                )
            )
            for index, attempt in enumerate(attempts)
        )
        try:
            settlements = tuple(await asyncio.gather(*tasks))
        except asyncio.CancelledError:
            settlements = tuple(await asyncio.gather(*tasks))
        except LifecycleError as lifecycle_failure:
            _abort_signal(signal)
            event_gate.close()
            for task in tasks:
                if not task.done():
                    task.cancel()
            cleanup_causes: list[BaseException] = []
            for settled in await asyncio.gather(*tasks, return_exceptions=True):
                if settled is lifecycle_failure or isinstance(
                    settled, asyncio.CancelledError
                ):
                    continue
                if isinstance(settled, LifecycleError):
                    if settled.code == "cleanup":
                        cleanup_causes.extend(settled.causes)
                    continue
                if isinstance(settled, BaseException):
                    cleanup_causes.append(settled)
            if cleanup_causes:
                raise LifecycleError(
                    lifecycle_failure.code,
                    str(lifecycle_failure),
                    causes=(*lifecycle_failure.causes, *cleanup_causes),
                ) from lifecycle_failure.causes[0]
            raise

    results: list[ToolResultMessage] = []
    for attempt, (result, is_error) in zip(attempts, settlements, strict=True):
        results.append(
            ToolResultMessage(
                toolCallId=attempt.call.id,
                toolName=attempt.call.name,
                content=result.content,
                details=result.details,
                isError=is_error,
                timestamp=0,
            )
        )
    terminate = all(
        not is_error and result.terminate is True
        for result, is_error in settlements
    )
    return tuple(results), terminate


def _has_sequential_tool(
    calls: list[ToolCall], tools: tuple[AgentTool, ...] | None
) -> bool:
    for call in calls:
        matches = tuple(tool for tool in tools or () if tool.name == call.name)
        if len(matches) == 1 and matches[0].executionMode == "sequential":
            return True
    return False


def _truncated_tool_attempt(call: ToolCall) -> _ToolAttempt:
    return _ToolAttempt(
        call=call,
        tool=None,
        params=None,
        failure=_tool_failure_result(
            f"Tool call {_quote(call.name)} was not executed: the response hit the "
            "output token limit, so its arguments may be truncated. Re-issue the tool "
            "call with complete arguments."
        ),
    )


def _cancelled_tool_attempt(call: ToolCall) -> _ToolAttempt:
    return _ToolAttempt(
        call=call,
        tool=None,
        params=None,
        failure=_cancelled_tool_result(call),
    )


def _cancelled_tool_result(call: ToolCall) -> AgentToolResult:
    return _tool_failure_result(f"Tool {_quote(call.name)} execution was cancelled")


async def _settle_tool_attempt(
    attempt: _ToolAttempt,
    signal: AbortSignal,
    emit: AgentEventSink,
    *,
    emit_end: bool,
) -> tuple[AgentToolResult, bool]:
    if attempt.failure is not None:
        result = attempt.failure
        is_error = True
    elif signal.aborted:
        result = _cancelled_tool_result(attempt.call)
        is_error = True
    else:
        try:
            result, is_error = await _execute_tool_attempt(attempt, signal, emit)
        except asyncio.CancelledError:
            result = (
                _cancelled_tool_result(attempt.call)
                if signal.aborted
                else _tool_failure_result(
                    f"Tool {_quote(attempt.call.name)} execution failed"
                )
            )
            is_error = True
        else:
            if signal.aborted:
                result = _cancelled_tool_result(attempt.call)
                is_error = True
    if emit_end:
        await _emit(
            emit,
            ToolExecutionEnd(
                toolCallId=attempt.call.id,
                toolName=attempt.call.name,
                result=result,
                isError=is_error,
            ),
        )
    return result, is_error


def _scan_tool_call_ids(calls: list[ToolCall]) -> tuple[AgentToolResult | None, ...]:
    counts: dict[str, int] = {}
    for call in calls:
        counts[call.id] = counts.get(call.id, 0) + 1
    return tuple(
        _tool_failure_result("Tool call id must not be empty")
        if not call.id
        else _tool_failure_result(
            f"Tool call id {_quote(call.id)} is duplicated in one assistant message"
        )
        if counts[call.id] > 1
        else None
        for call in calls
    )


def _preflight_tool_call(
    call: ToolCall,
    tools: tuple[AgentTool, ...] | None,
) -> _ToolAttempt:
    matches = [tool for tool in tools or () if tool.name == call.name]
    if not matches:
        return _ToolAttempt(
            call=call,
            tool=None,
            params=None,
            failure=_tool_failure_result(f"Tool {_quote(call.name)} was not found"),
        )
    if len(matches) != 1:
        return _ToolAttempt(
            call=call,
            tool=None,
            params=None,
            failure=_tool_failure_result(
                f"Tool {_quote(call.name)} matched more than once"
            ),
        )
    tool = matches[0]
    prepared_call = call
    if tool.prepareArguments is not None:
        try:
            prepared = tool.prepareArguments(
                cast(dict[str, object], _mutable_copy(call.arguments))
            )
        except Exception:
            return _rejected_preparation(call, tool)
        try:
            prepared_call = ToolCall(
                id=call.id,
                name=call.name,
                arguments=cast(Mapping[str, JSONValue], prepared),
            )
        except (TypeError, ValueError):
            return _rejected_preparation(call, tool)
    try:
        params = validateToolArguments(tool, prepared_call)
    except ValueError as error:
        return _ToolAttempt(
            call=call,
            tool=tool,
            params=None,
            failure=_tool_failure_result(str(error)),
        )
    return _ToolAttempt(call=call, tool=tool, params=params, failure=None)


def _rejected_preparation(call: ToolCall, tool: AgentTool) -> _ToolAttempt:
    return _ToolAttempt(
        call=call,
        tool=tool,
        params=None,
        failure=_tool_failure_result(
            f"Tool {_quote(call.name)} argument preparation failed"
        ),
    )


async def _execute_tool_attempt(
    attempt: _ToolAttempt,
    signal: AbortSignal,
    emit: AgentEventSink,
) -> tuple[AgentToolResult, bool]:
    tool = attempt.tool
    params = attempt.params
    if tool is None or params is None:
        raise RuntimeError("approved Tool attempt is missing its callable")
    pending: asyncio.Queue[AgentToolResult | None] = asyncio.Queue()
    invalid_update = False
    settled = False
    executing = False
    name = attempt.call.name

    async def drain() -> None:
        while True:
            item = await pending.get()
            if item is None:
                return
            await _emit(
                emit,
                ToolExecutionUpdate(
                    toolCallId=attempt.call.id,
                    toolName=attempt.call.name,
                    args=attempt.call.arguments,
                    partialResult=item,
                ),
            )

    def on_update(partial: object) -> None:
        nonlocal invalid_update
        if settled or invalid_update or signal.aborted:
            return
        if type(partial) is not AgentToolResult:
            invalid_update = True
            raise TypeError(f"Tool {_quote(name)} produced an invalid update")
        pending.put_nowait(partial)

    def fail(text: str) -> tuple[AgentToolResult, bool]:
        return _tool_failure_result(text), True

    drain_task = asyncio.create_task(drain())
    owner_task = asyncio.current_task()

    def interrupt_on_drain_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled() or task.exception() is None:
            return
        _abort_signal(signal)
        if executing and owner_task is not None and not owner_task.done():
            owner_task.cancel()

    drain_task.add_done_callback(interrupt_on_drain_failure)
    try:
        try:
            returned = tool.execute(attempt.call.id, params, signal, on_update)
        except Exception as error:
            if isinstance(error, LifecycleError) and error.code == "cleanup":
                raise
            if signal.aborted:
                raise LifecycleError(
                    "cleanup",
                    "Agent loop cleanup failed",
                    causes=(error,),
                ) from error
            if invalid_update:
                return fail(f"Tool {_quote(name)} produced an invalid update")
            return fail(f"Tool {_quote(name)} execution failed")
        if invalid_update:
            if inspect.isawaitable(returned):
                try:
                    await returned
                except Exception:
                    pass
            return fail(f"Tool {_quote(name)} produced an invalid update")
        if not inspect.isawaitable(returned):
            return fail(f"Tool {_quote(name)} execute must return an awaitable")
        executing = True
        try:
            try:
                final: object = await returned
            except Exception as error:
                if isinstance(error, LifecycleError) and error.code == "cleanup":
                    raise
                if signal.aborted:
                    raise LifecycleError(
                        "cleanup",
                        "Agent loop cleanup failed",
                        causes=(error,),
                    ) from error
                if invalid_update:
                    return fail(f"Tool {_quote(name)} produced an invalid update")
                return fail(f"Tool {_quote(name)} execution failed")
        finally:
            executing = False
        if invalid_update:
            return fail(f"Tool {_quote(name)} produced an invalid update")
        if type(final) is not AgentToolResult:
            return fail(f"Tool {_quote(name)} produced an invalid result")
        return final, False
    finally:
        primary = sys.exception()
        settled = True
        pending.put_nowait(None)
        try:
            await drain_task
        except LifecycleError as callback_failure:
            if isinstance(primary, LifecycleError) and primary.code == "cleanup":
                raise LifecycleError(
                    callback_failure.code,
                    str(callback_failure),
                    causes=(*callback_failure.causes, *primary.causes),
                ) from callback_failure.causes[0]
            raise


async def _consume_response_events(
    response_events: AsyncIterator[AssistantMessageEvent],
    emit: AgentEventSink,
    model: Model,
    control: _RunControl,
    *,
    cancellationResult: bool,
) -> AssistantMessage:
    signal = control.signal
    response: AssistantMessage | None = None
    response_started = False
    latest: AssistantMessage | None = None
    try:
        iterator = response_events.__aiter__()
        while True:
            if signal.aborted:
                raise asyncio.CancelledError
            try:
                event = await anext(iterator)
            except StopAsyncIteration:
                break
            if signal.aborted:
                raise asyncio.CancelledError
            if isinstance(event, AssistantMessageStartEvent):
                response_started = True
                latest = event.partial
                await _emit(emit, MessageStart(message=event.partial))
            elif isinstance(event, AssistantMessageDoneEvent):
                response = latest = event.message
            elif isinstance(event, AssistantMessageErrorEvent):
                response = latest = event.error
            else:
                latest = event.partial
                await _emit(
                    emit,
                    MessageUpdate(message=event.partial, assistantMessageEvent=event),
                )
    except LifecycleError as sink_error:
        control.requestCancellation()
        try:
            await _close_async_iterator(response_events)
        except BaseException as cleanup_error:
            raise LifecycleError(
                sink_error.code,
                str(sink_error),
                causes=(*sink_error.causes, cleanup_error),
            ) from sink_error.causes[0]
        raise
    except asyncio.CancelledError as cancellation:
        try:
            await _close_async_iterator(response_events)
        except BaseException as cleanup_error:
            cleanup_failure = LifecycleError(
                "cleanup",
                "Agent loop cleanup failed",
                causes=(cleanup_error,),
            )
            if cancellationResult:
                raise cleanup_failure from cleanup_error
            cancellation.__cause__ = cleanup_failure
            raise cancellation
        if not cancellationResult:
            raise
        response = _terminal_assistant(
            model,
            latest,
            reason="aborted",
            error_message="Operation aborted",
        )
        if not response_started:
            await _emit(emit, MessageStart(message=response))
    except Exception as stream_error:
        owner = asyncio.current_task()
        if signal.aborted or (owner is not None and owner.cancelling()):
            cleanup_failure = LifecycleError(
                "cleanup",
                "Agent loop cleanup failed",
                causes=(stream_error,),
            )
            if cancellationResult:
                raise cleanup_failure from stream_error
            owner_cancellation = asyncio.CancelledError()
            owner_cancellation.__cause__ = cleanup_failure
            raise owner_cancellation
        await _close_async_iterator(response_events)
        response = _terminal_assistant(
            model,
            latest,
            reason="error",
            error_message="Model stream failed",
        )
        if not response_started:
            await _emit(emit, MessageStart(message=response))

    if response is None:
        raise RuntimeError("streamFn settled without an AssistantMessage")
    if signal.aborted:
        return _terminal_assistant(
            model,
            latest,
            reason="aborted",
            error_message="Operation aborted",
        )
    return response


async def _close_async_iterator(iterator: AsyncIterator[object]) -> None:
    close = getattr(iterator, "aclose", None)
    if close is not None:
        await asyncio.shield(close())


def _terminal_assistant(
    model: Model,
    partial: AssistantMessage | None,
    *,
    reason: Literal["error", "aborted"],
    error_message: str,
) -> AssistantMessage:
    if partial is not None:
        return replace(
            partial,
            stopReason=reason,
            errorMessage=error_message,
        )
    return AssistantMessage(
        content=(),
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=Usage(
            input=0,
            output=0,
            cacheRead=0,
            cacheWrite=0,
            totalTokens=0,
            cost=UsageCost(
                input=0.0,
                output=0.0,
                cacheRead=0.0,
                cacheWrite=0.0,
                total=0.0,
            ),
        ),
        stopReason=reason,
        timestamp=0,
        errorMessage=error_message,
    )
