from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass
import inspect
from typing import Any, Final, Generic, TypeVar, cast

from ._types import SpanAttributes, SpanOptions, SpanStatus, TelemetrySpan


_T = TypeVar("_T")


@dataclass(frozen=True)
class _Returned(Generic[_T]):
    value: _T


@dataclass(frozen=True)
class _Raised:
    error: BaseException


async def _capture_outcome(
    callback: Awaitable[_T],
) -> _Returned[_T] | _Raised:
    try:
        return _Returned(await callback)
    except BaseException as error:
        return _Raised(error)


class _RepeatableCompletion(Generic[_T]):
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._completion: asyncio.Future[_T] = loop.create_future()
        self._callback: asyncio.Future[Any] | None = None
        self._cancellation_requested = False

    def bind(self, callback: asyncio.Future[Any]) -> None:
        self._callback = callback

    def set_result(self, result: _T) -> None:
        self._completion.set_result(result)

    def set_exception(self, error: BaseException) -> None:
        self._completion.set_exception(error)

    async def _wait(self) -> _T:
        while True:
            try:
                return await asyncio.shield(self._completion)
            except asyncio.CancelledError as requester_cancellation:
                if self._completion.done():
                    return self._completion.result()
                callback = self._callback
                if (
                    not self._cancellation_requested
                    and callback is not None
                    and not callback.done()
                ):
                    self._cancellation_requested = True
                    message = (
                        requester_cancellation.args[0]
                        if requester_cancellation.args
                        else None
                    )
                    callback.cancel(message)

    def __await__(self) -> Generator[Any, None, _T]:
        return self._wait().__await__()


class _NoopTelemetrySpan:
    def startSpan(
        self,
        options: SpanOptions,
        callback: Callable[[TelemetrySpan], _T | Awaitable[_T]],
    ) -> Awaitable[_T]:
        loop = asyncio.get_running_loop()
        completion = _RepeatableCompletion[_T](loop)
        try:
            callback_result = callback(self)
        except Exception as error:
            completion.set_exception(error)
            return completion

        if not inspect.isawaitable(callback_result):
            loop.call_soon(completion.set_result, callback_result)
            return completion

        callback_awaitable = asyncio.Task(
            _capture_outcome(cast(Awaitable[_T], callback_result)),
            loop=loop,
            eager_start=inspect.iscoroutine(callback_result),
        )
        completion.bind(callback_awaitable)

        def publish_result(
            finished: asyncio.Future[_Returned[_T] | _Raised],
        ) -> None:
            outcome = finished.result()
            if isinstance(outcome, _Raised):
                completion.set_exception(outcome.error)
            else:
                completion.set_result(outcome.value)

        callback_awaitable.add_done_callback(publish_result)
        return completion

    def addEvent(
        self, name: str, attributes: SpanAttributes | None = None
    ) -> None:
        return None

    def setAttributes(self, attributes: SpanAttributes) -> None:
        return None

    def setStatus(self, status: SpanStatus) -> None:
        return None


_NOOP_SPAN: Final = _NoopTelemetrySpan()
NOOP_TELEMETRY_CONTEXT: Final = cast(TelemetrySpan, _NOOP_SPAN)
