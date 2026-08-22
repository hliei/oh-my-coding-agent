from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from types import TracebackType
from typing import Any, Generic, NoReturn, TypeVar, final

from ._errors import LifecycleError


EventT = TypeVar("EventT")
ResultT = TypeVar("ResultT")
_END = object()
_ACTIVE_ABORT_SIGNAL: ContextVar[AbortSignal | None] = ContextVar(
    "omh_active_abort_signal", default=None
)


@final
class AbortSignal:
    __slots__ = ("_aborted", "_event")
    _aborted: bool
    _event: asyncio.Event

    def __init__(self) -> None:
        raise TypeError("AbortSignal values are factory-produced")

    @property
    def aborted(self) -> bool:
        return self._aborted

    async def wait(self) -> None:
        await self._event.wait()


@final
class _AbortController:
    __slots__ = ("signal",)

    def __init__(self) -> None:
        signal = object.__new__(AbortSignal)
        signal._aborted = False
        signal._event = asyncio.Event()
        self.signal = signal

    def abort(self) -> None:
        _abort_signal(self.signal)


def _abort_signal(signal: AbortSignal) -> None:
    if not signal._aborted:
        signal._aborted = True
        signal._event.set()


@contextmanager
def _bind_abort_signal(signal: AbortSignal) -> Iterator[None]:
    token = _ACTIVE_ABORT_SIGNAL.set(signal)
    try:
        yield
    finally:
        _ACTIVE_ABORT_SIGNAL.reset(token)


def _active_abort_signal() -> AbortSignal | None:
    return _ACTIVE_ABORT_SIGNAL.get()


_EventSink = Callable[[EventT], Awaitable[None]]
_Producer = Callable[[_EventSink[EventT], AbortSignal], Awaitable[ResultT]]


@final
class EventStream(Generic[EventT, ResultT], AsyncIterator[EventT]):
    __slots__ = (
        "_activated",
        "_available",
        "_closed",
        "_consumerClaimed",
        "_consumerTask",
        "_controller",
        "_discardEvents",
        "_producer",
        "_queue",
        "_result",
        "_task",
    )

    def __init__(self) -> None:
        raise TypeError("EventStream values are factory-produced")

    def _initialize(self, producer: _Producer[EventT, ResultT]) -> None:
        self._producer = producer
        self._activated = False
        self._closed = False
        self._consumerClaimed = False
        self._consumerTask: asyncio.Task[object] | None = None
        self._discardEvents = False
        self._controller = _AbortController()
        self._queue: deque[EventT | object] = deque()
        self._available = asyncio.Event()
        self._result: asyncio.Future[ResultT] | None = None
        self._task: asyncio.Task[None] | None = None

    def _activate(self) -> None:
        if self._closed and not self._activated:
            raise LifecycleError("consumer", "EventStream was closed before activation")
        if self._activated:
            return
        loop = asyncio.get_running_loop()
        self._activated = True
        self._result = loop.create_future()
        self._result.add_done_callback(_observe_future_exception)
        self._task = loop.create_task(self._run())

    async def _run(self) -> None:
        assert self._result is not None

        async def emit(event: EventT) -> None:
            if not self._discardEvents:
                self._queue.append(event)
                self._available.set()

        try:
            result = await self._producer(emit, self._controller.signal)
        except BaseException as error:
            if not self._result.done():
                self._result.set_exception(error)
        else:
            if not self._result.done():
                self._result.set_result(result)
        finally:
            if not self._discardEvents:
                self._queue.append(_END)
                self._available.set()

    def __aiter__(self) -> EventStream[EventT, ResultT]:
        if self._consumerClaimed:
            raise LifecycleError("consumer", "EventStream permits only one consumer")
        self._claim_consumer()
        return self

    async def __anext__(self) -> EventT:
        self._claim_consumer()
        if self._closed:
            raise StopAsyncIteration
        self._activate()
        try:
            while not self._queue:
                self._available.clear()
                await self._available.wait()
        except asyncio.CancelledError as cancellation:
            self._discardEvents = True
            self._queue.clear()
            await self._settle_cancelled_observer(cancellation)
        item = self._queue.popleft()
        if not self._queue:
            self._available.clear()
        if item is _END:
            self._discardEvents = True
            self._closed = True
            self._queue.clear()
            raise StopAsyncIteration
        return item  # type: ignore[return-value]

    def _claim_consumer(self) -> None:
        current = asyncio.current_task()
        if self._consumerClaimed:
            if current is not self._consumerTask:
                raise LifecycleError("consumer", "EventStream permits only one consumer")
            return
        self._consumerClaimed = True
        self._consumerTask = current

    async def result(self) -> ResultT:
        self._activate()
        assert self._result is not None
        try:
            return await asyncio.shield(self._result)
        except asyncio.CancelledError as cancellation:
            if self._result.done():
                return self._result.result()
            await self._settle_cancelled_observer(cancellation)

    async def _settle_cancelled_observer(
        self, cancellation: asyncio.CancelledError
    ) -> NoReturn:
        try:
            await self._cancel_and_settle()
            self._raise_cleanup_failure()
        except LifecycleError as cleanup_failure:
            cancellation.__cause__ = cleanup_failure
        raise cancellation

    async def _cancel_and_settle(self) -> None:
        self._controller.abort()
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancellation = error
        if cancellation is not None:
            cleanup_failure = self._cleanup_failure()
            if cleanup_failure is not None:
                cancellation.__cause__ = cleanup_failure
            raise cancellation

    async def aclose(self) -> None:
        if self._closed:
            return
        self._discardEvents = True
        self._queue.clear()
        self._available.set()
        if not self._activated:
            self._closed = True
            return
        try:
            await self._cancel_and_settle()
            self._raise_cleanup_failure()
        finally:
            self._closed = True

    def _raise_cleanup_failure(self) -> None:
        failure = self._cleanup_failure()
        if failure is not None:
            raise failure

    def _cleanup_failure(self) -> LifecycleError | None:
        if self._result is not None and self._result.done():
            error = self._result.exception()
            if isinstance(error, LifecycleError) and error.code == "cleanup":
                return error
        return None

    async def __aenter__(self) -> EventStream[EventT, ResultT]:
        self._activate()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_type, traceback
        try:
            await self.aclose()
        except BaseException as close_error:
            if exc is None:
                raise
            exc.__cause__ = close_error
        return False


def _create_event_stream(
    producer: _Producer[EventT, ResultT],
) -> EventStream[EventT, ResultT]:
    stream = object.__new__(EventStream)
    stream._initialize(producer)
    return stream


def _observe_future_exception(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()
