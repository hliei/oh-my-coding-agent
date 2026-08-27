from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import httpx

from oh_my_coding_agent import (
    CreateAgentSessionOptions,
    SessionManager,
    createAgentSession,
)
from oh_my_llm import AssistantMessage


_SUCCESS_SSE = (
    b'data: {"id":"installed-fresh","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"fresh"},"finish_reason":null}]}\n\n'
    b'data: {"id":"installed-fresh","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3,"prompt_cache_hit_tokens":0}}\n\n'
    b"data: [DONE]\n\n"
)
_RESOURCE_ORDER = ("response", "client", "transport", "tls")


class _CleanupGraph:
    def __init__(self, *, close_failure: str | None = None) -> None:
        self.close_failure = close_failure
        self.owner: asyncio.Task[Any] | None = None
        self.read_started = asyncio.Event()
        self.finish_read = asyncio.Event()
        self.cancellation_seen = asyncio.Event()
        self.first_cancellation: asyncio.CancelledError | None = None
        self.started = {name: asyncio.Event() for name in _RESOURCE_ORDER}
        self.proceed = {name: asyncio.Event() for name in _RESOURCE_ORDER}
        self.drained = {name: asyncio.Event() for name in _RESOURCE_ORDER}
        self.return_close = {name: asyncio.Event() for name in _RESOURCE_ORDER}
        self.closed = {name: asyncio.Event() for name in _RESOURCE_ORDER}
        self.close_counts = {name: 0 for name in _RESOURCE_ORDER}
        self.requests: list[httpx.Request] = []

    async def close_leaf(self, name: str) -> None:
        self.close_counts[name] += 1
        self.started[name].set()
        await self.proceed[name].wait()
        self.drained[name].set()
        await self.return_close[name].wait()
        self.closed[name].set()
        if self.close_failure == name:
            raise RuntimeError(f"SECRET_{name.upper()}_CLOSE_CANARY")


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, graph: _CleanupGraph) -> None:
        self._graph = graph

    async def __aiter__(self) -> AsyncIterator[bytes]:
        owner = asyncio.current_task()
        assert owner is not None
        self._graph.owner = owner
        self._graph.read_started.set()
        try:
            await self._graph.finish_read.wait()
        except asyncio.CancelledError as cancellation:
            self._graph.first_cancellation = cancellation
            self._graph.cancellation_seen.set()
            raise
        yield _SUCCESS_SSE

    async def aclose(self) -> None:
        await self._graph.close_leaf("response")


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, graph: _CleanupGraph) -> None:
        self.graph = graph

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.graph.requests.append(request)
        return httpx.Response(200, stream=_ResponseStream(self.graph), request=request)

    async def aclose(self) -> None:
        graph = self.graph
        graph.close_counts["transport"] += 1
        graph.started["transport"].set()
        await graph.proceed["transport"].wait()
        await graph.close_leaf("tls")
        graph.drained["transport"].set()
        await graph.return_close["transport"].wait()
        graph.closed["transport"].set()


class _SuccessTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.closed = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            content=_SUCCESS_SSE,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )

    async def aclose(self) -> None:
        self.closed.set()


class _TransportFactory:
    def __init__(self, *, close_failure: str | None = None) -> None:
        self.graph = _CleanupGraph(close_failure=close_failure)
        self.success = _SuccessTransport()
        self.transports: list[httpx.AsyncBaseTransport] = []
        self.retries: list[int] = []

    def __call__(self, *, retries: int) -> httpx.AsyncBaseTransport:
        self.retries.append(retries)
        transport: httpx.AsyncBaseTransport = (
            _Transport(self.graph) if not self.transports else self.success
        )
        self.transports.append(transport)
        return transport


async def _drive_cleanup(graph: _CleanupGraph, prompt: asyncio.Task[Any]) -> None:
    async def drive_leaf(name: str) -> None:
        await graph.started[name].wait()
        assert graph.cancellation_seen.is_set()
        assert graph.owner is not None
        graph.owner.cancel()
        graph.proceed[name].set()
        drained = asyncio.create_task(graph.drained[name].wait())
        done, _ = await asyncio.wait(
            (drained, prompt),
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert drained in done
        graph.owner.cancel()
        graph.return_close[name].set()

    await drive_leaf("response")
    await graph.started["client"].wait()
    assert graph.owner is not None
    graph.owner.cancel()
    graph.proceed["client"].set()
    await graph.started["transport"].wait()
    graph.owner.cancel()
    graph.proceed["transport"].set()
    await drive_leaf("tls")
    await graph.drained["transport"].wait()
    graph.owner.cancel()
    graph.return_close["transport"].set()
    await graph.drained["client"].wait()
    graph.owner.cancel()
    graph.return_close["client"].set()


async def _entered_waiter(operation: Awaitable[None]) -> asyncio.Task[None]:
    entered = asyncio.Event()

    async def wait() -> None:
        entered.set()
        await operation

    waiter = asyncio.create_task(wait())
    await entered.wait()
    return waiter


def _barrier_client_type() -> type[httpx.AsyncClient]:
    original_client = httpx.AsyncClient

    class BarrierClient(original_client):  # type: ignore[valid-type,misc]
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            transport = kwargs["transport"]
            self._graph = transport.graph if isinstance(transport, _Transport) else None

        async def aclose(self) -> None:
            graph = self._graph
            if graph is None:
                await super().aclose()
                return
            graph.close_counts["client"] += 1
            graph.started["client"].set()
            await graph.proceed["client"].wait()
            await super().aclose()
            graph.drained["client"].set()
            await graph.return_close["client"].wait()
            graph.closed["client"].set()
            if graph.close_failure == "client":
                raise RuntimeError("SECRET_CLIENT_CLOSE_CANARY")

    return BarrierClient


async def run_scenario(root: Path) -> dict[str, object]:
    factory = _TransportFactory()
    original_client = httpx.AsyncClient
    original_transport = httpx.AsyncHTTPTransport
    httpx.AsyncHTTPTransport = factory  # type: ignore[assignment,misc]
    httpx.AsyncClient = _barrier_client_type()  # type: ignore[misc]
    before = os.environ.get("DEEPSEEK_API_KEY")
    os.environ["DEEPSEEK_API_KEY"] = "installed-conformance-canary"
    try:
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    sessionManager=SessionManager.inMemory(os.fspath(root))
                )
            )
        ).session
        prompt = asyncio.create_task(session.prompt("cancel installed nested cleanup"))
        await factory.graph.read_started.wait()
        first_abort = asyncio.create_task(session.abort())
        await factory.graph.started["response"].wait()
        cancelled_abort = await _entered_waiter(session.abort())
        cancelled_idle = await _entered_waiter(session.waitForIdle())
        cancelled_abort.cancel()
        cancelled_idle.cancel()
        await _drive_cleanup(factory.graph, prompt)
        await prompt
        await first_abort

        cancelled_waiters = 0
        for waiter in (cancelled_abort, cancelled_idle):
            try:
                await waiter
            except asyncio.CancelledError:
                cancelled_waiters += 1

        terminal = session.messages[-1]
        assert isinstance(terminal, AssistantMessage)
        idle_before_reuse = session.isIdle
        await session.prompt("reuse installed session")
        reused = session.messages[-1]
        assert isinstance(reused, AssistantMessage)
        observation: dict[str, object] = {
            "A": "public_product_session_active_stream_abort",
            "L": {
                "cancellationBeforeCleanup": factory.graph.cancellation_seen.is_set(),
                "closed": [
                    name for name in _RESOURCE_ORDER if factory.graph.closed[name].is_set()
                ],
            },
            "T": {
                "stopReason": terminal.stopReason,
                "errorMessage": terminal.errorMessage,
                "lifecycleError": False,
            },
            "E": {
                "closeCounts": factory.graph.close_counts,
                "requestCount": len(factory.graph.requests)
                + len(factory.success.requests),
                "retries": factory.retries,
            },
            "C": {
                "cancelledWaiters": cancelled_waiters,
                "idleBeforeReuse": idle_before_reuse,
                "reuseStopReason": reused.stopReason,
            },
        }
        await session.dispose()
        return observation
    finally:
        httpx.AsyncClient = original_client  # type: ignore[misc]
        httpx.AsyncHTTPTransport = original_transport  # type: ignore[misc]
        if before is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = before


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        observation = await asyncio.wait_for(
            run_scenario(Path(directory)),
            timeout=10.0,
        )
    print(json.dumps({"reference.deepseek-nested-cancellation-cleanup": observation}))


if __name__ == "__main__":
    asyncio.run(main())
