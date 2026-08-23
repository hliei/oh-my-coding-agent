from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import inspect
import time
from typing import Any, TypeAlias, cast, final

from oh_my_llm import (
    AbortSignal,
    AssistantMessage,
    LifecycleError,
    Model,
    ToolResultMessage,
    UserMessage,
)
from oh_my_llm._streams import _AbortController

from ._loop import (
    AgentContext,
    AgentEvent,
    AgentLoopConfig,
    AgentMessage,
    StreamFn,
    ToolExecutionMode,
    _RunControl,
    _run_agent_loop,
    _validate_run,
)
from ._tools import AgentTool


_Listener: TypeAlias = Callable[[AgentEvent, AbortSignal], Any]


@final
class AgentState:
    __slots__ = (
        "_error_message",
        "_is_streaming",
        "_messages",
        "_model",
        "_owner",
        "_pending_tool_calls",
        "_streaming_message",
        "_system_prompt",
        "_tools",
    )

    def __init__(
        self,
        *,
        model: Model,
        systemPrompt: str = "",
        tools: Sequence[AgentTool] = (),
        messages: Sequence[AgentMessage] = (),
    ) -> None:
        if not isinstance(model, Model):
            raise TypeError("AgentState.model: must be a Model")
        self._owner: Agent | None = None
        self._model = model
        self._system_prompt = _string(systemPrompt, "AgentState.systemPrompt")
        self._tools = _snapshot_tools(tools)
        self._messages = _snapshot_messages(messages)
        self._is_streaming = False
        self._streaming_message: AssistantMessage | None = None
        self._pending_tool_calls: frozenset[str] = frozenset()
        self._error_message: str | None = None

    @property
    def systemPrompt(self) -> str:
        return self._system_prompt

    @systemPrompt.setter
    def systemPrompt(self, value: str) -> None:
        self._ensure_idle()
        self._system_prompt = _string(value, "AgentState.systemPrompt")

    @property
    def model(self) -> Model:
        return self._model

    @model.setter
    def model(self, value: Model) -> None:
        self._ensure_idle()
        if not isinstance(value, Model):
            raise TypeError("AgentState.model: must be a Model")
        self._model = value

    @property
    def tools(self) -> tuple[AgentTool, ...]:
        return self._tools

    @tools.setter
    def tools(self, value: Sequence[AgentTool]) -> None:
        self._ensure_idle()
        self._tools = _snapshot_tools(value)

    @property
    def messages(self) -> tuple[AgentMessage, ...]:
        return self._messages

    @messages.setter
    def messages(self, value: Sequence[AgentMessage]) -> None:
        self._ensure_idle()
        self._messages = _snapshot_messages(value)

    @property
    def isStreaming(self) -> bool:
        return self._is_streaming

    @property
    def streamingMessage(self) -> AssistantMessage | None:
        return self._streaming_message

    @property
    def pendingToolCalls(self) -> frozenset[str]:
        return self._pending_tool_calls

    @property
    def errorMessage(self) -> str | None:
        return self._error_message

    def _ensure_idle(self) -> None:
        owner = self._owner
        if owner is not None and owner._run is not None:
            raise LifecycleError("busy", "Agent is busy")

    def _copy_seed(self) -> AgentState:
        return AgentState(
            model=self._model,
            systemPrompt=self._system_prompt,
            tools=(*self._tools,),
            messages=(*self._messages,),
        )

    def _begin_run(self) -> None:
        self._is_streaming = True
        self._streaming_message = None
        self._pending_tool_calls = frozenset()
        self._error_message = None

    def _end_run(self) -> None:
        self._is_streaming = False
        self._streaming_message = None


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class AgentOptions:
    initialState: AgentState
    streamFn: StreamFn
    toolExecution: ToolExecutionMode = "parallel"

    def __post_init__(self) -> None:
        if type(self.initialState) is not AgentState:
            raise TypeError("AgentOptions.initialState: must be an AgentState")
        if not callable(self.streamFn):
            raise TypeError("AgentOptions.streamFn: must be callable")
        if self.toolExecution not in ("parallel", "sequential"):
            raise ValueError(
                "AgentOptions.toolExecution: must be parallel or sequential"
            )


class _ListenerRecord:
    __slots__ = ("listener",)

    def __init__(self, listener: _Listener) -> None:
        self.listener = listener


class _ActiveRun:
    __slots__ = ("control", "idle")

    def __init__(self, control: _RunControl, idle: asyncio.Event) -> None:
        self.control = control
        self.idle = idle


@final
class Agent:
    __slots__ = ("_listeners", "_run", "_state", "_stream_fn", "_tool_execution")

    def __init__(self, options: AgentOptions) -> None:
        if type(options) is not AgentOptions:
            raise TypeError("Agent options must be an AgentOptions")
        self._state = options.initialState._copy_seed()
        self._state._owner = self
        self._stream_fn = options.streamFn
        self._tool_execution = options.toolExecution
        self._run: _ActiveRun | None = None
        self._listeners: list[_ListenerRecord] = []

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def signal(self) -> AbortSignal | None:
        run = self._run
        return None if run is None else run.control.signal

    def subscribe(self, listener: _Listener) -> Callable[[], None]:
        if not callable(listener):
            raise TypeError("listener must be callable")
        record = _ListenerRecord(listener)
        self._listeners.append(record)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(record)
            except ValueError:
                return

        return unsubscribe

    async def prompt(
        self, input: str | AgentMessage | Sequence[AgentMessage]
    ) -> None:
        if self._run is not None:
            raise LifecycleError("busy", "Agent is busy")
        prompts = _coerce_prompt(input)
        await self._launch(prompts, continuation=False)

    async def continue_(self) -> None:
        if self._run is not None:
            raise LifecycleError("busy", "Agent is busy")
        await self._launch((), continuation=True)

    def abort(self) -> None:
        run = self._run
        if run is not None:
            run.control.requestCancellation()

    async def waitForIdle(self) -> None:
        run = self._run
        if run is None:
            return
        await run.idle.wait()

    def reset(self) -> None:
        if self._run is not None:
            raise LifecycleError("busy", "Agent is busy")
        self._state._messages = ()
        self._state._error_message = None

    async def _launch(
        self,
        prompts: tuple[AgentMessage, ...],
        *,
        continuation: bool,
    ) -> None:
        state = self._state
        context = AgentContext(
            messages=state.messages,
            systemPrompt=state.systemPrompt,
            tools=state.tools,
        )
        config = AgentLoopConfig(
            model=state.model,
            toolExecution=self._tool_execution,
        )
        _validate_run(
            prompts,
            context,
            config,
            self._stream_fn,
            continuation=continuation,
        )
        idle = asyncio.Event()
        control = _RunControl(_AbortController().signal)
        self._run = _ActiveRun(control, idle)
        state._begin_run()
        try:
            await _run_agent_loop(
                prompts,
                context,
                config,
                self._dispatch,
                self._stream_fn,
                control,
                continuation=continuation,
                cancellationResult=False,
            )
        finally:
            state._end_run()
            self._run = None
            idle.set()

    async def _dispatch(self, event: AgentEvent) -> None:
        self._reduce(event)
        snapshot = tuple(self._listeners)
        run = self._run
        if run is None:
            raise RuntimeError("Agent listener dispatched without an active Run")
        signal = run.control.signal
        for record in snapshot:
            result = record.listener(event, signal)
            if inspect.isawaitable(result):
                await result
            elif result is not None:
                raise TypeError("Agent listener must return None or an awaitable")

    def _reduce(self, event: AgentEvent) -> None:
        state = self._state
        if isinstance(event, (AgentEvent.MessageStart, AgentEvent.MessageUpdate)):
            message = event.message
            if type(message) is AssistantMessage:
                state._streaming_message = message
            return
        if isinstance(event, AgentEvent.MessageEnd):
            state._streaming_message = None
            state._messages = (*state._messages, event.message)
            return
        if isinstance(event, AgentEvent.ToolExecutionStart):
            state._pending_tool_calls = state._pending_tool_calls | {event.toolCallId}
            return
        if isinstance(event, AgentEvent.ToolExecutionEnd):
            state._pending_tool_calls = state._pending_tool_calls - {event.toolCallId}
            return
        if isinstance(event, AgentEvent.TurnEnd):
            message = event.message
            if message.stopReason in ("error", "aborted"):
                state._error_message = message.errorMessage


def _string(value: object, path: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{path}: must be a string")
    return value


def _snapshot_tools(tools: object) -> tuple[AgentTool, ...]:
    if type(tools) not in (list, tuple):
        raise TypeError("AgentState.tools: must be a list or tuple")
    snapshot = tuple(cast(Sequence[object], tools))
    if any(type(tool) is not AgentTool for tool in snapshot):
        raise TypeError("AgentState.tools: must contain AgentTool values")
    return cast(tuple[AgentTool, ...], snapshot)


def _snapshot_messages(messages: object) -> tuple[AgentMessage, ...]:
    if type(messages) not in (list, tuple):
        raise TypeError("AgentState.messages: must be a list or tuple")
    snapshot = tuple(cast(Sequence[object], messages))
    if any(
        not isinstance(message, (UserMessage, AssistantMessage, ToolResultMessage))
        for message in snapshot
    ):
        raise TypeError("AgentState.messages: must contain AgentMessage values")
    return cast(tuple[AgentMessage, ...], snapshot)


def _coerce_prompt(
    prompt: object,
) -> tuple[AgentMessage, ...]:
    if type(prompt) is str:
        return (
            UserMessage(
                content=prompt,
                timestamp=time.time_ns() // 1_000_000,
            ),
        )
    if isinstance(prompt, (UserMessage, AssistantMessage, ToolResultMessage)):
        return (prompt,)
    if type(prompt) not in (list, tuple):
        raise TypeError(
            "prompt must be a string, AgentMessage, or list or tuple of AgentMessage values"
        )
    if not prompt:
        raise ValueError("prompt messages must not be empty")
    messages = tuple(cast(Sequence[object], prompt))
    if any(
        not isinstance(message, (UserMessage, AssistantMessage, ToolResultMessage))
        for message in messages
    ):
        raise TypeError("prompt messages must contain AgentMessage values")
    return cast(tuple[AgentMessage, ...], messages)
