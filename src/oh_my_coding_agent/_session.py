from __future__ import annotations

import asyncio
import builtins
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import inspect
import os
import time
from typing import Any, ClassVar, Literal, TypeAlias, cast, final

from oh_my_core import (
    Agent,
    AgentEvent,
    AgentMessage,
    AgentOptions,
    AgentState,
    AgentToolResult,
)
from oh_my_llm import (
    AbortSignal,
    AssistantMessage,
    AssistantMessageEvent,
    JSONValue,
    LifecycleError,
    Model,
    ModelsError,
    SimpleStreamOptions,
    UserMessage,
    createModels,
)
from oh_my_llm.providers.deepseek import deepseekProvider

from ._session_manager import (
    CompactionResult,
    SessionManager,
    _resolve_path,
)


_SESSION_TOKEN = object()


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class CreateAgentSessionOptions:
    cwd: str | None = None
    model: Model | None = None
    sessionManager: SessionManager | None = None


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class CreateAgentSessionResult:
    session: "AgentSession"


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class PromptOptions:
    expandPromptTemplates: bool = True

    def __post_init__(self) -> None:
        if type(self.expandPromptTemplates) is not bool:
            raise TypeError("PromptOptions.expandPromptTemplates: must be a bool")


class AgentSessionEvent:
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
    CompactionStart: ClassVar[builtins.type[CompactionStart]]
    CompactionEnd: ClassVar[builtins.type[CompactionEnd]]
    AgentSettled: ClassVar[builtins.type[AgentSettled]]

    def __new__(cls, *args: object, **kwargs: object) -> AgentSessionEvent:
        del args, kwargs
        if cls is AgentSessionEvent:
            raise TypeError("AgentSessionEvent is a sealed event base")
        return super().__new__(cls)

    def __init_subclass__(cls) -> None:
        if cls.__module__ != __name__:
            raise TypeError("AgentSessionEvent variants are sealed")
        super().__init_subclass__()


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class AgentStart(AgentSessionEvent):
    type: Literal["agent_start"] = field(init=False, default="agent_start")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class AgentEnd(AgentSessionEvent):
    messages: tuple[AgentMessage, ...]
    willRetry: bool
    type: Literal["agent_end"] = field(init=False, default="agent_end")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class TurnStart(AgentSessionEvent):
    type: Literal["turn_start"] = field(init=False, default="turn_start")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class TurnEnd(AgentSessionEvent):
    message: AssistantMessage
    toolResults: tuple[AgentMessage, ...]
    type: Literal["turn_end"] = field(init=False, default="turn_end")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class MessageStart(AgentSessionEvent):
    message: AgentMessage
    type: Literal["message_start"] = field(init=False, default="message_start")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class MessageUpdate(AgentSessionEvent):
    message: AssistantMessage
    assistantMessageEvent: AssistantMessageEvent
    type: Literal["message_update"] = field(init=False, default="message_update")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class MessageEnd(AgentSessionEvent):
    message: AgentMessage
    type: Literal["message_end"] = field(init=False, default="message_end")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class ToolExecutionStart(AgentSessionEvent):
    toolCallId: str
    toolName: str
    args: Mapping[str, JSONValue]
    type: Literal["tool_execution_start"] = field(
        init=False, default="tool_execution_start"
    )


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class ToolExecutionUpdate(AgentSessionEvent):
    toolCallId: str
    toolName: str
    args: Mapping[str, JSONValue]
    partialResult: AgentToolResult
    type: Literal["tool_execution_update"] = field(
        init=False, default="tool_execution_update"
    )


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class ToolExecutionEnd(AgentSessionEvent):
    toolCallId: str
    toolName: str
    result: AgentToolResult
    isError: bool
    type: Literal["tool_execution_end"] = field(
        init=False, default="tool_execution_end"
    )


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class CompactionStart(AgentSessionEvent):
    reason: Literal["manual", "threshold", "overflow"]
    type: Literal["compaction_start"] = field(init=False, default="compaction_start")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class CompactionEnd(AgentSessionEvent):
    reason: Literal["manual", "threshold", "overflow"]
    result: CompactionResult | None
    aborted: bool
    willRetry: bool
    errorMessage: str | None = None
    type: Literal["compaction_end"] = field(init=False, default="compaction_end")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class AgentSettled(AgentSessionEvent):
    type: Literal["agent_settled"] = field(init=False, default="agent_settled")


AgentSessionEvent.AgentStart = AgentStart
AgentSessionEvent.AgentEnd = AgentEnd
AgentSessionEvent.TurnStart = TurnStart
AgentSessionEvent.TurnEnd = TurnEnd
AgentSessionEvent.MessageStart = MessageStart
AgentSessionEvent.MessageUpdate = MessageUpdate
AgentSessionEvent.MessageEnd = MessageEnd
AgentSessionEvent.ToolExecutionStart = ToolExecutionStart
AgentSessionEvent.ToolExecutionUpdate = ToolExecutionUpdate
AgentSessionEvent.ToolExecutionEnd = ToolExecutionEnd
AgentSessionEvent.CompactionStart = CompactionStart
AgentSessionEvent.CompactionEnd = CompactionEnd
AgentSessionEvent.AgentSettled = AgentSettled


AgentSessionEventListener: TypeAlias = Callable[
    [AgentSessionEvent], None | Awaitable[None]
]


class _SessionListenerRecord:
    __slots__ = ("listener",)

    def __init__(self, listener: AgentSessionEventListener) -> None:
        self.listener = listener


@final
class AgentSession:
    __slots__ = (
        "_agent",
        "_auto_compaction_enabled",
        "_closing",
        "_disposed",
        "_listeners",
        "_model",
        "_operational_cwd",
        "_prompt_active",
        "_prompt_settlement",
        "_session_manager",
        "_system_prompt",
    )

    def __init__(
        self,
        *,
        model: Model,
        session_manager: SessionManager,
        operational_cwd: str,
        agent: Agent,
        _token: object,
    ) -> None:
        if _token is not _SESSION_TOKEN:
            raise TypeError("AgentSession values are factory-produced")
        self._model = model
        self._session_manager = session_manager
        self._operational_cwd = operational_cwd
        self._agent = agent
        self._system_prompt = ""
        self._listeners: list[_SessionListenerRecord] = []
        self._auto_compaction_enabled = True
        self._closing = False
        self._disposed = False
        self._prompt_active = False
        self._prompt_settlement: asyncio.Future[None] | None = None
        self._agent.subscribe(self._handle_agent_event)

    @property
    def model(self) -> Model:
        return self._model

    @property
    def isStreaming(self) -> bool:
        return self._agent.state.isStreaming

    @property
    def isIdle(self) -> bool:
        return not self._agent.state.isStreaming

    @property
    def systemPrompt(self) -> str:
        return self._system_prompt

    @property
    def messages(self) -> tuple[AgentMessage, ...]:
        return self._session_manager.buildSessionContext().messages

    @property
    def sessionId(self) -> str:
        return self._session_manager.getSessionId()

    @property
    def sessionManager(self) -> SessionManager:
        return self._session_manager

    @property
    def sessionFile(self) -> str | None:
        return self._session_manager.getSessionFile()

    @property
    def sessionName(self) -> str | None:
        return self._session_manager.getSessionName()

    @property
    def isCompacting(self) -> bool:
        return False

    def subscribe(self, listener: AgentSessionEventListener) -> Callable[[], None]:
        self._ensure_open()
        if not callable(listener):
            raise TypeError("listener must be callable")
        record = _SessionListenerRecord(listener)
        self._listeners.append(record)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(record)
            except ValueError:
                return

        return unsubscribe

    async def prompt(
        self, text: str, options: PromptOptions | None = None
    ) -> None:
        if type(text) is not str:
            raise TypeError("text must be a string")
        if options is not None and type(options) is not PromptOptions:
            raise TypeError("options must be a PromptOptions")
        self._ensure_open()
        if self._prompt_active:
            raise LifecycleError("busy", "AgentSession is busy")
        self._prompt_active = True
        settlement = asyncio.get_running_loop().create_future()
        self._prompt_settlement = settlement
        try:
            if not self._session_manager.getEntries():
                self._session_manager.appendModelChange(
                    self._model.provider, self._model.id
                )
                self._session_manager.appendThinkingLevelChange("off")
            user = UserMessage(
                content=text,
                timestamp=time.time_ns() // 1_000_000,
            )
            self._session_manager.appendMessage(user)
            try:
                await self._agent.prompt(user)
            finally:
                await self._dispatch(AgentSettled())
        finally:
            self._prompt_active = False
            if not settlement.done():
                settlement.set_result(None)
            if self._prompt_settlement is settlement:
                self._prompt_settlement = None

    async def abort(self) -> None:
        if self._disposed:
            return
        self._agent.abort()
        await self.waitForIdle()

    async def waitForIdle(self) -> None:
        if self._disposed:
            return
        settlement = self._prompt_settlement
        if settlement is None:
            await self._agent.waitForIdle()
        else:
            await asyncio.shield(settlement)

    async def dispose(self) -> None:
        if self._disposed:
            return
        self._closing = True
        self._agent.abort()
        await self.waitForIdle()
        self._listeners.clear()
        self._disposed = True

    async def compact(
        self, customInstructions: str | None = None
    ) -> CompactionResult:
        self._ensure_open()
        if customInstructions is not None and type(customInstructions) is not str:
            raise TypeError("customInstructions must be a string")
        raise ValueError("Session history is too small to compact")

    def abortCompaction(self) -> None:
        return

    @property
    def autoCompactionEnabled(self) -> bool:
        return self._auto_compaction_enabled

    def setAutoCompactionEnabled(self, enabled: bool) -> None:
        self._ensure_open()
        if type(enabled) is not bool:
            raise TypeError("enabled must be a bool")
        self._auto_compaction_enabled = enabled

    def setSessionName(self, name: str) -> None:
        self._ensure_open()
        self._session_manager.appendSessionInfo(name)

    async def navigateTree(
        self, targetId: str, options: object | None = None
    ) -> str | None:
        self._ensure_open()
        if type(targetId) is not str:
            raise TypeError("targetId must be a string")
        if self._session_manager.getEntry(targetId) is None:
            raise ValueError(f"Entry {targetId} not found")
        self._session_manager.branch(targetId)
        return None

    def abortBranchSummary(self) -> None:
        return

    def getUserMessagesForForking(self) -> tuple[tuple[str, str], ...]:
        result: list[tuple[str, str]] = []
        for entry in self._session_manager.getEntries():
            message = getattr(entry, "message", None)
            if isinstance(message, UserMessage):
                content = message.content
                if type(content) is str and content:
                    result.append((entry.id, content))
        return tuple(result)

    async def __aenter__(self) -> AgentSession:
        self._ensure_open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool:
        await self.dispose()
        return False

    def _ensure_open(self) -> None:
        if self._disposed:
            raise LifecycleError("disposed", "AgentSession is disposed")
        if self._closing:
            raise LifecycleError("closing", "AgentSession is closing")

    async def _handle_agent_event(
        self, event: AgentEvent, signal: AbortSignal
    ) -> None:
        del signal
        if isinstance(event, AgentEvent.MessageEnd) and not isinstance(
            event.message, UserMessage
        ):
            self._session_manager.appendMessage(event.message)
        await self._dispatch(_project_event(event))

    async def _dispatch(self, event: AgentSessionEvent) -> None:
        failures: list[BaseException] = []
        for record in tuple(self._listeners):
            try:
                settled = record.listener(event)
                if inspect.isawaitable(settled):
                    await settled
                elif settled is not None:
                    raise TypeError(
                        "AgentSession listener must return None or an awaitable"
                    )
            except BaseException as error:
                failures.append(error)
        if failures:
            raise LifecycleError(
                "listener",
                "AgentSession listener failed",
                causes=tuple(failures),
            ) from failures[0]


def _project_event(event: AgentEvent) -> AgentSessionEvent:
    if isinstance(event, AgentEvent.AgentStart):
        return AgentStart()
    if isinstance(event, AgentEvent.AgentEnd):
        return AgentEnd(messages=event.messages, willRetry=False)
    if isinstance(event, AgentEvent.TurnStart):
        return TurnStart()
    if isinstance(event, AgentEvent.TurnEnd):
        return TurnEnd(message=event.message, toolResults=event.toolResults)
    if isinstance(event, AgentEvent.MessageStart):
        return MessageStart(message=event.message)
    if isinstance(event, AgentEvent.MessageUpdate):
        return MessageUpdate(
            message=event.message,
            assistantMessageEvent=event.assistantMessageEvent,
        )
    if isinstance(event, AgentEvent.MessageEnd):
        return MessageEnd(message=event.message)
    if isinstance(event, AgentEvent.ToolExecutionStart):
        return ToolExecutionStart(
            toolCallId=event.toolCallId,
            toolName=event.toolName,
            args=event.args,
        )
    if isinstance(event, AgentEvent.ToolExecutionUpdate):
        return ToolExecutionUpdate(
            toolCallId=event.toolCallId,
            toolName=event.toolName,
            args=event.args,
            partialResult=event.partialResult,
        )
    if isinstance(event, AgentEvent.ToolExecutionEnd):
        return ToolExecutionEnd(
            toolCallId=event.toolCallId,
            toolName=event.toolName,
            result=event.result,
            isError=event.isError,
        )
    raise TypeError(f"unsupported Agent event: {event.type}")


async def createAgentSession(
    options: CreateAgentSessionOptions | None = None,
) -> CreateAgentSessionResult:
    if options is None:
        selected = CreateAgentSessionOptions()
    elif type(options) is CreateAgentSessionOptions:
        selected = options
    else:
        raise TypeError("options must be a CreateAgentSessionOptions")

    supplied_manager = selected.sessionManager
    if supplied_manager is not None and type(supplied_manager) is not SessionManager:
        raise TypeError("CreateAgentSessionOptions.sessionManager: must be a SessionManager")

    if selected.cwd is not None:
        operational_cwd = _resolve_path(
            selected.cwd, "CreateAgentSessionOptions.cwd"
        )
    elif supplied_manager is not None:
        operational_cwd = supplied_manager.getCwd()
    else:
        operational_cwd = os.path.abspath(os.getcwd())

    models = createModels()
    models.setProvider(deepseekProvider())
    default_model = models.getModel("deepseek", "deepseek-v4-flash")
    assert default_model is not None
    if selected.model is None:
        model = default_model
    elif selected.model is default_model:
        model = selected.model
    else:
        raise ValueError("CreateAgentSessionOptions.model: unsupported Model")

    if await models.getAuth(model) is None:
        raise ModelsError("auth", "DeepSeek authentication is required")

    manager = (
        supplied_manager
        if supplied_manager is not None
        else SessionManager.create(operational_cwd)
    )

    async def stream_fn(
        active_model: Model,
        context: object,
        stream_options: SimpleStreamOptions | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del signal
        async for event in models.streamSimple(
            active_model, cast(Any, context), stream_options
        ):
            yield event

    agent = Agent(
        AgentOptions(
            initialState=AgentState(
                model=model,
                systemPrompt="",
                messages=manager.buildSessionContext().messages,
            ),
            streamFn=stream_fn,
        )
    )
    session = AgentSession(
        model=model,
        session_manager=manager,
        operational_cwd=operational_cwd,
        agent=agent,
        _token=_SESSION_TOKEN,
    )
    try:
        await asyncio.sleep(0)
    except asyncio.CancelledError:
        disposal = asyncio.create_task(session.dispose())
        while not disposal.done():
            try:
                await asyncio.shield(disposal)
            except asyncio.CancelledError:
                continue
        disposal.result()
        raise
    return CreateAgentSessionResult(session=session)
