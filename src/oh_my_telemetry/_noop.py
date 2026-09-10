from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Final, TypeVar, cast

from ._operation import _start_callback
from ._types import SpanAttributes, SpanOptions, SpanStatus, TelemetrySpan


_T = TypeVar("_T")


class _NoopTelemetrySpan:
    def startSpan(
        self,
        options: SpanOptions,
        callback: Callable[[TelemetrySpan], _T | Awaitable[_T]],
    ) -> Awaitable[_T]:
        loop = asyncio.get_running_loop()
        start = cast(Callable[..., Awaitable[_T]], _start_callback)
        return start(loop, self, callback, lambda _error: None)

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
