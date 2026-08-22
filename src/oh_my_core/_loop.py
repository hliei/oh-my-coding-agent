from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
import asyncio
import builtins
from dataclasses import dataclass, field, replace
import inspect
from typing import ClassVar, Literal, TypeAlias, cast, final

from oh_my_llm import (
    AbortSignal,
    AssistantMessage,
    AssistantMessageDoneEvent,
    AssistantMessageErrorEvent,
    AssistantMessageEvent,
    AssistantMessageStartEvent,
    Context,
    EventStream,
    LifecycleError,
    Message,
    Model,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
)
from oh_my_llm._streams import _AbortController, _create_event_stream

from ._tools import AgentTool


AgentMessage: TypeAlias = Message
StreamFn: TypeAlias = Callable[
    [Model, Context, object | None, AbortSignal], AsyncIterator[AssistantMessageEvent]
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


AgentEvent.AgentStart = AgentStart
AgentEvent.AgentEnd = AgentEnd
AgentEvent.TurnStart = TurnStart
AgentEvent.TurnEnd = TurnEnd
AgentEvent.MessageStart = MessageStart
AgentEvent.MessageUpdate = MessageUpdate
AgentEvent.MessageEnd = MessageEnd

AgentEventSink: TypeAlias = Callable[[AgentEvent], None | Awaitable[None]]


async def _emit(sink: AgentEventSink, event: AgentEvent) -> None:
    try:
        settled = sink(event)
        if inspect.isawaitable(settled):
            await settled
        elif settled is not None:
            raise TypeError("AgentEventSink must return None or an awaitable")
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


async def runAgentLoop(
    prompts: Sequence[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    streamFn: StreamFn,
) -> tuple[AgentMessage, ...]:
    controller = _AbortController()
    try:
        return await _run_agent_loop(
            _snapshot_prompts(prompts),
            context,
            config,
            emit,
            streamFn,
            controller.signal,
            continuation=False,
            cancellationResult=False,
        )
    except (asyncio.CancelledError, LifecycleError):
        controller.abort()
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
            signal,
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
            signal,
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
    try:
        return await _run_agent_loop(
            (),
            context,
            config,
            emit,
            streamFn,
            controller.signal,
            continuation=True,
            cancellationResult=False,
        )
    except (asyncio.CancelledError, LifecycleError):
        controller.abort()
        raise


async def _run_agent_loop(
    prompt_messages: tuple[AgentMessage, ...],
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    streamFn: StreamFn,
    signal: AbortSignal,
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
        signal,
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
    signal: AbortSignal,
    *,
    cancellationResult: bool,
) -> tuple[AgentMessage, ...]:

    await _emit(emit, AgentStart())
    await _emit(emit, TurnStart())
    for prompt in prompt_messages:
        await _emit(emit, MessageStart(message=prompt))
        await _emit(emit, MessageEnd(message=prompt))

    working = Context(
        systemPrompt=context.systemPrompt,
        messages=(*context.messages, *prompt_messages),
        tools=context.tools,
    )
    response_events = streamFn(config.model, working, None, signal)
    response: AssistantMessage | None = None
    response_started = False
    latest: AssistantMessage | None = None
    try:
        async for event in response_events:
            if isinstance(event, AssistantMessageStartEvent):
                response_started = True
                latest = event.partial
                await _emit(emit, MessageStart(message=event.partial))
            elif isinstance(event, AssistantMessageDoneEvent):
                response = event.message
            elif isinstance(event, AssistantMessageErrorEvent):
                response = event.error
            else:
                latest = event.partial
                await _emit(
                    emit,
                    MessageUpdate(message=event.partial, assistantMessageEvent=event),
                )
    except LifecycleError as sink_error:
        try:
            await _close_async_iterator(response_events)
        except BaseException as cleanup_error:
            raise LifecycleError(
                "event_sink",
                "Agent event sink failed",
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
        response = _aborted_assistant(config.model, latest)
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
        response = _error_assistant(config.model, latest)
        if not response_started:
            await _emit(emit, MessageStart(message=response))

    if response is None:
        raise RuntimeError("streamFn settled without an AssistantMessage")
    await _emit(emit, MessageEnd(message=response))
    result = (*prompt_messages, response)
    await _emit(emit, TurnEnd(message=response, toolResults=()))
    await _emit(emit, AgentEnd(messages=result))
    return result


async def _close_async_iterator(iterator: AsyncIterator[object]) -> None:
    close = getattr(iterator, "aclose", None)
    if close is not None:
        await asyncio.shield(close())


def _aborted_assistant(
    model: Model, partial: AssistantMessage | None
) -> AssistantMessage:
    if partial is not None:
        return replace(
            partial,
            stopReason="aborted",
            errorMessage="Operation aborted",
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
        stopReason="aborted",
        timestamp=0,
        errorMessage="Operation aborted",
    )


def _error_assistant(model: Model, partial: AssistantMessage | None) -> AssistantMessage:
    if partial is not None:
        return replace(
            partial,
            stopReason="error",
            errorMessage="Model stream failed",
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
        stopReason="error",
        timestamp=0,
        errorMessage="Model stream failed",
    )
