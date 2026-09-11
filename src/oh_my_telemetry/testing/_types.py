from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from oh_my_telemetry import RecordedTelemetrySpan, TelemetryContext


class TelemetryAdapterFixture(Protocol):
    @property
    def context(self) -> TelemetryContext: ...

    def getSpans(self) -> Awaitable[list[RecordedTelemetrySpan]]: ...

    def aclose(self) -> Awaitable[None]: ...


TelemetryAdapterFixtureFactory = Callable[[], Awaitable[TelemetryAdapterFixture]]


class TelemetryAdapterConformanceCase(Protocol):
    @property
    def group(self) -> str: ...

    @property
    def name(self) -> str: ...

    def run(self) -> Awaitable[None]: ...
