from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal, NotRequired, Protocol, Required, TypeVar, TypedDict, overload


AttributeValue = (
    str
    | bool
    | int
    | float
    | list[str]
    | list[bool]
    | list[int | float]
)
SpanAttributes = dict[str, AttributeValue | None]


class SpanOptions(TypedDict, total=False):
    name: Required[str]
    attributes: NotRequired[SpanAttributes]


class _OkSpanStatus(TypedDict):
    status: Required[Literal["ok"]]


class _SpanErrorDetails(TypedDict):
    name: Required[str]
    message: Required[str]


class _ErrorSpanStatus(TypedDict, total=False):
    status: Required[Literal["error"]]
    error: NotRequired[_SpanErrorDetails]


SpanStatus = _OkSpanStatus | _ErrorSpanStatus

_T = TypeVar("_T")


class TelemetryContext(Protocol):
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


class TelemetrySpan(TelemetryContext, Protocol):
    def addEvent(
        self, name: str, attributes: SpanAttributes | None = None
    ) -> None: ...

    def setAttributes(self, attributes: SpanAttributes) -> None: ...

    def setStatus(self, status: SpanStatus) -> None: ...
