from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar, cast, overload

from ._noop import NOOP_TELEMETRY_CONTEXT
from ._operation import _start_callback
from ._types import (
    RecordedTelemetryEvent,
    RecordedTelemetrySpan,
    SpanAttributes,
    SpanOptions,
    SpanStatus,
    TelemetrySpan,
)


_T = TypeVar("_T")


def _copy_attribute_value(value: object) -> object:
    return list(value) if isinstance(value, list) else value


def _copy_attributes(
    attributes: Mapping[str, object] | None,
) -> dict[str, object]:
    copied: dict[str, object] = {}
    if attributes is None:
        return copied
    for name, value in attributes.items():
        if value is not None:
            copied[name] = _copy_attribute_value(value)
    return copied


def _merge_attributes(
    current: Mapping[str, object], attributes: Mapping[str, object]
) -> dict[str, object]:
    merged = _copy_attributes(current)
    merged.update(_copy_attributes(attributes))
    return merged


def _snapshot_event(event: Mapping[str, object]) -> RecordedTelemetryEvent:
    return {
        "name": cast(str, event["name"]),
        "attributes": cast(
            dict[str, Any],
            _copy_attributes(cast(Mapping[str, object], event["attributes"])),
        ),
    }


def _ok_status() -> SpanStatus:
    return {"status": "ok"}


@dataclass
class _RecordedSpan:
    id: int
    name: str
    attributes: dict[str, object] = field(default_factory=dict)
    events: list[dict[str, object]] = field(default_factory=list)
    settled: bool = False
    end_sequence: int | None = None
    status: SpanStatus = field(default_factory=_ok_status)


class _InMemoryState:
    def __init__(self) -> None:
        self.spans: list[_RecordedSpan] = []
        self.next_span_id = 1
        self.next_end_sequence = 1

    def create_root(
        self, name: str, attributes: dict[str, object]
    ) -> _RecordedSpan:
        span = _RecordedSpan(
            id=self.next_span_id, name=name, attributes=attributes
        )
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
    def __init__(self, recorded: _RecordedSpan) -> None:
        self._recorded = recorded

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
        if self._recorded.settled:
            return
        self._recorded.events.append(
            {"name": name, "attributes": _copy_attributes(attributes)}
        )

    def setAttributes(self, attributes: SpanAttributes) -> None:
        if self._recorded.settled:
            return
        self._recorded.attributes = _merge_attributes(
            self._recorded.attributes, attributes
        )

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
        span = self._state.create_root(
            options["name"],
            _copy_attributes(options.get("attributes")),
        )
        callback_span = _InMemoryTelemetrySpan(span)
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
                "attributes": cast(
                    dict[str, Any], _copy_attributes(span.attributes)
                ),
                "events": [_snapshot_event(event) for event in span.events],
                "status": status,
                "settled": span.settled,
            }
            if span.end_sequence is not None:
                snapshot["endSequence"] = span.end_sequence
            snapshots.append(snapshot)
        return snapshots
