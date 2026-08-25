from __future__ import annotations

import asyncio
import builtins
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
import inspect
import os
import time
from typing import Any, ClassVar, Literal, TypeAlias, TypeVar, cast, final

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
    Context,
    JSONValue,
    LifecycleError,
    Model,
    Models,
    ModelsError,
    SimpleStreamOptions,
    StreamOptions,
    TextContent,
    UserMessage,
    createModels,
)
from oh_my_llm.providers.deepseek import (
    _CONTEXT_OVERFLOW_ERROR,
    deepseekProvider,
)

from ._extensions import (
    ExtensionContext,
    ExtensionRuntime,
    load_extensions,
    snapshot_extension_context,
)
from ._prompt_resources import (
    PromptResourceSnapshot,
    expand_prompt,
    load_prompt_resources,
)
from ._system_prompt import build_system_prompt
from ._tools import product_session_tools
from ._compaction import (
    CONTEXT_WINDOW,
    PREFIX_SUMMARY_MAX_TOKENS,
    RESERVE_TOKENS,
    SUMMARY_MAX_TOKENS,
    SUMMARIZATION_SYSTEM_PROMPT,
    CompactionPreparation,
    append_file_operations,
    build_summary_prompt,
    build_turn_prefix_prompt,
    estimate_context_tokens,
    prepare_compaction,
)
from ._session_manager import (
    CompactionEntry,
    CompactionResult,
    SessionMessageEntry,
    SessionManager,
    _resolve_path,
)


_SESSION_TOKEN = object()
_T = TypeVar("_T")
_ACTIVE_SESSION_DISPATCH: ContextVar[frozenset[int]] = ContextVar(
    "omh_active_session_dispatch", default=frozenset()
)


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class CreateAgentSessionOptions:
    cwd: str | None = None
    model: Model | None = None
    sessionManager: SessionManager | None = None
    projectTrusted: bool = False


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
        "_agent_unsubscribe",
        "_auto_compaction_enabled",
        "_closing",
        "_compaction_cancel_requested",
        "_compaction_result_committed",
        "_compaction_task",
        "_compaction_terminal_committed",
        "_disposal_task",
        "_disposed",
        "_extensions",
        "_listeners",
        "_manager_failure",
        "_model",
        "_models",
        "_operational_cwd",
        "_overflow_recovery_attempted",
        "_pending_agent_end",
        "_pending_agent_end_signal",
        "_last_run_signal",
        "_projecting_agent_end",
        "_prompt_active",
        "_prompt_cancel_requested",
        "_prompt_settlement",
        "_prompt_resources",
        "_prompt_terminal_projection_truncated",
        "_session_manager",
        "_system_prompt",
    )

    def __init__(
        self,
        *,
        model: Model,
        models: Models,
        session_manager: SessionManager,
        operational_cwd: str,
        agent: Agent,
        prompt_resources: PromptResourceSnapshot,
        extensions: ExtensionRuntime,
        _token: object,
    ) -> None:
        if _token is not _SESSION_TOKEN:
            raise TypeError("AgentSession values are factory-produced")
        self._model = model
        self._models = models
        self._session_manager = session_manager
        self._operational_cwd = operational_cwd
        self._prompt_resources = prompt_resources
        self._extensions = extensions
        self._overflow_recovery_attempted = False
        self._pending_agent_end: AgentEvent | None = None
        self._pending_agent_end_signal: AbortSignal | None = None
        self._last_run_signal: AbortSignal | None = None
        self._projecting_agent_end = False
        self._agent = agent
        self._system_prompt = agent.state.systemPrompt
        self._listeners: list[_SessionListenerRecord] = []
        self._manager_failure: BaseException | None = None
        self._auto_compaction_enabled = True
        self._compaction_cancel_requested = False
        self._compaction_result_committed = False
        self._compaction_task: asyncio.Task[Any] | None = None
        self._compaction_terminal_committed = False
        self._closing = False
        self._disposed = False
        self._prompt_active = False
        self._prompt_cancel_requested = False
        self._prompt_settlement: asyncio.Future[None] | None = None
        self._prompt_terminal_projection_truncated = False
        self._disposal_task: asyncio.Task[None] | None = None
        self._agent_unsubscribe: Callable[[], None] | None = self._agent.subscribe(
            self._handle_agent_event
        )

    @property
    def model(self) -> Model:
        return self._model

    @property
    def isStreaming(self) -> bool:
        return self._projecting_agent_end or self._agent.state.isStreaming

    @property
    def isIdle(self) -> bool:
        if self._disposed:
            return True
        return (
            not self._closing
            and not self.isCompacting
            and not self.isStreaming
        )

    @property
    def systemPrompt(self) -> str:
        return self._system_prompt

    @property
    def messages(self) -> tuple[AgentMessage, ...]:
        return self._agent.state.messages

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
        task = self._compaction_task
        return task is not None and not task.done()

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
        self._ensure_not_reentrant()
        if type(text) is not str:
            raise TypeError("text must be a string")
        if options is not None and type(options) is not PromptOptions:
            raise TypeError("options must be a PromptOptions")
        self._ensure_open()
        if self._prompt_active or self.isCompacting:
            raise LifecycleError("busy", "AgentSession is busy")
        expand = True if options is None else options.expandPromptTemplates
        text = expand_prompt(text, self._prompt_resources, enabled=expand)
        self._prompt_active = True
        self._prompt_cancel_requested = False
        self._prompt_terminal_projection_truncated = False
        self._overflow_recovery_attempted = False
        self._manager_failure = None
        settlement = asyncio.get_running_loop().create_future()
        settlement.add_done_callback(_consume_future_exception)
        self._prompt_settlement = settlement
        settlement_failure: BaseException | None = None
        agent_run_started = False
        try:
            if not await self._run_pre_prompt_compaction():
                return
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
                agent_run_started = True
                await self._agent.prompt(user)
            except asyncio.CancelledError as cancellation:
                lifecycle_failure = cancellation.__cause__
                if isinstance(lifecycle_failure, LifecycleError):
                    settlement_failure = lifecycle_failure
                elif agent_run_started:
                    try:
                        await self._dispatch_pending_agent_end(will_retry=False)
                        await self._dispatch(
                            AgentSettled(),
                            agent_signal=self._last_run_signal,
                        )
                    except BaseException as error:
                        settlement_failure = error
                        cancellation.__cause__ = error
                raise
            except BaseException as error:
                manager_failure = self._manager_failure
                settlement_failure = (
                    error if manager_failure is None else manager_failure
                )
                if manager_failure is not None:
                    raise manager_failure from error
                raise
            else:
                await self._run_automatic_compaction()
                if not self._prompt_terminal_projection_truncated:
                    await self._dispatch(
                        AgentSettled(),
                        agent_signal=self._last_run_signal,
                    )
        except asyncio.CancelledError as cancellation:
            if self._pending_agent_end is not None:
                try:
                    await self._dispatch_pending_agent_end(will_retry=False)
                    await self._dispatch(
                        AgentSettled(),
                        agent_signal=self._last_run_signal,
                    )
                except BaseException as error:
                    settlement_failure = error
                    cancellation.__cause__ = error
            raise
        except BaseException as error:
            if settlement_failure is None and not isinstance(
                error, asyncio.CancelledError
            ):
                settlement_failure = error
            raise
        finally:
            self._prompt_active = False
            self._compaction_result_committed = False
            self._prompt_terminal_projection_truncated = False
            if not settlement.done():
                if settlement_failure is None:
                    settlement.set_result(None)
                else:
                    settlement.set_exception(settlement_failure)
            if self._prompt_settlement is settlement:
                self._prompt_settlement = None

    async def abort(self) -> None:
        self._ensure_not_reentrant()
        if self._disposed:
            return
        disposal = self._disposal_task
        if disposal is not None:
            await _join_task(disposal)
            return
        settlement = self._prompt_settlement
        self._request_active_prompt_cancel()
        if settlement is None:
            return
        try:
            await asyncio.shield(settlement)
        except asyncio.CancelledError as cancellation:
            await _finish_settlement_before_cancellation(settlement, cancellation)

    async def waitForIdle(self) -> None:
        self._ensure_not_reentrant()
        if self._disposed:
            return
        disposal = self._disposal_task
        if disposal is not None:
            await _join_task(disposal, cancel_owner=self._request_active_prompt_cancel)
            return
        settlement = self._prompt_settlement
        if settlement is not None:
            try:
                await asyncio.shield(settlement)
            except asyncio.CancelledError as cancellation:
                self._request_active_prompt_cancel()
                await _finish_settlement_before_cancellation(
                    settlement, cancellation
                )
            return
        compaction = self._compaction_task
        if compaction is not None:
            try:
                await _join_task(
                    compaction, cancel_owner=self._request_compaction_cancel
                )
            except asyncio.CancelledError:
                owner = asyncio.current_task()
                if compaction.cancelled() and (
                    owner is None or owner.cancelling() == 0
                ):
                    return
                raise
            return
        await self._agent.waitForIdle()

    async def dispose(self) -> None:
        self._ensure_not_reentrant()
        if self._disposed:
            return
        attempt = self._disposal_task
        if attempt is None:
            self._closing = True
            attempt = asyncio.create_task(self._drive_disposal())
            attempt.add_done_callback(_consume_task_exception)
            self._disposal_task = attempt
        try:
            await _join_task(attempt)
        finally:
            if attempt.done() and self._disposal_task is attempt:
                self._disposal_task = None

    async def _drive_disposal(self) -> None:
        disposal_failures: list[BaseException] = []
        compaction = self._compaction_task
        if compaction is not None and not compaction.done():
            self._request_compaction_cancel()
            try:
                await asyncio.shield(compaction)
            except asyncio.CancelledError:
                pass
            except BaseException as error:
                disposal_failures.append(error)
        settlement = self._prompt_settlement
        self._agent.abort()
        settlement_failure: BaseException | None = None
        if settlement is not None:
            try:
                await asyncio.shield(settlement)
            except BaseException as error:
                settlement_failure = error
        else:
            try:
                await self._agent.waitForIdle()
            except BaseException as error:
                settlement_failure = error

        try:
            shutdown_failures = await self._extensions.shutdown(
                self._extension_context(None)
            )
        except BaseException as error:
            shutdown_failures = [error]
        disposal_failures.extend(shutdown_failures)

        unsubscribe = self._agent_unsubscribe
        if unsubscribe is not None:
            try:
                unsubscribe()
            except BaseException as error:
                disposal_failures.append(error)
            else:
                self._agent_unsubscribe = None
        if disposal_failures:
            raise LifecycleError(
                "disposal",
                "AgentSession disposal failed",
                causes=tuple(disposal_failures),
            )
        self._listeners.clear()
        self._pending_agent_end = None
        self._pending_agent_end_signal = None
        self._extensions.release()
        self._disposed = True
        if settlement_failure is not None:
            raise settlement_failure

    async def compact(
        self, customInstructions: str | None = None
    ) -> CompactionResult:
        self._ensure_not_reentrant()
        self._ensure_open()
        if customInstructions is not None and type(customInstructions) is not str:
            raise TypeError("customInstructions must be a string")
        if self.isCompacting:
            raise LifecycleError("busy", "AgentSession is busy")
        task = asyncio.create_task(self._drive_manual_compaction(customInstructions))
        task.add_done_callback(_consume_task_exception)
        self._compaction_cancel_requested = False
        self._compaction_result_committed = False
        self._compaction_terminal_committed = False
        self._compaction_task = task
        try:
            return await _join_task(task, cancel_owner=self._request_compaction_cancel)
        finally:
            if task.done() and self._compaction_task is task:
                self._compaction_task = None
                self._compaction_cancel_requested = False
                self._compaction_result_committed = False
                self._compaction_terminal_committed = False

    def abortCompaction(self) -> None:
        self._request_compaction_cancel()

    def _request_compaction_cancel(self) -> None:
        task = self._compaction_task
        if (
            task is not None
            and not task.done()
            and not self._compaction_terminal_committed
        ):
            self._compaction_cancel_requested = True
            task.cancel()

    def _request_active_prompt_cancel(self) -> None:
        if self._prompt_settlement is not None:
            self._prompt_cancel_requested = True
        if self._compaction_result_committed:
            self._pending_agent_end = None
            self._prompt_terminal_projection_truncated = True
        self._agent.abort()
        self._request_compaction_cancel()

    async def _drive_manual_compaction(
        self, custom_instructions: str | None
    ) -> CompactionResult:
        settlement = self._prompt_settlement
        self._agent.abort()
        if settlement is not None:
            try:
                await asyncio.shield(settlement)
            except asyncio.CancelledError as cancellation:
                await _finish_settlement_before_cancellation(
                    settlement, cancellation
                )
        else:
            await self._agent.waitForIdle()
        preparation = prepare_compaction(self._session_manager)
        if preparation is None:
            raise ValueError("Session history is too small to compact")
        return await self._perform_compaction(
            preparation,
            custom_instructions=custom_instructions,
            reason="manual",
            will_retry=False,
        )

    async def _run_automatic_compaction(
        self, *, allow_overflow_retry: bool = True
    ) -> None:
        messages = self._agent.state.messages
        if (
            not allow_overflow_retry
            and self._overflow_recovery_attempted
            and messages
            and _is_context_overflow_error(messages[-1])
        ):
            await self._report_exhausted_overflow()
            return
        candidate = self._automatic_compaction_candidate(
            include_aborted=False,
            allow_overflow_retry=allow_overflow_retry,
        )
        if candidate is None:
            await self._dispatch_pending_agent_end(will_retry=False)
            return
        assistant, preparation, reason, will_retry = candidate
        retryable_overflow = reason == "overflow" and assistant.stopReason != "stop"
        if (
            retryable_overflow
            and self._overflow_recovery_attempted
            and not allow_overflow_retry
        ):
            await self._report_exhausted_overflow()
            return
        if will_retry:
            self._overflow_recovery_attempted = True
        succeeded = await self._run_automatic_compaction_task(
            preparation, reason=reason, will_retry=will_retry
        )
        if self._prompt_terminal_projection_truncated:
            self._compaction_result_committed = False
            return
        await self._dispatch_pending_agent_end(will_retry=will_retry and succeeded)
        if self._prompt_terminal_projection_truncated:
            self._compaction_result_committed = False
            return
        if not succeeded:
            self._compaction_result_committed = False
            return
        if will_retry:
            self._compaction_result_committed = False
            self._remove_trailing_overflow_error()
            await self._agent.continue_()
            await self._run_automatic_compaction(allow_overflow_retry=False)
            return
        self._compaction_result_committed = False

    async def _report_exhausted_overflow(self) -> None:
        await self._dispatch(
            CompactionEnd(
                reason="overflow",
                result=None,
                aborted=False,
                willRetry=False,
                errorMessage=(
                    "Context overflow recovery failed after one "
                    "compact-and-retry attempt. Try reducing context or "
                    "switching to a larger-context model."
                ),
            )
        )
        await self._dispatch_pending_agent_end(will_retry=False)

    async def _run_pre_prompt_compaction(self) -> bool:
        candidate = self._automatic_compaction_candidate(
            include_aborted=True,
            allow_overflow_retry=False,
        )
        if candidate is None:
            return not self._closing
        assistant, preparation, reason, _will_retry = candidate
        succeeded = await self._run_automatic_compaction_task(
            preparation, reason=reason, will_retry=False
        )
        if succeeded and _is_context_overflow_error(assistant):
            self._remove_trailing_overflow_error()
        return not self._closing and not self._prompt_cancel_requested

    def _automatic_compaction_candidate(
        self,
        *,
        include_aborted: bool,
        allow_overflow_retry: bool,
    ) -> tuple[
        AssistantMessage,
        CompactionPreparation,
        Literal["threshold", "overflow"],
        bool,
    ] | None:
        if not self._auto_compaction_enabled:
            return None
        messages = self._agent.state.messages
        if not messages or not isinstance(messages[-1], AssistantMessage):
            return None
        assistant = messages[-1]
        if assistant.stopReason == "aborted" and not include_aborted:
            return None
        if self._message_is_before_latest_compaction(assistant):
            return None
        overflow_error = _is_context_overflow_error(assistant)
        context_tokens, last_usage_index = estimate_context_tokens(messages)
        if assistant.stopReason == "error" and not overflow_error:
            if last_usage_index is None:
                return None
            usage_message = messages[last_usage_index]
            if self._message_is_before_latest_compaction(usage_message):
                return None
        if overflow_error:
            reason: Literal["threshold", "overflow"] = "overflow"
        elif context_tokens > CONTEXT_WINDOW - RESERVE_TOKENS:
            reason = "threshold"
        else:
            return None
        preparation = prepare_compaction(self._session_manager)
        if preparation is None:
            return None
        will_retry = (
            overflow_error and allow_overflow_retry
        )
        return assistant, preparation, reason, will_retry

    async def _run_automatic_compaction_task(
        self,
        preparation: CompactionPreparation,
        *,
        reason: Literal["threshold", "overflow"],
        will_retry: bool,
    ) -> bool:
        task = asyncio.create_task(
            self._perform_compaction(
                preparation,
                custom_instructions=None,
                reason=reason,
                will_retry=will_retry,
            )
        )
        task.add_done_callback(_consume_task_exception)
        self._compaction_cancel_requested = False
        self._compaction_result_committed = False
        self._compaction_terminal_committed = False
        self._compaction_task = task
        try:
            await _join_task(task, cancel_owner=self._request_compaction_cancel)
        except asyncio.CancelledError:
            owner = asyncio.current_task()
            if self._compaction_result_committed:
                self._pending_agent_end = None
                self._prompt_terminal_projection_truncated = True
                raise
            if (
                task.cancelled()
                and self._compaction_cancel_requested
                and (owner is None or owner.cancelling() == 0)
            ):
                return False
            raise
        except ModelsError as error:
            caused = error.__cause__
            if isinstance(caused, LifecycleError) and caused.code == "listener":
                self._pending_agent_end = None
                error.__cause__ = None
                raise caused from error
            return False
        except BaseException as error:
            self._agent.state.messages = (
                self._session_manager.buildSessionContext().messages
            )
            direct_listener_failure = (
                isinstance(error, LifecycleError) and error.code == "listener"
            )
            caused = error.__cause__
            caused_listener_failure = (
                isinstance(caused, LifecycleError) and caused.code == "listener"
            )
            if direct_listener_failure or caused_listener_failure:
                self._pending_agent_end = None
                raise
            if self._pending_agent_end is not None:
                try:
                    await self._dispatch_pending_agent_end(will_retry=False)
                except BaseException as projection_failure:
                    error.__cause__ = projection_failure
            raise
        finally:
            if self._compaction_task is task:
                self._compaction_task = None
                self._compaction_cancel_requested = False
                self._compaction_terminal_committed = False
        return True

    def _remove_trailing_overflow_error(self) -> None:
        active_context = self._agent.state.messages
        if active_context and _is_context_overflow_error(active_context[-1]):
            self._agent.state.messages = active_context[:-1]

    def _message_is_before_latest_compaction(self, message: AgentMessage) -> bool:
        branch = self._session_manager.getBranch()
        compaction_index: int | None = None
        for index in range(len(branch) - 1, -1, -1):
            if isinstance(branch[index], CompactionEntry):
                compaction_index = index
                break
        if compaction_index is None:
            return False
        for index, entry in enumerate(branch):
            if isinstance(entry, SessionMessageEntry) and entry.message is message:
                return index < compaction_index
        compaction = branch[compaction_index]
        try:
            compaction_timestamp = int(
                datetime.fromisoformat(
                    compaction.timestamp.replace("Z", "+00:00")
                ).timestamp()
                * 1000
            )
        except ValueError:
            return False
        return message.timestamp < compaction_timestamp

    async def _perform_compaction(
        self,
        preparation: CompactionPreparation,
        *,
        custom_instructions: str | None,
        reason: Literal["manual", "threshold", "overflow"],
        will_retry: bool,
    ) -> CompactionResult:
        try:
            await self._dispatch(CompactionStart(reason=reason))
        except asyncio.CancelledError:
            await self._commit_aborted_compaction(reason)
            raise
        result: CompactionResult
        try:
            owner = asyncio.current_task()
            if owner is not None and owner.cancelling():
                raise asyncio.CancelledError
            if preparation.turn_prefix_messages:
                history_summary = (
                    await self._complete_compaction_summary(
                        build_summary_prompt(preparation, custom_instructions),
                        max_tokens=SUMMARY_MAX_TOKENS,
                    )
                    if preparation.messages_to_summarize
                    else "No prior history."
                )
                prefix_summary = await self._complete_compaction_summary(
                    build_turn_prefix_prompt(preparation),
                    max_tokens=PREFIX_SUMMARY_MAX_TOKENS,
                )
                summary = (
                    f"{history_summary}\n\n---\n\n"
                    "**Turn Context (split turn):**\n\n"
                    f"{prefix_summary}"
                )
            else:
                summary = await self._complete_compaction_summary(
                    build_summary_prompt(preparation, custom_instructions),
                    max_tokens=SUMMARY_MAX_TOKENS,
                )
            summary = append_file_operations(summary, preparation)
            details = {
                "readFiles": list(preparation.read_files),
                "modifiedFiles": list(preparation.modified_files),
            }
            self._session_manager.appendCompaction(
                summary,
                preparation.first_kept_entry_id,
                preparation.tokens_before,
                details,
            )
            context = self._session_manager.buildSessionContext().messages
            self._agent.state.messages = context
            estimated_after, _ = estimate_context_tokens(context)
            result = CompactionResult(
                summary=summary,
                firstKeptEntryId=preparation.first_kept_entry_id,
                tokensBefore=preparation.tokens_before,
                estimatedTokensAfter=estimated_after,
                details=details,
            )
        except asyncio.CancelledError:
            await self._commit_aborted_compaction(reason)
            raise
        except BaseException as error:
            self._agent.state.messages = (
                self._session_manager.buildSessionContext().messages
            )
            self._compaction_terminal_committed = True
            try:
                await self._dispatch(
                    CompactionEnd(
                        reason=reason,
                        result=None,
                        aborted=False,
                        willRetry=False,
                        errorMessage=f"Compaction failed: {error}",
                    )
                )
            except BaseException as projection_failure:
                error.__cause__ = projection_failure
            raise
        self._compaction_terminal_committed = True
        self._compaction_result_committed = True
        await self._dispatch(
            CompactionEnd(
                reason=reason,
                result=result,
                aborted=False,
                willRetry=will_retry,
            )
        )
        return result

    async def _commit_aborted_compaction(
        self, reason: Literal["manual", "threshold", "overflow"]
    ) -> None:
        self._compaction_terminal_committed = True
        await self._dispatch(
            CompactionEnd(
                reason=reason,
                result=None,
                aborted=True,
                willRetry=False,
            )
        )

    async def _complete_compaction_summary(
        self, prompt_text: str, *, max_tokens: int
    ) -> str:
        prompt = UserMessage(
            content=prompt_text,
            timestamp=time.time_ns() // 1_000_000,
        )
        response = await self._models.completeSimple(
            self._model,
            Context(messages=(prompt,), systemPrompt=SUMMARIZATION_SYSTEM_PROMPT),
            StreamOptions(maxTokens=max_tokens),
        )
        if response.stopReason == "error":
            raise ModelsError("provider", "Compaction failed")
        return "\n".join(
            item.text for item in response.content if isinstance(item, TextContent)
        )

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
        self._ensure_not_reentrant()
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
        try:
            await self.dispose()
        except BaseException as disposal_failure:
            if exc is None:
                raise
            disposal_failure.__context__ = exc.__context__
            exc.__context__ = disposal_failure
        return False

    def _ensure_open(self) -> None:
        if self._disposed:
            raise LifecycleError("disposed", "AgentSession is disposed")
        if self._closing:
            raise LifecycleError("closing", "AgentSession is closing")

    def _ensure_not_reentrant(self) -> None:
        if id(self) in _ACTIVE_SESSION_DISPATCH.get():
            raise LifecycleError(
                "reentrant", "AgentSession operation cannot wait from its listener"
            )

    async def _handle_agent_event(
        self, event: AgentEvent, signal: AbortSignal
    ) -> None:
        if isinstance(event, AgentEvent.MessageEnd) and not isinstance(
            event.message, UserMessage
        ):
            try:
                self._session_manager.appendMessage(event.message)
            except BaseException as error:
                self._manager_failure = error
                raise
        if isinstance(event, AgentEvent.AgentEnd):
            if self._pending_agent_end is not None:
                raise RuntimeError("AgentSession has an unprojected AgentEnd")
            self._pending_agent_end = event
            self._pending_agent_end_signal = signal
            self._last_run_signal = signal
            return
        self._last_run_signal = signal
        await self._dispatch(
            _project_event(event),
            agent_signal=signal,
        )

    async def _dispatch_pending_agent_end(self, *, will_retry: bool) -> None:
        event = self._pending_agent_end
        if event is None:
            return
        if not isinstance(event, AgentEvent.AgentEnd):
            raise RuntimeError("AgentSession pending event is not AgentEnd")
        self._pending_agent_end = None
        self._projecting_agent_end = True
        try:
            await self._dispatch(
                _project_event(event, will_retry=will_retry),
                agent_signal=self._pending_agent_end_signal,
            )
        finally:
            self._projecting_agent_end = False
            self._pending_agent_end_signal = None

    async def _dispatch(
        self,
        event: AgentSessionEvent,
        *,
        agent_signal: AbortSignal | None = None,
    ) -> None:
        active = _ACTIVE_SESSION_DISPATCH.get()
        token = _ACTIVE_SESSION_DISPATCH.set(active | {id(self)})
        failures: list[BaseException] = []
        owner_cancellation: asyncio.CancelledError | None = None
        try:
            try:
                await self._extensions.dispatch(
                    event,
                    self._extension_context(agent_signal),
                )
            except asyncio.CancelledError as error:
                owner = asyncio.current_task()
                if owner is not None and owner.cancelling():
                    owner.uncancel()
                    if agent_signal is None or not agent_signal.aborted:
                        owner_cancellation = error
                else:
                    raise LifecycleError(
                        "hook",
                        "Python Extension handler failed",
                        causes=(error,),
                    ) from error
            else:
                for record in tuple(self._listeners):
                    try:
                        settled = record.listener(event)
                        if inspect.isawaitable(settled):
                            await settled
                        elif settled is not None:
                            raise TypeError(
                                "AgentSession listener must return None or an awaitable"
                            )
                    except asyncio.CancelledError as error:
                        owner = asyncio.current_task()
                        if owner is not None and owner.cancelling():
                            owner.uncancel()
                            if agent_signal is None or not agent_signal.aborted:
                                owner_cancellation = error
                        else:
                            failures.append(error)
                    except BaseException as error:
                        failures.append(error)
        finally:
            _ACTIVE_SESSION_DISPATCH.reset(token)
        if owner_cancellation is not None:
            if agent_signal is not None:
                self._agent.abort()
            else:
                raise owner_cancellation
        if failures:
            raise LifecycleError(
                "listener",
                "AgentSession listener failed",
                causes=tuple(failures),
            ) from failures[0]

    def _extension_context(
        self, signal: AbortSignal | None
    ) -> ExtensionContext:
        return snapshot_extension_context(
            cwd=self._operational_cwd,
            session_id=self.sessionId,
            model=self._model,
            messages=self._session_manager.buildSessionContext().messages,
            signal=signal,
        )


def _project_event(
    event: AgentEvent, *, will_retry: bool = False
) -> AgentSessionEvent:
    if isinstance(event, AgentEvent.AgentStart):
        return AgentStart()
    if isinstance(event, AgentEvent.AgentEnd):
        return AgentEnd(messages=event.messages, willRetry=will_retry)
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


def _is_context_overflow_error(message: AgentMessage) -> bool:
    return (
        isinstance(message, AssistantMessage)
        and message.stopReason == "error"
        and message.errorMessage == _CONTEXT_OVERFLOW_ERROR
    )


async def _finish_settlement_before_cancellation(
    settlement: asyncio.Future[None],
    cancellation: asyncio.CancelledError,
) -> None:
    while not settlement.done():
        try:
            await asyncio.shield(settlement)
        except asyncio.CancelledError:
            continue
    try:
        settlement.result()
    except BaseException as failure:
        cancellation.__cause__ = failure
    raise cancellation


def _consume_future_exception(settlement: asyncio.Future[None]) -> None:
    if not settlement.cancelled():
        settlement.exception()


async def _join_task(
    task: asyncio.Task[_T],
    *,
    cancel_owner: Callable[[], None] | None = None,
) -> _T:
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        if task.done() and task.cancelled():
            raise
        if cancel_owner is not None:
            cancel_owner()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        try:
            task.result()
        except BaseException as failure:
            cancellation.__cause__ = failure
        raise cancellation


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


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
    if type(selected.projectTrusted) is not bool:
        raise TypeError("CreateAgentSessionOptions.projectTrusted: must be a bool")

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

    prompt_resources = load_prompt_resources(operational_cwd, selected.projectTrusted)
    extensions = await load_extensions(operational_cwd, selected.projectTrusted)

    manager = (
        supplied_manager
        if supplied_manager is not None
        else SessionManager.create(operational_cwd)
    )
    start_context = snapshot_extension_context(
        cwd=operational_cwd,
        session_id=manager.getSessionId(),
        model=model,
        messages=manager.buildSessionContext().messages,
        signal=None,
    )
    await extensions.start(start_context)
    try:
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

        system_prompt = build_system_prompt(
            operational_cwd, prompt_resources, extensions.tool_summaries()
        )
        agent = Agent(
            AgentOptions(
                initialState=AgentState(
                    model=model,
                    systemPrompt=system_prompt,
                    tools=(
                        *product_session_tools(operational_cwd),
                        *extensions.tools,
                    ),
                    messages=manager.buildSessionContext().messages,
                ),
                streamFn=stream_fn,
                toolExecution="parallel",
            )
        )
        session = AgentSession(
            model=model,
            models=models,
            session_manager=manager,
            operational_cwd=operational_cwd,
            agent=agent,
            prompt_resources=prompt_resources,
            extensions=extensions,
            _token=_SESSION_TOKEN,
        )
    except BaseException as error:
        shutdown_errors = await extensions.shutdown(start_context)
        if isinstance(error, asyncio.CancelledError):
            if shutdown_errors:
                error.__cause__ = shutdown_errors[0]
            raise
        if shutdown_errors and isinstance(error, LifecycleError):
            raise LifecycleError(
                error.code,
                str(error),
                causes=(*error.causes, *shutdown_errors),
            ) from error
        raise
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
