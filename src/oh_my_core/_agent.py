from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import inspect
import time
from typing import Any, NoReturn, TypeAlias, cast, final

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
    _ListenerFailure,
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
        self._pending_tool_calls = frozenset()


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
    __slots__ = ("control", "task")

    def __init__(self, control: _RunControl) -> None:
        self.control = control
        self.task: asyncio.Task[tuple[AgentMessage, ...]] | None = None


@final
class Agent:
    __slots__ = (
        "_follow_up_queue",
        "_listeners",
        "_run",
        "_state",
        "_steering_queue",
        "_stream_fn",
        "_tool_execution",
    )

    def __init__(self, options: AgentOptions) -> None:
        if type(options) is not AgentOptions:
            raise TypeError("Agent options must be an AgentOptions")
        self._state = options.initialState._copy_seed()
        self._state._owner = self
        self._stream_fn = options.streamFn
        self._tool_execution = options.toolExecution
        self._run: _ActiveRun | None = None
        self._listeners: list[_ListenerRecord] = []
        self._steering_queue: deque[UserMessage] = deque()
        self._follow_up_queue: deque[UserMessage] = deque()

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def signal(self) -> AbortSignal | None:
        run = self._run
        return None if run is None else run.control.signal

    @property
    def hasQueuedMessages(self) -> bool:
        return bool(self._steering_queue or self._follow_up_queue)

    def steer(self, message: UserMessage) -> None:
        if type(message) is not UserMessage:
            raise TypeError("message must be a UserMessage")
        self._steering_queue.append(message)

    def followUp(self, message: UserMessage) -> None:
        if type(message) is not UserMessage:
            raise TypeError("message must be a UserMessage")
        self._follow_up_queue.append(message)

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
        messages = self._state.messages
        if messages and type(messages[-1]) is AssistantMessage:
            steering = self._poll_steering()
            if steering is not None:
                await self._launch(
                    (steering,),
                    continuation=False,
                    skip_initial_steering_poll=True,
                )
                return
            follow_up = self._poll_follow_up()
            if follow_up is not None:
                await self._launch((follow_up,), continuation=False)
                return
        await self._launch((), continuation=True)

    def abort(self) -> None:
        run = self._run
        if run is not None and run.control.requestCancellation():
            task = run.task
            if (
                task is not None
                and task is not asyncio.current_task()
                and not task.done()
            ):
                task.cancel()

    async def waitForIdle(self) -> None:
        run = self._run
        if run is None:
            return
        task = run.task
        if task is None:
            raise RuntimeError("active Agent Run is missing its owner task")
        await asyncio.shield(task)

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
        skip_initial_steering_poll: bool = False,
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
        control = _RunControl(_AbortController().signal)
        run = _ActiveRun(control)
        self._run = run
        state._begin_run()
        task = asyncio.create_task(
            self._drive_run(
                run,
                prompts,
                context,
                config,
                continuation=continuation,
                skip_initial_steering_poll=skip_initial_steering_poll,
            )
        )
        run.task = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            control.requestCancellation()
            if not task.done():
                task.cancel()
            await _settle_operation_task(task, cancellation)

    async def _drive_run(
        self,
        run: _ActiveRun,
        prompts: tuple[AgentMessage, ...],
        context: AgentContext,
        config: AgentLoopConfig,
        *,
        continuation: bool,
        skip_initial_steering_poll: bool = False,
    ) -> tuple[AgentMessage, ...]:
        try:
            return await _run_agent_loop(
                prompts,
                context,
                config,
                self._dispatch,
                self._stream_fn,
                run.control,
                continuation=continuation,
                cancellationResult=True,
                steeringPoll=self._poll_steering,
                followUpPoll=self._poll_follow_up,
                skipInitialSteeringPoll=skip_initial_steering_poll,
            )
        except LifecycleError:
            run.control.requestCancellation()
            raise
        finally:
            if self._run is run:
                self._state._end_run()
                self._run = None

    def _poll_steering(self) -> UserMessage | None:
        if not self._steering_queue:
            return None
        return self._steering_queue.popleft()

    def _poll_follow_up(self) -> UserMessage | None:
        if not self._follow_up_queue:
            return None
        return self._follow_up_queue.popleft()

    async def _dispatch(self, event: AgentEvent) -> None:
        self._reduce(event)
        snapshot = tuple(self._listeners)
        run = self._run
        if run is None:
            raise RuntimeError("Agent listener dispatched without an active Run")
        signal = run.control.signal
        failures: list[BaseException] = []
        for record in snapshot:
            try:
                result = record.listener(event, signal)
                if inspect.isawaitable(result):
                    await result
                elif result is not None:
                    raise TypeError("Agent listener must return None or an awaitable")
            except asyncio.CancelledError as error:
                owner = asyncio.current_task()
                if owner is not None and owner.cancelling():
                    owner.uncancel()
                    continue
                else:
                    failures.append(error)
            except BaseException as error:
                if isinstance(error, LifecycleError) and error.code == "hook":
                    raise
                failures.append(error)
        if failures:
            for failure in failures:
                if isinstance(failure, LifecycleError) and failure.code == "hook":
                    raise failure
            raise _ListenerFailure(tuple(failures))

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


async def _settle_operation_task(
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
    except LifecycleError as lifecycle_failure:
        cancellation.__cause__ = lifecycle_failure
    except asyncio.CancelledError:
        pass
    raise cancellation
