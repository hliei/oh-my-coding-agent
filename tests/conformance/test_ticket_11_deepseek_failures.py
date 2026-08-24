from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from oh_my_core import Agent, AgentOptions, AgentState
from oh_my_llm import (
    AbortSignal,
    AssistantMessage,
    AssistantMessageErrorEvent,
    AssistantMessageStartEvent,
    AssistantMessageTextDeltaEvent,
    AssistantMessageTextStartEvent,
    Context,
    LifecycleError,
    Model,
    SimpleStreamOptions,
    TextContent,
    UserMessage,
    createModels,
)
from oh_my_llm.providers.deepseek import deepseekProvider


ROOT = Path(__file__).parents[2]


class _HttpSpy:
    def __init__(self, *, status: int = 200, body: bytes = b"") -> None:
        self.status = status
        self.body = body
        self.requests: list[httpx.Request] = []
        self.client_kwargs: list[dict[str, Any]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = self

        class FakeClient(httpx.AsyncClient):
            def __init__(self, **kwargs: Any) -> None:
                spy.client_kwargs.append(dict(kwargs))
                kwargs = dict(kwargs)
                kwargs["transport"] = httpx.MockTransport(spy._handle)
                super().__init__(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            self.status,
            content=self.body,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )


class _TransportFailureSpy:
    def __init__(self, kind: str, secret: str) -> None:
        self.kind = kind
        self.secret = secret
        self.request_count = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = self

        class FakeClient(httpx.AsyncClient):
            def __init__(self, **kwargs: Any) -> None:
                kwargs = dict(kwargs)
                kwargs["transport"] = httpx.MockTransport(spy._handle)
                super().__init__(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.request_count += 1
        if self.kind == "connection":
            raise httpx.ConnectError(self.secret, request=request)
        if self.kind == "timeout":
            raise httpx.ReadTimeout(self.secret, request=request)
        raise RuntimeError(self.secret)


class _BarrierByteStream(httpx.AsyncByteStream):
    def __init__(self, close_failure: str | None = None) -> None:
        self.close_failure = close_failure
        self.read_started = asyncio.Event()
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.read_started.set()
        await asyncio.Event().wait()
        yield b""

    async def aclose(self) -> None:
        self.close_started.set()
        if self.close_failure is not None:
            self.closed.set()
            raise RuntimeError(self.close_failure)
        await self.allow_close.wait()
        self.closed.set()


class _BarrierTransport(httpx.AsyncBaseTransport):
    def __init__(self, mode: str, success_body: bytes) -> None:
        self.mode = mode
        self.success_body = success_body
        self.requests: list[httpx.Request] = []
        self.stream = (
            _BarrierByteStream(
                "SECRET_CLEANUP_CANARY"
                if mode == "blocked_cleanup_failure"
                else None
            )
            if mode in ("blocked", "blocked_cleanup_failure")
            else None
        )
        self.closed = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.stream is not None:
            return httpx.Response(200, stream=self.stream, request=request)
        if self.mode == "provider_failure":
            return httpx.Response(503, content=b"private provider prose", request=request)
        return httpx.Response(200, content=self.success_body, request=request)

    async def aclose(self) -> None:
        self.closed.set()


class _TransportFactory:
    def __init__(self, modes: list[str], success_body: bytes) -> None:
        self.modes = modes
        self.success_body = success_body
        self.retries: list[int] = []
        self.transports: list[_BarrierTransport] = []
        self.created = asyncio.Event()

    def __call__(self, *, retries: int) -> _BarrierTransport:
        self.retries.append(retries)
        transport = _BarrierTransport(self.modes.pop(0), self.success_body)
        self.transports.append(transport)
        self.created.set()
        return transport


def _deepseek() -> tuple[Any, Any, Context]:
    models = createModels()
    models.setProvider(deepseekProvider())
    model = models.getModel("deepseek", "deepseek-v4-flash")
    assert model is not None
    context = Context(messages=(UserMessage(content="Hello", timestamp=0),))
    return models, model, context


def _sse(*payloads: dict[str, Any] | str) -> bytes:
    chunks: list[bytes] = []
    for payload in payloads:
        if payload == "[DONE]":
            chunks.append(b"data: [DONE]\n\n")
        else:
            chunks.append(b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n")
    return b"".join(chunks)


async def _invoke(
    helper: str,
    models: Any,
    model: Any,
    context: Context,
) -> tuple[list[Any], AssistantMessage]:
    if helper == "streamSimple":
        events = [event async for event in models.streamSimple(model, context)]
        terminal = events[-1].error
        return events, terminal
    if helper == "stream":
        stream = models.stream(model, context)
        terminal = await stream.result()
        return [event async for event in stream], terminal
    if helper == "completeSimple":
        return [], await models.completeSimple(model, context)
    assert helper == "complete"
    return [], await models.complete(model, context)


@pytest.mark.parametrize(
    "helper", ("streamSimple", "completeSimple", "stream", "complete")
)
def test_missing_auth_settles_every_helper_as_one_redacted_terminal(
    monkeypatch: pytest.MonkeyPatch,
    helper: str,
) -> None:
    spy = _HttpSpy()
    spy.install(monkeypatch)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    models, model, context = _deepseek()

    events, terminal = asyncio.run(_invoke(helper, models, model, context))

    assert terminal.stopReason == "error"
    assert terminal.errorMessage == "DeepSeek authentication failed"
    assert terminal.content == ()
    assert terminal.usage.totalTokens == 0
    if events:
        assert [type(event) for event in events] == [AssistantMessageErrorEvent]
        assert events[0].reason == "error"
        assert events[0].error is terminal
    assert spy.client_kwargs == []
    assert spy.requests == []


@pytest.mark.parametrize(
    ("status", "public_error"),
    (
        (401, "DeepSeek authentication failed"),
        (403, "DeepSeek authentication failed"),
        (429, "DeepSeek request failed"),
        (503, "DeepSeek request failed"),
    ),
)
@pytest.mark.parametrize(
    "helper", ("streamSimple", "completeSimple", "stream", "complete")
)
def test_http_failures_classify_once_without_leaking_provider_data(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    public_error: str,
    helper: str,
) -> None:
    secret = "SECRET_HTTP_CANARY"
    provider_prose = b"raw provider prose with private account data"
    spy = _HttpSpy(status=status, body=provider_prose)
    spy.install(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    models, model, context = _deepseek()

    events, terminal = asyncio.run(_invoke(helper, models, model, context))

    assert terminal.stopReason == "error"
    assert terminal.errorMessage == public_error
    public_observation = f"{events!s}{events!r}{terminal!s}{terminal!r}"
    assert secret not in public_observation
    assert provider_prose.decode() not in public_observation
    assert len(spy.client_kwargs) == 1
    assert len(spy.requests) == 1


@pytest.mark.parametrize("kind", ("connection", "timeout", "arbitrary"))
def test_transport_failure_is_redacted_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    secret = f"SECRET_{kind.upper()}_CANARY"
    spy = _TransportFailureSpy(kind, secret)
    spy.install(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured-key")
    models, model, context = _deepseek()

    terminal = asyncio.run(models.completeSimple(model, context))

    assert terminal.stopReason == "error"
    assert terminal.errorMessage == "DeepSeek request failed"
    assert secret not in f"{terminal!s}{terminal!r}"
    assert spy.request_count == 1


def test_stream_failure_preserves_latest_legal_content_identity_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "SECRET_STREAM_CANARY"
    usage = {
        "prompt_tokens": 5,
        "completion_tokens": 2,
        "total_tokens": 7,
        "prompt_cache_hit_tokens": 1,
    }
    body = _sse(
        {
            "id": "response-safe-id",
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "safe partial"},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "response-safe-id",
            "model": "deepseek-v4-flash",
            "choices": [],
            "usage": usage,
        },
        {
            "id": secret,
            "model": "deepseek-v4-flash",
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": "unknown-private-value"}
            ],
        },
    )
    spy = _HttpSpy(body=body)
    spy.install(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured-key")
    models, model, context = _deepseek()

    events = asyncio.run(_invoke("streamSimple", models, model, context))[0]
    terminal = events[-1].error

    assert [type(event) for event in events] == [
        AssistantMessageStartEvent,
        AssistantMessageTextStartEvent,
        AssistantMessageTextDeltaEvent,
        AssistantMessageErrorEvent,
    ]
    assert terminal.content == (TextContent(text="safe partial"),)
    assert terminal.responseId == "response-safe-id"
    assert terminal.responseModel == "deepseek-v4-flash"
    assert terminal.usage.input == 4
    assert terminal.usage.output == 2
    assert terminal.usage.cacheRead == 1
    assert terminal.usage.totalTokens == 7
    assert terminal.stopReason == "error"
    assert terminal.errorMessage == "DeepSeek stream failed"
    assert secret not in f"{events!s}{events!r}"
    assert len(spy.requests) == 1


@pytest.mark.parametrize(
    "body",
    (
        b"data: not-json\n\n",
        _sse(
            {
                "id": "invalid-tool-delta",
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": "not-a-list"},
                        "finish_reason": None,
                    }
                ],
            }
        ),
        _sse(
            {
                "id": "missing-terminal",
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "partial"},
                        "finish_reason": None,
                    }
                ],
            },
            "[DONE]",
        ),
        _sse(
            {
                "id": "unknown-finish",
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "content_filter",
                    }
                ],
            }
        ),
        _sse(
            {
                "id": "unexpected-reasoning",
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"reasoning_content": "private reasoning"},
                        "finish_reason": None,
                    }
                ],
            }
        ),
        _sse(
            {
                "id": "invalid-usage",
                "model": "deepseek-v4-flash",
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 99,
                    "prompt_cache_hit_tokens": 0,
                },
            }
        ),
        _sse(
            {
                "id": "missing-done-sentinel",
                "model": "deepseek-v4-flash",
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                    "prompt_cache_hit_tokens": 0,
                },
            }
        ),
    ),
    ids=(
        "malformed-json",
        "invalid-tool-delta",
        "missing-terminal",
        "unknown-finish",
        "unexpected-reasoning",
        "invalid-usage",
        "missing-done-sentinel",
    ),
)
@pytest.mark.parametrize(
    "helper", ("streamSimple", "completeSimple", "stream", "complete")
)
def test_wire_and_terminal_failures_settle_as_stream_errors(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    helper: str,
) -> None:
    spy = _HttpSpy(body=body)
    spy.install(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured-key")
    models, model, context = _deepseek()

    events, terminal = asyncio.run(_invoke(helper, models, model, context))

    assert terminal.stopReason == "error"
    assert terminal.errorMessage == "DeepSeek stream failed"
    if events:
        assert isinstance(events[-1], AssistantMessageErrorEvent)
        assert events[-1].reason == "error"
        assert events[-1].error is terminal
    assert len(spy.requests) == 1


def test_duplicate_wire_json_key_cannot_replace_identity_with_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "SECRET_DUPLICATE_KEY_CANARY"
    body = (
        b'data: {"id":"safe-id","id":"'
        + secret.encode()
        + b'","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},'
        b'"finish_reason":"stop"}],"usage":{"prompt_tokens":1,'
        b'"completion_tokens":1,"total_tokens":2,"prompt_cache_hit_tokens":0}}\n\n'
        b"data: [DONE]\n\n"
    )
    spy = _HttpSpy(body=body)
    spy.install(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured-key")
    models, model, context = _deepseek()

    events, terminal = asyncio.run(_invoke("streamSimple", models, model, context))

    assert terminal.stopReason == "error"
    assert terminal.errorMessage == "DeepSeek stream failed"
    assert terminal.responseId is None
    assert secret not in f"{events!s}{events!r}{terminal!s}{terminal!r}"


def test_agent_cancellation_drains_deepseek_before_fresh_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    success_body = _sse(
        {
            "id": "recovered-response",
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "recovered"},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "recovered-response",
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "total_tokens": 3,
                "prompt_cache_hit_tokens": 0,
            },
        },
        "[DONE]",
    )
    factory = _TransportFactory(["blocked", "success"], success_body)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", factory)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "first-key")
    models, model, _ = _deepseek()

    async def stream_fn(
        selected: Model,
        context: Context,
        options: SimpleStreamOptions | None,
        signal: AbortSignal,
    ) -> AsyncIterator[Any]:
        del signal
        async for event in models.streamSimple(selected, context, options):
            yield event

    agent = Agent(AgentOptions(initialState=AgentState(model=model), streamFn=stream_fn))

    async def run() -> None:
        first = asyncio.create_task(agent.prompt("block"))
        await factory.created.wait()
        blocked = factory.transports[0]
        assert blocked.stream is not None
        await blocked.stream.read_started.wait()

        agent.abort()
        await blocked.stream.close_started.wait()
        assert not first.done()
        with pytest.raises(LifecycleError) as busy:
            await agent.prompt("too early")
        assert busy.value.code == "busy"

        blocked.stream.allow_close.set()
        await first
        assert blocked.stream.closed.is_set()
        assert blocked.closed.is_set()
        assert agent.state.isStreaming is False
        aborted = agent.state.messages[-1]
        assert isinstance(aborted, AssistantMessage)
        assert aborted.stopReason == "aborted"

        monkeypatch.setenv("DEEPSEEK_API_KEY", "rotated-key")
        await agent.prompt("again")
        recovered = agent.state.messages[-1]
        assert isinstance(recovered, AssistantMessage)
        assert recovered.content == (TextContent(text="recovered"),)
        assert recovered.stopReason == "stop"

    asyncio.run(run())

    assert factory.retries == [0, 0]
    assert len(factory.transports) == 2
    assert [len(transport.requests) for transport in factory.transports] == [1, 1]
    assert factory.transports[0].requests[0].headers["Authorization"] == "Bearer first-key"
    assert factory.transports[1].requests[0].headers["Authorization"] == "Bearer rotated-key"
    assert factory.transports[1].closed.is_set()


def test_agent_reuses_after_provider_failure_and_rereads_removed_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    success_body = _sse(
        {
            "id": "fresh-response",
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "prompt_cache_hit_tokens": 0,
            },
        },
        "[DONE]",
    )
    factory = _TransportFactory(["provider_failure", "success"], success_body)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", factory)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "first-key")
    models, model, _ = _deepseek()

    async def stream_fn(
        selected: Model,
        context: Context,
        options: SimpleStreamOptions | None,
        signal: AbortSignal,
    ) -> AsyncIterator[Any]:
        del signal
        async for event in models.streamSimple(selected, context, options):
            yield event

    agent = Agent(AgentOptions(initialState=AgentState(model=model), streamFn=stream_fn))

    async def run() -> None:
        await agent.prompt("provider failure")
        provider_error = agent.state.messages[-1]
        assert isinstance(provider_error, AssistantMessage)
        assert provider_error.stopReason == "error"
        assert provider_error.errorMessage == "DeepSeek request failed"
        assert agent.state.isStreaming is False

        monkeypatch.delenv("DEEPSEEK_API_KEY")
        await agent.prompt("missing auth")
        auth_error = agent.state.messages[-1]
        assert isinstance(auth_error, AssistantMessage)
        assert auth_error.stopReason == "error"
        assert auth_error.errorMessage == "DeepSeek authentication failed"
        assert len(factory.transports) == 1

        monkeypatch.setenv("DEEPSEEK_API_KEY", "rotated-key")
        await agent.prompt("fresh success")
        recovered = agent.state.messages[-1]
        assert isinstance(recovered, AssistantMessage)
        assert recovered.stopReason == "stop"
        assert recovered.responseId == "fresh-response"

    asyncio.run(run())

    assert factory.retries == [0, 0]
    assert len(factory.transports) == 2
    assert [len(transport.requests) for transport in factory.transports] == [1, 1]
    assert factory.transports[0].requests[0].headers["Authorization"] == "Bearer first-key"
    assert factory.transports[1].requests[0].headers["Authorization"] == "Bearer rotated-key"


def test_cancellation_cleanup_failure_remains_a_lifecycle_carrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _TransportFactory(["blocked_cleanup_failure"], b"")
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", factory)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured-key")
    models, model, _ = _deepseek()

    async def stream_fn(
        selected: Model,
        context: Context,
        options: SimpleStreamOptions | None,
        signal: AbortSignal,
    ) -> AsyncIterator[Any]:
        del signal
        async for event in models.streamSimple(selected, context, options):
            yield event

    agent = Agent(AgentOptions(initialState=AgentState(model=model), streamFn=stream_fn))

    async def run() -> None:
        operation = asyncio.create_task(agent.prompt("cancel"))
        await factory.created.wait()
        transport = factory.transports[0]
        assert transport.stream is not None
        await transport.stream.read_started.wait()

        agent.abort()
        with pytest.raises(LifecycleError) as cleanup:
            await operation

        assert cleanup.value.code == "cleanup"
        assert transport.stream.closed.is_set()
        assert transport.closed.is_set()
        assert agent.state.isStreaming is False
        assert agent.state.errorMessage is None
        assert all(
            not isinstance(message, AssistantMessage) or message.stopReason != "error"
            for message in agent.state.messages
        )

    asyncio.run(run())


def test_ticket_11_conformance_links_failure_reuse_and_cancellation() -> None:
    matrix = json.loads((ROOT / "conformance/obligation-matrix.json").read_text())
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    required = {
        "omh-v0.deepseek-failure-classification": (
            "reference.deepseek-failure-classification"
        ),
        "omh-v0.deepseek-failure-reuse": "reference.deepseek-failure-reuse",
        "omh-v0.deepseek-cancellation-settlement": (
            "reference.deepseek-cancellation-settlement"
        ),
    }
    rows = {row["id"]: row for row in matrix["obligations"]}
    cases = {case["id"]: case for case in corpus["cases"]}
    assert required.keys() <= rows.keys()
    assert set(required.values()) <= cases.keys()
    for obligation, corpus_case in required.items():
        assert rows[obligation]["corpusCase"] == corpus_case
        assert "ticket-11-deepseek-failures" in rows[obligation]["executableCases"]
        assert "ticket-11-installed" in rows[obligation]["executableCases"]
        assert cases[corpus_case]["obligation"] == obligation
