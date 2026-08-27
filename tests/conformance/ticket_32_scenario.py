from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import httpx

from oh_my_coding_agent import (
    AgentSessionEvent,
    CreateAgentSessionOptions,
    SessionManager,
    createAgentSession,
)
from oh_my_llm import (
    AssistantMessage,
    AssistantMessageTextDeltaEvent,
    Context,
    UserMessage,
    createModels,
)
from oh_my_llm.providers.deepseek import deepseekProvider


_TEXT_HEAD = (
    b'data: {"id":"chatcmpl-incremental","model":"deepseek-v4-flash",'
    b'"choices":[{"index":0,"delta":{"content":"Hel"},"finish_reason":null}]}\n\n'
)
_TEXT_TAIL = (
    b'data: {"id":"chatcmpl-incremental","model":"deepseek-v4-flash",'
    b'"choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-incremental","model":"deepseek-v4-flash",'
    b'"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":12,"completion_tokens":2,"total_tokens":14,'
    b'"prompt_cache_hit_tokens":4}}\n\n'
    b"data: [DONE]\n\n"
)
_SUCCESS_SSE = (
    b'data: {"id":"installed-fresh","model":"deepseek-v4-flash",'
    b'"choices":[{"index":0,"delta":{"content":"fresh"},"finish_reason":null}]}\n\n'
    b'data: {"id":"installed-fresh","model":"deepseek-v4-flash",'
    b'"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3,'
    b'"prompt_cache_hit_tokens":0}}\n\n'
    b"data: [DONE]\n\n"
)


class _GatedByteStream(httpx.AsyncByteStream):
    def __init__(
        self, head: bytes, tail: bytes, *, gate_close: bool = False
    ) -> None:
        self.head = head
        self.tail = tail
        self.gate_close = gate_close
        self.head_sent = asyncio.Event()
        self.tail_requested = asyncio.Event()
        self.release_tail = asyncio.Event()
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.closed = asyncio.Event()
        if not gate_close:
            self.allow_close.set()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.head
        self.head_sent.set()
        self.tail_requested.set()
        await self.release_tail.wait()
        yield self.tail

    async def aclose(self) -> None:
        self.close_started.set()
        await self.allow_close.wait()
        self.closed.set()


class _GatedTransport(httpx.AsyncBaseTransport):
    def __init__(self, stream: _GatedByteStream) -> None:
        self.stream = stream
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, stream=self.stream, request=request)

    async def aclose(self) -> None:
        return


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
    def __init__(
        self,
        *,
        head: bytes = _TEXT_HEAD,
        tail: bytes = _TEXT_TAIL,
        gate_close: bool = False,
    ) -> None:
        self.stream = _GatedByteStream(head, tail, gate_close=gate_close)
        self.success = _SuccessTransport()
        self.transports: list[httpx.AsyncBaseTransport] = []
        self.retries: list[int] = []

    def __call__(self, *, retries: int) -> httpx.AsyncBaseTransport:
        self.retries.append(retries)
        transport: httpx.AsyncBaseTransport = (
            _GatedTransport(self.stream) if not self.transports else self.success
        )
        self.transports.append(transport)
        return transport


def _install_transport_factory(factory: _TransportFactory) -> tuple[Any, Any]:
    original_client = httpx.AsyncClient
    original_transport = httpx.AsyncHTTPTransport
    httpx.AsyncHTTPTransport = factory  # type: ignore[assignment,misc]
    return original_client, original_transport


def _restore_httpx(original_client: Any, original_transport: Any) -> None:
    httpx.AsyncClient = original_client  # type: ignore[misc]
    httpx.AsyncHTTPTransport = original_transport  # type: ignore[misc]


async def run_incremental_delivery() -> dict[str, object]:
    factory = _TransportFactory()
    original_client, original_transport = _install_transport_factory(factory)
    before = os.environ.get("DEEPSEEK_API_KEY")
    os.environ["DEEPSEEK_API_KEY"] = "installed-conformance-canary"
    try:
        models = createModels()
        models.setProvider(deepseekProvider())
        model = models.getModel("deepseek", "deepseek-v4-flash")
        assert model is not None
        context = Context(messages=(UserMessage(content="Hello", timestamp=0),))
        events: list[Any] = []
        delta_seen = asyncio.Event()

        async def consume() -> None:
            async for event in models.streamSimple(model, context):
                events.append(event)
                if (
                    isinstance(event, AssistantMessageTextDeltaEvent)
                    and not delta_seen.is_set()
                ):
                    delta_seen.set()

        consumer = asyncio.create_task(consume())
        await delta_seen.wait()
        later_read_pending = factory.stream.tail_requested.is_set()
        released_before_delta = factory.stream.release_tail.is_set()
        pre_eof_types = [event.type for event in events]
        factory.stream.release_tail.set()
        await consumer
        terminal = events[-1]
        leaked = "installed-conformance-canary" in f"{events!s}{events!r}"
        first_transport = factory.transports[0]
        assert isinstance(first_transport, _GatedTransport)
        return {
            "A": "public_models_pre_eof_text_delta",
            "L": pre_eof_types,
            "T": {
                "delta": next(
                    event.delta
                    for event in events
                    if isinstance(event, AssistantMessageTextDeltaEvent)
                ),
                "stopReason": terminal.reason
                if terminal.type == "done"
                else getattr(terminal, "reason", None),
                "text": terminal.message.content[0].text
                if terminal.type == "done"
                else None,
            },
            "E": {
                "requestCount": len(first_transport.requests),
                "retries": factory.retries,
                "secretLeaked": leaked,
            },
            "C": {
                "laterReadPending": later_read_pending,
                "releasedBeforeDelta": released_before_delta,
                "closed": factory.stream.closed.is_set(),
            },
        }
    finally:
        _restore_httpx(original_client, original_transport)
        if before is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = before


async def run_abort_after_delta(root: Path) -> dict[str, object]:
    factory = _TransportFactory(gate_close=True)
    original_client, original_transport = _install_transport_factory(factory)
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
        events: list[AgentSessionEvent] = []
        delta_seen = asyncio.Event()

        def listener(event: AgentSessionEvent) -> None:
            events.append(event)
            if isinstance(event, AgentSessionEvent.MessageUpdate) and isinstance(
                event.assistantMessageEvent, AssistantMessageTextDeltaEvent
            ):
                delta_seen.set()

        session.subscribe(listener)
        prompt = asyncio.create_task(session.prompt("cancel after incremental delta"))
        await delta_seen.wait()
        later_read_pending = factory.stream.tail_requested.is_set()
        released_before_abort = factory.stream.release_tail.is_set()
        delta_before_eof = not released_before_abort
        abort = asyncio.create_task(session.abort())
        await factory.stream.close_started.wait()
        idle_during_cleanup = session.isIdle
        factory.stream.allow_close.set()
        await prompt
        await abort
        aborted_ends = [
            event
            for event in events
            if isinstance(event, AgentSessionEvent.MessageEnd)
            and isinstance(event.message, AssistantMessage)
            and event.message.stopReason == "aborted"
        ]
        agent_ends = [
            event for event in events if isinstance(event, AgentSessionEvent.AgentEnd)
        ]
        terminal = session.messages[-1]
        assert isinstance(terminal, AssistantMessage)
        idle_before_reuse = session.isIdle
        await session.prompt("reuse after active-stream abort")
        reused = session.messages[-1]
        assert isinstance(reused, AssistantMessage)
        leaked = "installed-conformance-canary" in f"{events!s}{events!r}{terminal!s}"
        first_transport = factory.transports[0]
        assert isinstance(first_transport, _GatedTransport)
        observation: dict[str, object] = {
            "A": "public_product_session_abort_after_delta",
            "L": {
                "deltaBeforeEof": delta_before_eof,
                "laterReadPending": later_read_pending,
                "abortedMessageEnds": len(aborted_ends),
                "agentEnds": len(agent_ends),
            },
            "T": {
                "stopReason": terminal.stopReason,
                "errorMessage": terminal.errorMessage,
                "lifecycleError": False,
            },
            "E": {
                "requestCount": len(first_transport.requests) + len(factory.success.requests),
                "retries": factory.retries,
                "secretLeaked": leaked,
            },
            "C": {
                "releasedBeforeAbort": released_before_abort,
                "idleDuringCleanup": idle_during_cleanup,
                "idleBeforeReuse": idle_before_reuse,
                "reuseStopReason": reused.stopReason,
            },
        }
        await session.dispose()
        return observation
    finally:
        _restore_httpx(original_client, original_transport)
        if before is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = before


async def run_scenario(root: Path) -> dict[str, object]:
    return {
        "reference.deepseek-incremental-sse-delivery": await run_incremental_delivery(),
        "reference.deepseek-active-stream-abort-after-delta": await run_abort_after_delta(
            root
        ),
    }


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        observation = await asyncio.wait_for(
            run_scenario(Path(directory)),
            timeout=10.0,
        )
    print(json.dumps(observation, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
