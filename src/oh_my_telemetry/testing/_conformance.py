from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import cast

from oh_my_telemetry import (
    RecordedTelemetrySpan,
    SpanAttributes,
    SpanOptions,
    SpanStatus,
    TelemetrySpan,
)

from ._types import (
    TelemetryAdapterConformanceCase,
    TelemetryAdapterFixture,
    TelemetryAdapterFixtureFactory,
)


class _Unreadable:
    def __getitem__(self, key: object) -> object:
        raise RuntimeError("read")

    def __iter__(self) -> Iterator[object]:
        raise RuntimeError("enumerate")

    def get(self, key: object, default: object = None) -> object:
        raise RuntimeError("read")

    def items(self) -> Iterator[tuple[object, object]]:
        raise RuntimeError("enumerate")

    def keys(self) -> Iterator[object]:
        raise RuntimeError("enumerate")


class _UnreadableList(list[object]):
    def __iter__(self) -> Iterator[object]:
        raise RuntimeError("read")


@dataclass(frozen=True, slots=True)
class _ConformanceCase:
    group: str
    name: str
    _factory: TelemetryAdapterFixtureFactory
    _body: Callable[[TelemetryAdapterFixture], Awaitable[None]]

    async def run(self) -> None:
        fixture = await self._factory()
        primary: BaseException | None = None
        try:
            await self._body(fixture)
        except BaseException as error:
            primary = error

        cleanup = await _settle_cleanup(fixture)
        if primary is None and cleanup.cancellation is not None:
            primary = cleanup.cancellation
        if primary is not None:
            secondary = cleanup.failure
            if (
                cleanup.cancellation is not None
                and cleanup.cancellation is not primary
            ):
                secondary = (
                    cleanup.cancellation
                    if secondary is None
                    else BaseExceptionGroup(
                        "fixture cleanup secondary failures",
                        [cleanup.cancellation, secondary],
                    )
                )
            if secondary is None:
                raise primary.with_traceback(primary.__traceback__)
            raise primary.with_traceback(primary.__traceback__) from secondary
        if cleanup.failure is not None:
            raise cleanup.failure


@dataclass(frozen=True, slots=True)
class _CleanupResult:
    failure: BaseException | None


@dataclass(frozen=True, slots=True)
class _CleanupSettlement:
    cancellation: asyncio.CancelledError | None
    failure: BaseException | None


async def _capture_cleanup(
    fixture: TelemetryAdapterFixture,
) -> _CleanupResult:
    try:
        await fixture.aclose()
    except BaseException as error:
        return _CleanupResult(error)
    return _CleanupResult(None)


async def _settle_cleanup(
    fixture: TelemetryAdapterFixture,
) -> _CleanupSettlement:
    cleanup = asyncio.create_task(_capture_cleanup(fixture))
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(cleanup)
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
            continue
        break
    return _CleanupSettlement(cancellation, result.failure)


def _find_span(
    spans: list[RecordedTelemetrySpan], name: str
) -> RecordedTelemetrySpan:
    for span in spans:
        if span["name"] == name:
            return span
    raise AssertionError(f"Expected recorded span {name}")


async def _rejects_with_same(
    operation: Awaitable[object], expected: BaseException
) -> None:
    try:
        await operation
    except BaseException as error:
        assert error is expected
        return
    raise AssertionError("Expected operation to reject")


def _rejected(error: BaseException) -> Awaitable[object]:
    future = asyncio.get_running_loop().create_future()
    future.set_exception(error)
    return future


def _case(
    factory: TelemetryAdapterFixtureFactory,
    group: str,
    name: str,
    body: Callable[[TelemetryAdapterFixture], Awaitable[None]],
) -> _ConformanceCase:
    return _ConformanceCase(group, name, factory, body)


async def _admits_once(fixture: TelemetryAdapterFixture) -> None:
    admitted = False
    calls = 0
    expected = {"value": 42}

    def callback(_span: TelemetrySpan) -> dict[str, int]:
        nonlocal admitted, calls
        admitted = True
        calls += 1
        return expected

    result = fixture.context.startSpan({"name": "success"}, callback)
    assert admitted is True
    assert calls == 1
    assert await result is expected
    assert _find_span(await fixture.getSpans(), "success")["status"] == {
        "status": "ok"
    }
    assert _find_span(await fixture.getSpans(), "success")["settled"] is True


async def _preserves_rejections(fixture: TelemetryAdapterFixture) -> None:
    sync_error = RuntimeError("sync")

    def sync_callback(_span: TelemetrySpan) -> object:
        raise sync_error

    await _rejects_with_same(
        fixture.context.startSpan({"name": "sync-error"}, sync_callback),
        sync_error,
    )

    async_error = RuntimeError("async")

    async def async_callback(_span: TelemetrySpan) -> object:
        raise async_error

    await _rejects_with_same(
        fixture.context.startSpan({"name": "async-error"}, async_callback),
        async_error,
    )

    undefined_error = RuntimeError("undefined")

    def undefined_callback(_span: TelemetrySpan) -> Awaitable[object]:
        return _rejected(undefined_error)

    await _rejects_with_same(
        fixture.context.startSpan(
            {"name": "undefined-error"}, undefined_callback
        ),
        undefined_error,
    )

    unreadable_error = RuntimeError("unreadable")

    def unreadable_callback(_span: TelemetrySpan) -> object:
        raise unreadable_error

    await _rejects_with_same(
        fixture.context.startSpan(
            {"name": "unreadable-error"}, unreadable_callback
        ),
        unreadable_error,
    )

    async_unreadable_error = RuntimeError("async-unreadable")

    def async_unreadable_callback(_span: TelemetrySpan) -> Awaitable[object]:
        return _rejected(async_unreadable_error)

    await _rejects_with_same(
        fixture.context.startSpan(
            {"name": "async-unreadable-error"}, async_unreadable_callback
        ),
        async_unreadable_error,
    )

    spans = await fixture.getSpans()
    for name in (
        "sync-error",
        "async-error",
        "undefined-error",
        "unreadable-error",
        "async-unreadable-error",
    ):
        assert _find_span(spans, name)["status"]["status"] == "error"


async def _last_explicit_status(fixture: TelemetryAdapterFixture) -> None:
    def last_status(span: TelemetrySpan) -> None:
        span.setStatus(
            {
                "status": "error",
                "error": {"name": "Expected", "message": "first"},
            }
        )
        span.setStatus({"status": "ok"})

    await fixture.context.startSpan({"name": "last-status"}, last_status)

    thrown = RuntimeError("after explicit status")

    def explicit_before_throw(span: TelemetrySpan) -> object:
        span.setStatus({"status": "ok"})
        raise thrown

    await _rejects_with_same(
        fixture.context.startSpan(
            {"name": "explicit-before-throw"}, explicit_before_throw
        ),
        thrown,
    )

    rejected = RuntimeError("after async explicit status")

    def explicit_before_rejection(span: TelemetrySpan) -> Awaitable[object]:
        span.setStatus(
            {
                "status": "error",
                "error": {"name": "Expected", "message": "async failure"},
            }
        )
        return _rejected(rejected)

    await _rejects_with_same(
        fixture.context.startSpan(
            {"name": "explicit-before-rejection"}, explicit_before_rejection
        ),
        rejected,
    )

    def expected_failure(span: TelemetrySpan) -> dict[str, bool]:
        span.setStatus(
            {
                "status": "error",
                "error": {"name": "Expected", "message": "returned failure"},
            }
        )
        return {"ok": False}

    await fixture.context.startSpan(
        {"name": "expected-failure"}, expected_failure
    )

    spans = await fixture.getSpans()
    assert _find_span(spans, "last-status")["status"] == {"status": "ok"}
    assert _find_span(spans, "explicit-before-throw")["status"] == {
        "status": "ok"
    }
    assert _find_span(spans, "explicit-before-rejection")["status"] == {
        "status": "error",
        "error": {"name": "Expected", "message": "async failure"},
    }
    assert _find_span(spans, "expected-failure")["status"] == {
        "status": "error",
        "error": {"name": "Expected", "message": "returned failure"},
    }


async def _merges_attributes(fixture: TelemetryAdapterFixture) -> None:
    def record(span: TelemetrySpan) -> None:
        span.setAttributes({"count": 1, "overwrite": "middle"})
        span.setAttributes({"count": None, "overwrite": "end"})
        span.addEvent("first", {"index": 1, "ignored": None})
        span.addEvent("second", {"index": 2})

    await fixture.context.startSpan(
        {
            "name": "recording",
            "attributes": {
                "start": "value",
                "overwrite": "start",
                "ignored": None,
            },
        },
        record,
    )
    span = _find_span(await fixture.getSpans(), "recording")
    assert span["attributes"] == {
        "start": "value",
        "overwrite": "end",
        "count": 1,
    }
    assert span["events"] == [
        {"name": "first", "attributes": {"index": 1}},
        {"name": "second", "attributes": {"index": 2}},
    ]


async def _atomic_attributes(fixture: TelemetryAdapterFixture) -> None:
    def record(span: TelemetrySpan) -> None:
        attributes = cast(
            SpanAttributes,
            {
                "partial": "must not survive",
                "unreadable": _UnreadableList(["value"]),
            },
        )
        span.setAttributes(attributes)

    await fixture.context.startSpan(
        {"name": "atomic-attributes", "attributes": {"retained": "value"}},
        record,
    )
    assert _find_span(await fixture.getSpans(), "atomic-attributes")[
        "attributes"
    ] == {"retained": "value"}


async def _settled_calls_are_inert(fixture: TelemetryAdapterFixture) -> None:
    settled_span: TelemetrySpan | None = None

    def capture(span: TelemetrySpan) -> None:
        nonlocal settled_span
        settled_span = span

    await fixture.context.startSpan(
        {"name": "settled", "attributes": {"value": "initial"}}, capture
    )
    assert settled_span is not None
    captured = settled_span
    captured.setAttributes({"value": "late"})
    captured.addEvent("late", {"value": True})
    captured.setStatus({"status": "error"})
    child_admitted = False

    def late_child(_span: TelemetrySpan) -> int:
        nonlocal child_admitted
        child_admitted = True
        return 7

    child_result = captured.startSpan({"name": "late-child"}, late_child)
    assert child_admitted is True
    assert await child_result == 7
    spans = await fixture.getSpans()
    assert len(spans) == 1
    assert spans[0]["attributes"] == {"value": "initial"}
    assert spans[0]["events"] == []
    assert spans[0]["status"] == {"status": "ok"}


async def _nested_and_concurrent_children(
    fixture: TelemetryAdapterFixture,
) -> None:
    first_gate = asyncio.Event()

    async def first_child(_span: TelemetrySpan) -> None:
        await first_gate.wait()

    def second_child(_span: TelemetrySpan) -> str:
        return "done"

    async def parent(parent_span: TelemetrySpan) -> None:
        first = parent_span.startSpan({"name": "first-child"}, first_child)
        second = parent_span.startSpan({"name": "second-child"}, second_child)
        assert await second == "done"
        first_gate.set()
        await first

    await fixture.context.startSpan({"name": "parent"}, parent)
    spans = await fixture.getSpans()
    parent_span = _find_span(spans, "parent")
    first = _find_span(spans, "first-child")
    second = _find_span(spans, "second-child")
    assert parent_span["parentId"] is None
    assert first["parentId"] == parent_span["id"]
    assert second["parentId"] == parent_span["id"]
    assert (
        second["endSequence"] is not None
        and first["endSequence"] is not None
        and parent_span["endSequence"] is not None
    )
    assert second["endSequence"] < first["endSequence"]
    assert first["endSequence"] < parent_span["endSequence"]


async def _suppresses_unreadable_payloads(
    fixture: TelemetryAdapterFixture,
) -> None:
    calls = 0

    def callback(_span: TelemetrySpan) -> int:
        nonlocal calls
        calls += 1
        return 9

    result = fixture.context.startSpan(
        cast(SpanOptions, _Unreadable()), callback
    )
    assert calls == 1
    assert await result == 9
    assert await fixture.getSpans() == []

    def record(span: TelemetrySpan) -> None:
        attributes = cast(SpanAttributes, _Unreadable())
        status = cast(SpanStatus, _Unreadable())
        span.setAttributes(attributes)
        span.addEvent("unreadable-event", attributes)
        span.setStatus(status)

    await fixture.context.startSpan({"name": "unreadable-recording"}, record)
    recorded = await fixture.getSpans()
    assert len(recorded) == 1
    assert recorded[0]["attributes"] == {}
    assert recorded[0]["events"] == []
    assert recorded[0]["status"] == {"status": "ok"}


async def _atomic_status(fixture: TelemetryAdapterFixture) -> None:
    rejection = RuntimeError("rejected after unreadable status")

    def callback(span: TelemetrySpan) -> Awaitable[object]:
        span.setStatus(cast(SpanStatus, _Unreadable()))
        return _rejected(rejection)

    await _rejects_with_same(
        fixture.context.startSpan({"name": "unreadable-status"}, callback),
        rejection,
    )
    assert (
        _find_span(await fixture.getSpans(), "unreadable-status")["status"][
            "status"
        ]
        == "error"
    )


def createTelemetryAdapterConformance(
    factory: TelemetryAdapterFixtureFactory,
) -> list[TelemetryAdapterConformanceCase]:
    return [
        _case(
            factory,
            "callback lifecycle",
            "admits once synchronously and preserves the result",
            _admits_once,
        ),
        _case(
            factory,
            "callback lifecycle",
            "preserves synchronous and asynchronous rejection values",
            _preserves_rejections,
        ),
        _case(
            factory,
            "status",
            "uses last explicit status without automatic overwrite",
            _last_explicit_status,
        ),
        _case(
            factory,
            "recording",
            "merges attributes and records ordered events",
            _merges_attributes,
        ),
        _case(
            factory,
            "recording",
            "ignores failed attribute calls atomically",
            _atomic_attributes,
        ),
        _case(
            factory,
            "recording",
            "makes calls after settlement inert",
            _settled_calls_are_inert,
        ),
        _case(
            factory,
            "parentage",
            "records nested and concurrent child relationships",
            _nested_and_concurrent_children,
        ),
        _case(
            factory,
            "passivity",
            "suppresses unreadable telemetry payload failures",
            _suppresses_unreadable_payloads,
        ),
        _case(
            factory,
            "passivity",
            "ignores failed status calls atomically",
            _atomic_status,
        ),
    ]
