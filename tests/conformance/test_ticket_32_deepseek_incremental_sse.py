from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from oh_my_coding_agent import (
    AgentSessionEvent,
    CreateAgentSessionOptions,
    SessionManager,
    createAgentSession,
)
from oh_my_llm import (
    AssistantMessage,
    AssistantMessageDoneEvent,
    AssistantMessageErrorEvent,
    AssistantMessageStartEvent,
    AssistantMessageTextDeltaEvent,
    AssistantMessageTextEndEvent,
    AssistantMessageTextStartEvent,
    AssistantMessageToolCallDeltaEvent,
    AssistantMessageToolCallStartEvent,
    Context,
    TextContent,
    ToolCall,
    UserMessage,
    createModels,
)
from oh_my_llm.providers.deepseek import deepseekProvider
from ticket_32_scenario import (
    _GatedTransport,
    _TEXT_HEAD,
    _TEXT_TAIL,
    _TransportFactory,
    run_scenario,
)


ROOT = Path(__file__).parents[2]
_TERMINAL_USAGE = {
    "prompt_tokens": 12,
    "completion_tokens": 2,
    "total_tokens": 14,
    "prompt_cache_hit_tokens": 4,
}
_TOOL_HEAD = (
    b'data: {"id":"chatcmpl-tool","model":"deepseek-v4-flash","choices":[{"index":0,'
    b'"delta":{"tool_calls":[{"index":0,"id":"call-1","type":"function",'
    b'"function":{"name":"echo","arguments":"{\\"n\\":1}"}}]},'
    b'"finish_reason":null}]}\n\n'
)
_TOOL_TAIL = (
    b'data: {"id":"chatcmpl-tool","model":"deepseek-v4-flash",'
    b'"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],'
    b'"usage":{"prompt_tokens":8,"completion_tokens":3,"total_tokens":11,'
    b'"prompt_cache_hit_tokens":0}}\n\n'
    b"data: [DONE]\n\n"
)


def _install_factory(
    monkeypatch: pytest.MonkeyPatch, factory: _TransportFactory
) -> None:
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", factory)


def _models_context() -> tuple[Any, Any, Context]:
    models = createModels()
    models.setProvider(deepseekProvider())
    model = models.getModel("deepseek", "deepseek-v4-flash")
    assert model is not None
    return (
        models,
        model,
        Context(messages=(UserMessage(content="Hello", timestamp=0),)),
    )


def _sse(*payloads: dict[str, Any] | str, separator: bytes = b"\n\n") -> bytes:
    chunks: list[bytes] = []
    for payload in payloads:
        if payload == "[DONE]":
            chunks.append(b"data: [DONE]" + separator)
        else:
            chunks.append(
                b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + separator
            )
    return b"".join(chunks)


def _split_on_marker(body: bytes, marker: bytes, offset: int) -> tuple[bytes, bytes]:
    index = body.find(marker)
    assert index >= 0
    split = index + offset
    return (body[:split], body[split:])


def _text_payloads() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    return (
        {
            "id": "chatcmpl-incremental",
            "model": "deepseek-v4-flash",
            "choices": [
                {"index": 0, "delta": {"content": "Hel"}, "finish_reason": None}
            ],
        },
        {
            "id": "chatcmpl-incremental",
            "model": "deepseek-v4-flash",
            "choices": [
                {"index": 0, "delta": {"content": "lo"}, "finish_reason": None}
            ],
        },
        {
            "id": "chatcmpl-incremental",
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": _TERMINAL_USAGE,
        },
        "[DONE]",
    )


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            if chunk:
                yield chunk

    async def aclose(self) -> None:
        return


class _ChunkFactory:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks
        self.retries: list[int] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, *, retries: int) -> httpx.AsyncBaseTransport:
        self.retries.append(retries)
        owner = self

        class Transport(httpx.AsyncBaseTransport):
            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                owner.requests.append(request)
                return httpx.Response(
                    200, stream=_ChunkStream(owner.chunks), request=request
                )

            async def aclose(self) -> None:
                return

        return Transport()


async def _collect(stream: Any) -> list[Any]:
    return [event async for event in stream]


async def _consume_until_delta(
    models: Any, model: Any, context: Context
) -> tuple[list[Any], asyncio.Task[None]]:
    events: list[Any] = []
    delta_seen = asyncio.Event()

    async def consume() -> None:
        async for event in models.streamSimple(model, context):
            events.append(event)
            if isinstance(event, AssistantMessageTextDeltaEvent) and not delta_seen.is_set():
                delta_seen.set()

    consumer = asyncio.create_task(consume())
    await delta_seen.wait()
    return events, consumer


def test_stream_simple_observes_text_delta_before_http_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    factory = _TransportFactory()
    _install_factory(monkeypatch, factory)
    models, model, context = _models_context()

    async def scenario() -> None:
        events, consumer = await _consume_until_delta(models, model, context)
        assert factory.stream.release_tail.is_set() is False
        assert factory.stream.tail_requested.is_set() is True
        assert [type(event) for event in events] == [
            AssistantMessageStartEvent,
            AssistantMessageTextStartEvent,
            AssistantMessageTextDeltaEvent,
        ]
        assert events[-1].delta == "Hel"
        factory.stream.release_tail.set()
        await consumer
        assert [type(event) for event in events] == [
            AssistantMessageStartEvent,
            AssistantMessageTextStartEvent,
            AssistantMessageTextDeltaEvent,
            AssistantMessageTextDeltaEvent,
            AssistantMessageTextEndEvent,
            AssistantMessageDoneEvent,
        ]
        done = events[-1]
        assert isinstance(done, AssistantMessageDoneEvent)
        assert done.message.content == (TextContent(text="Hello"),)
        assert factory.stream.closed.is_set()
        assert factory.retries == [0]

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))
    assert len(factory.transports) == 1
    first_transport = factory.transports[0]
    assert isinstance(first_transport, _GatedTransport)
    assert len(first_transport.requests) == 1


def test_incomplete_sse_frame_is_private_until_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    cafe_event = _sse(
        {
            "id": "chatcmpl-incremental",
            "model": "deepseek-v4-flash",
            "choices": [
                {"index": 0, "delta": {"content": "café"}, "finish_reason": None}
            ],
        }
    )
    split_at = cafe_event.find("é".encode("utf-8")) + 1
    assert 0 < split_at < len(cafe_event)
    factory = _TransportFactory(head=cafe_event[:split_at], tail=cafe_event[split_at:] + _TEXT_TAIL)
    _install_factory(monkeypatch, factory)
    models, model, context = _models_context()

    async def scenario() -> None:
        events: list[Any] = []
        started = asyncio.Event()

        async def consume() -> None:
            async for event in models.streamSimple(model, context):
                events.append(event)
                if isinstance(event, AssistantMessageStartEvent):
                    started.set()

        consumer = asyncio.create_task(consume())
        await started.wait()
        await factory.stream.head_sent.wait()
        await factory.stream.tail_requested.wait()
        assert [type(event) for event in events] == [AssistantMessageStartEvent]
        factory.stream.release_tail.set()
        await consumer
        deltas = [
            event.delta
            for event in events
            if isinstance(event, AssistantMessageTextDeltaEvent)
        ]
        assert deltas[0] == "café"
        done = events[-1]
        assert isinstance(done, AssistantMessageDoneEvent)
        assert done.message.content == (TextContent(text="cafélo"),)

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


@pytest.mark.parametrize(
    "chunks",
    (
        pytest.param((_TEXT_HEAD + _TEXT_TAIL,), id="whole-body"),
        pytest.param(
            tuple(bytes([byte]) for byte in (_TEXT_HEAD + _TEXT_TAIL)),
            id="byte-at-a-time",
        ),
        pytest.param(
            _split_on_marker(_sse(*_text_payloads(), separator=b"\r\n\r\n"), b"\r\n\r\n", 2),
            id="crlf-separator",
        ),
        pytest.param(
            _split_on_marker(_TEXT_HEAD + _TEXT_TAIL, b'"Hel"', 3),
            id="json-payload-boundary",
        ),
    ),
)
def test_chunked_sse_matches_whole_body_terminal(
    monkeypatch: pytest.MonkeyPatch,
    chunks: tuple[bytes, ...],
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    factory = _ChunkFactory(chunks)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", factory)
    models, model, context = _models_context()

    events = asyncio.run(
        asyncio.wait_for(_collect(models.streamSimple(model, context)), timeout=10.0)
    )
    assert [type(event) for event in events] == [
        AssistantMessageStartEvent,
        AssistantMessageTextStartEvent,
        AssistantMessageTextDeltaEvent,
        AssistantMessageTextDeltaEvent,
        AssistantMessageTextEndEvent,
        AssistantMessageDoneEvent,
    ]
    done = events[-1]
    assert isinstance(done, AssistantMessageDoneEvent)
    assert done.message.content == (TextContent(text="Hello"),)
    assert done.message.usage.totalTokens == 14
    assert factory.retries == [0]


def test_tool_event_is_observable_before_http_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    factory = _TransportFactory(head=_TOOL_HEAD, tail=_TOOL_TAIL)
    _install_factory(monkeypatch, factory)
    models, model, context = _models_context()

    async def scenario() -> None:
        events: list[Any] = []
        tool_seen = asyncio.Event()

        async def consume() -> None:
            async for event in models.streamSimple(model, context):
                events.append(event)
                if isinstance(event, AssistantMessageToolCallDeltaEvent):
                    tool_seen.set()

        consumer = asyncio.create_task(consume())
        await tool_seen.wait()
        assert factory.stream.release_tail.is_set() is False
        assert [type(event) for event in events] == [
            AssistantMessageStartEvent,
            AssistantMessageToolCallStartEvent,
            AssistantMessageToolCallDeltaEvent,
        ]
        factory.stream.release_tail.set()
        await consumer
        done = events[-1]
        assert isinstance(done, AssistantMessageDoneEvent)
        assert isinstance(done.message.content[0], ToolCall)
        assert done.message.content[0].name == "echo"
        assert done.reason == "toolUse"

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


@pytest.mark.parametrize(
    "helper", ("streamSimple", "completeSimple", "stream", "complete")
)
def test_four_helpers_keep_terminal_contract_on_gated_stream(
    monkeypatch: pytest.MonkeyPatch,
    helper: str,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    factory = _TransportFactory()
    _install_factory(monkeypatch, factory)
    models, model, context = _models_context()

    async def scenario() -> None:
        async def release() -> None:
            await factory.stream.tail_requested.wait()
            factory.stream.release_tail.set()

        releaser = asyncio.create_task(release())
        if helper == "streamSimple":
            events = await _collect(models.streamSimple(model, context))
            terminal = events[-1]
            assert isinstance(terminal, AssistantMessageDoneEvent)
            message = terminal.message
        elif helper == "completeSimple":
            message = await models.completeSimple(model, context)
        elif helper == "stream":
            stream = models.stream(model, context)
            message = await stream.result()
        else:
            message = await models.complete(model, context)
        await releaser
        assert message.stopReason == "stop"
        assert message.content == (TextContent(text="Hello"),)
        assert message.usage.totalTokens == 14
        assert "configured" not in f"{message!s}{message!r}"

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))
    assert factory.retries == [0]


def test_truncated_stream_and_post_done_payloads_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    models, model, context = _models_context()

    async def truncated() -> None:
        factory = _ChunkFactory((_TEXT_HEAD,))
        monkeypatch.setattr(httpx, "AsyncHTTPTransport", factory)
        events = await _collect(models.streamSimple(model, context))
        terminal = events[-1]
        assert isinstance(terminal, AssistantMessageErrorEvent)
        assert terminal.error.errorMessage == "DeepSeek stream failed"

    async def extra_after_done() -> None:
        factory = _ChunkFactory((_TEXT_HEAD + _TEXT_TAIL, b'data: {"id":"late"}\n\n'))
        monkeypatch.setattr(httpx, "AsyncHTTPTransport", factory)
        events = await _collect(models.streamSimple(model, context))
        terminal = events[-1]
        assert isinstance(terminal, AssistantMessageErrorEvent)
        assert terminal.error.errorMessage == "DeepSeek stream failed"

    asyncio.run(asyncio.wait_for(truncated(), timeout=10.0))
    asyncio.run(asyncio.wait_for(extra_after_done(), timeout=10.0))


def test_product_session_aborts_after_text_delta_while_transport_is_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    factory = _TransportFactory(gate_close=True)
    _install_factory(monkeypatch, factory)

    async def scenario() -> None:
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    sessionManager=SessionManager.inMemory(os.fspath(tmp_path))
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
        prompt = asyncio.create_task(session.prompt("cancel after delta"))
        await delta_seen.wait()
        assert factory.stream.release_tail.is_set() is False
        assert factory.stream.tail_requested.is_set() is True
        abort = asyncio.create_task(session.abort())
        await factory.stream.close_started.wait()
        assert session.isIdle is False
        assert prompt.done() is False
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
        assert len(aborted_ends) == 1
        assert len(agent_ends) == 1
        terminal = session.messages[-1]
        assert isinstance(terminal, AssistantMessage)
        assert terminal.stopReason == "aborted"
        assert terminal.errorMessage == "Operation aborted"
        assert session.isIdle is True
        await session.prompt("reuse")
        reused = session.messages[-1]
        assert isinstance(reused, AssistantMessage)
        assert reused.stopReason == "stop"
        assert factory.retries == [0, 0]
        await session.dispose()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


def test_installed_scenario_and_matrix_lock_incremental_observations(
    tmp_path: Path,
) -> None:
    actual = asyncio.run(asyncio.wait_for(run_scenario(tmp_path), timeout=10.0))
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    expected = {
        case["id"]: case["omhExpectation"]
        for case in corpus["cases"]
        if case["id"]
        in {
            "reference.deepseek-incremental-sse-delivery",
            "reference.deepseek-active-stream-abort-after-delta",
        }
    }
    assert actual == expected

    matrix = json.loads((ROOT / "conformance/obligation-matrix.json").read_text())
    rows = {
        item["id"]: item
        for item in matrix["obligations"]
        if item["id"]
        in {
            "omh-v0.deepseek-incremental-sse-delivery",
            "omh-v0.deepseek-active-stream-abort-after-delta",
        }
    }
    assert set(rows) == {
        "omh-v0.deepseek-incremental-sse-delivery",
        "omh-v0.deepseek-active-stream-abort-after-delta",
    }
    for row in rows.values():
        assert row["authority"].endswith(
            ".scratch/omh-v0/tickets/32-stream-deepseek-sse-incrementally.md"
        )
        assert row["executableRunners"] == [
            "ticket-32-deepseek-sse",
            "ticket-32-installed",
        ]
