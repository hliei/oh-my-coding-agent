from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar, cast, overload

from ._noop import NOOP_TELEMETRY_CONTEXT
from ._operation import _start_callback
from ._types import (
    RecordedTelemetrySpan,
    SpanAttributes,
    SpanOptions,
    SpanStatus,
    TelemetrySpan,
)


_T = TypeVar("_T")


def _ok_status() -> SpanStatus:
    return {"status": "ok"}


@dataclass
class _RecordedSpan:
    id: int
    name: str
    settled: bool = False
    end_sequence: int | None = None
    status: SpanStatus = field(default_factory=_ok_status)


class _InMemoryState:
    def __init__(self) -> None:
        self.spans: list[_RecordedSpan] = []
        self.next_span_id = 1
        self.next_end_sequence = 1

    def create_root(self, name: str) -> _RecordedSpan:
        span = _RecordedSpan(id=self.next_span_id, name=name)
        self.next_span_id += 1
        self.spans.append(span)
        return span

    def settle(self, span: _RecordedSpan, error: BaseException | None) -> None:
        if span.settled:
            return
        if error is not None:
            span.status = {
                "status": "error",
                "error": {
                    "name": type(error).__name__,
                    "message": str(error),
                },
            }
        span.settled = True
        span.end_sequence = self.next_end_sequence
        self.next_end_sequence += 1


class _InMemoryTelemetrySpan:
    @overload
    def startSpan(
        self,
        options: SpanOptions,
        callback: Callable[[TelemetrySpan], Awaitable[_T]],
    ) -> Awaitable[_T]: ...

    @overload
    def startSpan(
        self,
        options: SpanOptions,
        callback: Callable[[TelemetrySpan], _T],
    ) -> Awaitable[_T]: ...

    def startSpan(
        self,
        options: SpanOptions,
        callback: Callable[[TelemetrySpan], Any],
    ) -> Awaitable[Any]:
        start = cast(Any, NOOP_TELEMETRY_CONTEXT.startSpan)
        return cast(Awaitable[Any], start(options, callback))

    def addEvent(
        self, name: str, attributes: SpanAttributes | None = None
    ) -> None:
        return None

    def setAttributes(self, attributes: SpanAttributes) -> None:
        return None

    def setStatus(self, status: SpanStatus) -> None:
        return None


class InMemoryTelemetryContext:
    def __init__(self) -> None:
        self._state = _InMemoryState()

    @overload
    def startSpan(
        self,
        options: SpanOptions,
        callback: Callable[[TelemetrySpan], Awaitable[_T]],
    ) -> Awaitable[_T]: ...

    @overload
    def startSpan(
        self,
        options: SpanOptions,
        callback: Callable[[TelemetrySpan], _T],
    ) -> Awaitable[_T]: ...

    def startSpan(
        self,
        options: SpanOptions,
        callback: Callable[[TelemetrySpan], Any],
    ) -> Awaitable[Any]:
        loop = asyncio.get_running_loop()
        span = self._state.create_root(options["name"])
        callback_span = _InMemoryTelemetrySpan()
        return _start_callback(
            loop,
            callback_span,
            callback,
            lambda error: self._state.settle(span, error),
        )

    def getSpans(self) -> list[RecordedTelemetrySpan]:
        snapshots: list[RecordedTelemetrySpan] = []
        for span in self._state.spans:
            status: SpanStatus
            if span.status["status"] == "ok":
                status = {"status": "ok"}
            else:
                error = span.status.get("error")
                status = (
                    {"status": "error"}
                    if error is None
                    else {
                        "status": "error",
                        "error": {
                            "name": error["name"],
                            "message": error["message"],
                        },
                    }
                )
            snapshot: RecordedTelemetrySpan = {
                "id": span.id,
                "parentId": None,
                "name": span.name,
                "attributes": {},
                "events": [],
                "status": status,
                "settled": span.settled,
            }
            if span.end_sequence is not None:
                snapshot["endSequence"] = span.end_sequence
            snapshots.append(snapshot)
        return snapshots
