from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
import builtins
from dataclasses import dataclass, field
import inspect
from typing import ClassVar, Literal, TypeAlias, final

from oh_my_llm import (
    AssistantMessage,
    AssistantMessageDoneEvent,
    AssistantMessageEvent,
    AssistantMessageStartEvent,
    Context,
    Message,
    Model,
    UserMessage,
)


AgentMessage: TypeAlias = Message
StreamFn: TypeAlias = Callable[
    [Model, Context, object | None], AsyncIterator[AssistantMessageEvent]
]
ToolExecutionMode: TypeAlias = Literal["sequential", "parallel"]


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class AgentContext:
    messages: tuple[AgentMessage, ...]
    systemPrompt: str | None
    tools: tuple[object, ...] | None = None


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
    settled = sink(event)
    if inspect.isawaitable(settled):
        await settled


async def runAgentLoop(
    prompts: Sequence[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    streamFn: StreamFn,
) -> tuple[AgentMessage, ...]:
    prompt_messages = tuple(prompts)
    if not prompt_messages:
        raise ValueError("prompts must not be empty")
    if not isinstance(context, AgentContext):
        raise TypeError("context must be an AgentContext")
    if not isinstance(config, AgentLoopConfig) or not isinstance(config.model, Model):
        raise TypeError("config must contain a Model")
    if not callable(emit):
        raise TypeError("emit must be callable")
    if not callable(streamFn):
        raise TypeError("streamFn must be callable")
    if not all(isinstance(message, (UserMessage, AssistantMessage)) for message in prompt_messages):
        raise TypeError("prompts must contain AgentMessage values")
    if not isinstance(prompt_messages[-1], UserMessage):
        raise ValueError("the effective prompt tail must be a UserMessage")

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
    response_events = streamFn(config.model, working, None)
    response: AssistantMessage | None = None
    async for event in response_events:
        if isinstance(event, AssistantMessageStartEvent):
            await _emit(emit, MessageStart(message=event.partial))
        elif isinstance(event, AssistantMessageDoneEvent):
            response = event.message
        else:
            await _emit(
                emit,
                MessageUpdate(message=event.partial, assistantMessageEvent=event),
            )

    if response is None:
        raise RuntimeError("streamFn settled without an AssistantMessage")
    await _emit(emit, MessageEnd(message=response))
    result = (*prompt_messages, response)
    await _emit(emit, TurnEnd(message=response, toolResults=()))
    await _emit(emit, AgentEnd(messages=result))
    return result
