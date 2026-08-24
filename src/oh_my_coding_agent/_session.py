from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
import os
from typing import Any, TypeAlias, cast, final

from oh_my_core import Agent, AgentEvent, AgentMessage, AgentOptions, AgentState
from oh_my_llm import (
    AbortSignal,
    AssistantMessageEvent,
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


AgentSessionEvent: TypeAlias = AgentEvent
AgentSessionEventListener: TypeAlias = Callable[
    [AgentSessionEvent], None | Awaitable[None]
]


@final
class AgentSession:
    __slots__ = (
        "_agent",
        "_auto_compaction_enabled",
        "_disposed",
        "_listeners",
        "_model",
        "_operational_cwd",
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
        self._listeners: list[AgentSessionEventListener] = []
        self._auto_compaction_enabled = True
        self._disposed = False

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
        self._listeners.append(listener)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                return

        return unsubscribe

    async def prompt(
        self, text: str, options: PromptOptions | None = None
    ) -> None:
        self._ensure_open()
        if type(text) is not str:
            raise TypeError("text must be a string")
        if options is not None and type(options) is not PromptOptions:
            raise TypeError("options must be a PromptOptions")
        raise NotImplementedError("Product Session prompts are not implemented")

    async def abort(self) -> None:
        if self._disposed:
            return
        self._agent.abort()
        await self._agent.waitForIdle()

    async def waitForIdle(self) -> None:
        if self._disposed:
            return
        await self._agent.waitForIdle()

    async def dispose(self) -> None:
        if self._disposed:
            return
        self._agent.abort()
        await self._agent.waitForIdle()
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
