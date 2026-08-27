from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from oh_my_coding_agent import (
    CreateAgentSessionOptions,
    SessionManager,
    createAgentSession,
)
from oh_my_llm import (
    AssistantMessage,
    AssistantMessageErrorEvent,
    Context,
    LifecycleError,
    UserMessage,
    createModels,
)
from oh_my_llm.providers.deepseek import deepseekProvider
from ticket_31_scenario import (
    _RESOURCE_ORDER,
    _TransportFactory,
    _barrier_client_type,
    _drive_cleanup as _drive_nested_cleanup,
    _entered_waiter,
    run_scenario,
)


ROOT = Path(__file__).parents[2]


def _install_transport_factory(
    monkeypatch: pytest.MonkeyPatch,
    factory: _TransportFactory,
) -> None:
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", factory)
    monkeypatch.setattr(httpx, "AsyncClient", _barrier_client_type())


def _models_context() -> tuple[Any, Any, Context]:
    models = createModels()
    models.setProvider(deepseekProvider())
    model = models.getModel("deepseek", "deepseek-v4-flash")
    assert model is not None
    return (
        models,
        model,
        Context(messages=(UserMessage(content="cancel", timestamp=0),)),
    )


def test_product_session_nested_deepseek_cleanup_survives_later_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    factory = _TransportFactory()
    _install_transport_factory(monkeypatch, factory)

    async def scenario() -> None:
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    sessionManager=SessionManager.inMemory(os.fspath(tmp_path))
                )
            )
        ).session
        prompt = asyncio.create_task(session.prompt("cancel nested cleanup"))
        await factory.graph.read_started.wait()

        first_abort = asyncio.create_task(session.abort())
        await factory.graph.started["response"].wait()
        second_abort = await _entered_waiter(session.abort())
        idle_waiter = await _entered_waiter(session.waitForIdle())
        second_abort.cancel()
        idle_waiter.cancel()

        await _drive_nested_cleanup(factory.graph, prompt)
        await prompt
        await first_abort
        with pytest.raises(asyncio.CancelledError):
            await second_abort
        with pytest.raises(asyncio.CancelledError):
            await idle_waiter

        assert factory.graph.close_counts == dict.fromkeys(_RESOURCE_ORDER, 1)
        assert all(event.is_set() for event in factory.graph.closed.values())
        assert len(factory.graph.requests) == 1
        assert session.isIdle is True
        cancelled = session.messages[-1]
        assert isinstance(cancelled, AssistantMessage)
        assert cancelled.stopReason == "aborted"
        assert cancelled.errorMessage == "Operation aborted"

        await session.prompt("reuse")
        reused = session.messages[-1]
        assert isinstance(reused, AssistantMessage)
        assert reused.stopReason == "stop"
        assert len(factory.success.requests) == 1
        assert factory.success.closed.is_set()
        await session.dispose()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))
    assert factory.retries == [0, 0]


@pytest.mark.parametrize("helper", ("streamSimple", "completeSimple"))
def test_direct_simple_cancellation_retains_original_carrier_after_nested_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    helper: str,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    factory = _TransportFactory()
    _install_transport_factory(monkeypatch, factory)
    models, model, context = _models_context()

    async def scenario() -> None:
        observed: list[asyncio.CancelledError] = []

        async def invoke() -> None:
            try:
                if helper == "streamSimple":
                    async for _ in models.streamSimple(model, context):
                        pass
                else:
                    await models.completeSimple(model, context)
            except asyncio.CancelledError as cancellation:
                observed.append(cancellation)
                raise

        operation = asyncio.create_task(invoke())
        await factory.graph.read_started.wait()
        operation.cancel()
        await factory.graph.started["response"].wait()
        await _drive_nested_cleanup(factory.graph, operation)

        with pytest.raises(asyncio.CancelledError) as cancelled:
            await operation
        assert cancelled.value is factory.graph.first_cancellation
        assert observed == [cancelled.value]
        assert factory.graph.close_counts == dict.fromkeys(_RESOURCE_ORDER, 1)
        assert all(event.is_set() for event in factory.graph.closed.values())

        recovered = await models.completeSimple(model, context)
        assert recovered.stopReason == "stop"

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))
    assert factory.retries == [0, 0]


def test_managed_stream_commits_one_aborted_terminal_after_nested_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    factory = _TransportFactory()
    _install_transport_factory(monkeypatch, factory)
    models, model, context = _models_context()

    async def scenario() -> None:
        stream = models.stream(model, context)
        events = asyncio.create_task(_collect_events(stream))
        cancelled_waiter = asyncio.create_task(stream.result())
        surviving_waiter = asyncio.create_task(stream.result())
        await factory.graph.read_started.wait()
        cancelled_waiter.cancel()
        await factory.graph.started["response"].wait()
        await _drive_nested_cleanup(factory.graph, surviving_waiter)

        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter
        terminal = await surviving_waiter
        observed = await events
        aborted = [
            event
            for event in observed
            if isinstance(event, AssistantMessageErrorEvent)
            and event.reason == "aborted"
        ]
        assert aborted == [observed[-1]]
        assert aborted[0].error is terminal
        assert terminal.stopReason == "aborted"
        assert terminal.errorMessage == "Operation aborted"
        assert factory.graph.close_counts == dict.fromkeys(_RESOURCE_ORDER, 1)

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))
    assert factory.retries == [0]


async def _collect_events(stream: Any) -> list[Any]:
    return [event async for event in stream]


def test_concurrent_dispose_and_cancelled_waiters_join_nested_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    factory = _TransportFactory()
    _install_transport_factory(monkeypatch, factory)

    async def scenario() -> None:
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    sessionManager=SessionManager.inMemory(os.fspath(tmp_path))
                )
            )
        ).session
        prompt = asyncio.create_task(session.prompt("dispose nested cleanup"))
        await factory.graph.read_started.wait()
        cancelled_disposal = asyncio.create_task(session.dispose())
        await factory.graph.started["response"].wait()
        surviving_disposal = await _entered_waiter(session.dispose())
        idle_waiter = await _entered_waiter(session.waitForIdle())
        cancelled_disposal.cancel()
        idle_waiter.cancel()
        await _drive_nested_cleanup(factory.graph, prompt)

        await prompt
        await surviving_disposal
        with pytest.raises(asyncio.CancelledError):
            await cancelled_disposal
        with pytest.raises(asyncio.CancelledError):
            await idle_waiter
        assert factory.graph.close_counts == dict.fromkeys(_RESOURCE_ORDER, 1)
        assert all(event.is_set() for event in factory.graph.closed.values())
        assert session.isIdle is True
        with pytest.raises(LifecycleError) as disposed:
            await session.prompt("closed")
        assert disposed.value.code == "disposed"

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))
    assert factory.retries == [0]


@pytest.mark.parametrize("cancel_at_settlement", (False, True))
def test_nested_resource_close_failure_is_fail_closed_and_never_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_at_settlement: bool,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    factory = _TransportFactory(close_failure="response")
    _install_transport_factory(monkeypatch, factory)

    async def scenario() -> None:
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    sessionManager=SessionManager.inMemory(os.fspath(tmp_path))
                )
            )
        ).session
        prompt = asyncio.create_task(session.prompt("fail nested cleanup"))
        await factory.graph.read_started.wait()
        if cancel_at_settlement:
            assert factory.graph.owner is not None
            factory.graph.owner.add_done_callback(lambda _: prompt.cancel())
        abort = asyncio.create_task(session.abort())
        await factory.graph.started["response"].wait()
        await _drive_nested_cleanup(factory.graph, prompt)

        cleanup: LifecycleError
        if cancel_at_settlement:
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await prompt
            assert isinstance(cancelled.value.__cause__, LifecycleError)
            cleanup = cancelled.value.__cause__
        else:
            with pytest.raises(LifecycleError) as failed:
                await prompt
            cleanup = failed.value
        with pytest.raises(LifecycleError) as abort_cleanup:
            await abort
        assert cleanup.code == "cleanup"
        assert abort_cleanup.value is cleanup
        assert "SECRET_RESPONSE_CLOSE_CANARY" not in str(cleanup)
        assert factory.graph.close_counts == dict.fromkeys(_RESOURCE_ORDER, 1)
        assert all(event.is_set() for event in factory.graph.closed.values())
        assert not any(
            isinstance(message, AssistantMessage) and message.stopReason == "aborted"
            for message in session.messages
        )
        assert session.isIdle is False
        with pytest.raises(LifecycleError) as closed:
            await session.prompt("must not retry")
        assert closed.value.code == "closing"
        assert len(factory.transports) == 1

        await session.dispose()
        await session.dispose()
        assert factory.graph.close_counts == dict.fromkeys(_RESOURCE_ORDER, 1)

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))
    assert factory.retries == [0]


@pytest.mark.parametrize("resource", _RESOURCE_ORDER)
@pytest.mark.parametrize("cutoff", ("started", "drained"))
def test_first_cancellation_at_each_normal_cleanup_cutoff_drains_once(
    monkeypatch: pytest.MonkeyPatch,
    resource: str,
    cutoff: str,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    factory = _TransportFactory()
    _install_transport_factory(monkeypatch, factory)
    models, model, context = _models_context()

    async def scenario() -> None:
        operation = asyncio.create_task(models.completeSimple(model, context))
        graph = factory.graph
        await graph.read_started.wait()
        for name in _RESOURCE_ORDER:
            if name != resource or cutoff != "started":
                graph.proceed[name].set()
            if name != resource or cutoff != "drained":
                graph.return_close[name].set()
        graph.finish_read.set()
        barriers = graph.started if cutoff == "started" else graph.drained
        await barriers[resource].wait()
        operation.cancel()
        graph.proceed[resource].set()
        graph.return_close[resource].set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert graph.close_counts == dict.fromkeys(_RESOURCE_ORDER, 1)
        assert all(event.is_set() for event in graph.closed.values())

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))
    assert factory.retries == [0]


def test_installed_scenario_and_matrix_lock_nested_cleanup_observation(
    tmp_path: Path,
) -> None:
    actual = asyncio.run(asyncio.wait_for(run_scenario(tmp_path), timeout=10.0))
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    case = next(
        item
        for item in corpus["cases"]
        if item["id"] == "reference.deepseek-nested-cancellation-cleanup"
    )
    assert actual == case["omhExpectation"]

    matrix = json.loads((ROOT / "conformance/obligation-matrix.json").read_text())
    row = next(
        item
        for item in matrix["obligations"]
        if item["id"] == "omh-v0.deepseek-nested-cancellation-cleanup"
    )
    assert row["authority"].endswith(
        ".scratch/omh-v0/tickets/31-shield-nested-deepseek-cancellation-cleanup.md"
    )
    assert row["corpusCase"] == case["id"]
    assert row["executableRunners"] == [
        "ticket-31-deepseek-cleanup",
        "ticket-31-installed",
    ]
