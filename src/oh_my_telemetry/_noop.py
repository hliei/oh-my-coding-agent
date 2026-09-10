from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import inspect
from typing import Any, Final, TypeVar, cast

from ._types import SpanAttributes, SpanOptions, SpanStatus, TelemetrySpan


_T = TypeVar("_T")


class _NoopTelemetrySpan:
    def startSpan(
        self,
        options: SpanOptions,
        callback: Callable[[TelemetrySpan], _T | Awaitable[_T]],
    ) -> Awaitable[_T]:
        loop = asyncio.get_running_loop()
        completion: asyncio.Future[_T] = loop.create_future()
        try:
            callback_result = callback(self)
        except Exception as error:
            completion.set_exception(error)
            return completion

        if not inspect.isawaitable(callback_result):
            loop.call_soon(completion.set_result, callback_result)
            return completion

        if inspect.iscoroutine(callback_result):
            callback_awaitable: asyncio.Future[_T] = asyncio.Task(
                callback_result, loop=loop, eager_start=True
            )
        else:
            callback_awaitable = asyncio.ensure_future(
                cast(Awaitable[_T], callback_result), loop=loop
            )

        def publish_result(finished: asyncio.Future[_T]) -> None:
            if finished.cancelled():
                completion.cancel()
                return
            error = finished.exception()
            if error is not None:
                completion.set_exception(error)
                return
            completion.set_result(finished.result())

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
