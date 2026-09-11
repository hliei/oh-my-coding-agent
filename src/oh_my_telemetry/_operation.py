from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass
import inspect
from typing import Any, Generic, TypeVar

from ._types import TelemetrySpan


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


def _start_callback(
    loop: asyncio.AbstractEventLoop,
    callback_span: TelemetrySpan,
    callback: Callable[[TelemetrySpan], Any],
    settle: Callable[[BaseException | None], None],
) -> Awaitable[Any]:
    completion = _RepeatableCompletion[Any](loop)
    try:
        callback_result = callback(callback_span)
    except BaseException as error:
        settle(error)
        completion.set_exception(error)
        return completion

    if not inspect.isawaitable(callback_result):
        def publish_result() -> None:
            settle(None)
            completion.set_result(callback_result)

        loop.call_soon(publish_result)
        return completion

    callback_awaitable = asyncio.Task(
        _capture_outcome(callback_result),
        loop=loop,
        eager_start=inspect.iscoroutine(callback_result),
    )
    completion.bind(callback_awaitable)

    def publish_awaitable_result(finished: asyncio.Future[Any]) -> None:
        outcome = finished.result()
        if isinstance(outcome, _Raised):
            settle(outcome.error)
            completion.set_exception(outcome.error)
        else:
            settle(None)
            completion.set_result(outcome.value)

    callback_awaitable.add_done_callback(publish_awaitable_result)
    return completion
