from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
import os
from typing import Any

import httpx

from oh_my_core import Agent, AgentOptions, AgentState
from oh_my_llm import (
    AbortSignal,
    AssistantMessage,
    AssistantMessageErrorEvent,
    AssistantMessageEvent,
    Context,
    LifecycleError,
    Model,
    SimpleStreamOptions,
    TextContent,
    UserMessage,
    createModels,
)
from oh_my_llm.providers.deepseek import deepseekProvider


def _sse(*payloads: dict[str, Any] | str) -> bytes:
    return b"".join(
        b"data: [DONE]\n\n"
        if payload == "[DONE]"
        else b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n"
        for payload in payloads
    )


_SUCCESS = _sse(
    {
        "id": "fresh-response",
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
        "id": "fresh-response",
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
_PARTIAL_THEN_MALFORMED = (
    _sse(
        {
            "id": "safe-response-id",
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "safe partial"},
                    "finish_reason": None,
                }
            ],
        }
    )
    + b"data: not-json\n\n"
)


class _SequenceSpy:
    def __init__(self, *responses: tuple[int, bytes]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.client_count = 0

    def install(self) -> type[httpx.AsyncClient]:
        original = httpx.AsyncClient
        spy = self

        class FakeClient(original):  # type: ignore[valid-type,misc]
            def __init__(self, **kwargs: Any) -> None:
                spy.client_count += 1
                status, body = spy.responses.pop(0)
                kwargs = dict(kwargs)
                kwargs["transport"] = httpx.MockTransport(
                    lambda request: spy._handle(request, status, body)
                )
                super().__init__(**kwargs)

        setattr(httpx, "AsyncClient", FakeClient)
        return original

    def _handle(
        self,
        request: httpx.Request,
        status: int,
        body: bytes,
    ) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(status, content=body, request=request)


def _deepseek() -> tuple[Any, Model, Context]:
    models = createModels()
    models.setProvider(deepseekProvider())
    model = models.getModel("deepseek", "deepseek-v4-flash")
    assert model is not None
    context = Context(messages=(UserMessage(content="Hello", timestamp=0),))
    return models, model, context


def _deepseek_agent(models: Any, model: Model) -> Agent:
    async def stream_fn(
        selected: Model,
        context: Context,
        options: SimpleStreamOptions | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del signal
        async for event in models.streamSimple(selected, context, options):
            yield event

    return Agent(
        AgentOptions(initialState=AgentState(model=model), streamFn=stream_fn)
    )


async def _collect(stream: Any) -> list[AssistantMessageEvent]:
    return [event async for event in stream]


async def _classification() -> dict[str, object]:
    secret = "SECRET_CLASSIFICATION_CANARY"
    spy = _SequenceSpy(
        (401, secret.encode()),
        (503, secret.encode()),
        (200, _PARTIAL_THEN_MALFORMED),
    )
    original = spy.install()
    try:
        models, model, context = _deepseek()
        os.environ.pop("DEEPSEEK_API_KEY", None)
        missing_events = await _collect(models.streamSimple(model, context))
        missing = missing_events[-1]
        assert isinstance(missing, AssistantMessageErrorEvent)

        os.environ["DEEPSEEK_API_KEY"] = secret
        rejected = await models.completeSimple(model, context)
        provider = await models.complete(model, context)
        stream = models.stream(model, context)
        malformed = await stream.result()
        malformed_events = [event async for event in stream]

        public = (
            f"{missing_events!s}{missing_events!r}{rejected!s}{provider!s}"
            f"{malformed_events!s}{malformed_events!r}{malformed!s}{malformed!r}"
        )
        return {
            "A": "admitted",
            "L": {
                "preStart": [event.type for event in missing_events],
                "postStart": [event.type for event in malformed_events],
            },
            "T": {
                "missingAuth": missing.error.errorMessage,
                "httpAuth": rejected.errorMessage,
                "provider": provider.errorMessage,
                "stream": malformed.errorMessage,
                "partial": malformed.content[0].text,
                "responseId": malformed.responseId,
                "secretLeaked": secret in public,
            },
            "E": {
                "requestCount": len(spy.requests),
                "clientCount": spy.client_count,
            },
            "C": "terminal_assistant_all_helpers",
        }
    finally:
        setattr(httpx, "AsyncClient", original)


async def _reuse() -> dict[str, object]:
    spy = _SequenceSpy((503, b"private provider prose"), (200, _SUCCESS))
    original = spy.install()
    try:
        models, model, _ = _deepseek()

        agent = _deepseek_agent(models, model)
        os.environ["DEEPSEEK_API_KEY"] = "first-key"
        await agent.prompt("provider failure")
        provider_error = agent.state.messages[-1]
        assert isinstance(provider_error, AssistantMessage)

        os.environ.pop("DEEPSEEK_API_KEY", None)
        await agent.prompt("missing auth")
        auth_error = agent.state.messages[-1]
        assert isinstance(auth_error, AssistantMessage)
        count_after_removal = len(spy.requests)

        os.environ["DEEPSEEK_API_KEY"] = "rotated-key"
        await agent.prompt("fresh success")
        recovered = agent.state.messages[-1]
        assert isinstance(recovered, AssistantMessage)
        return {
            "A": "caller_started_runs",
            "L": [],
            "T": {
                "provider": provider_error.errorMessage,
                "removedKey": auth_error.errorMessage,
                "recovered": recovered.content == (TextContent(text="recovered"),),
            },
            "E": {
                "requestCount": len(spy.requests),
                "requestCountAfterRemoval": count_after_removal,
                "oneAttemptPerActivatedRun": len(spy.requests) == 2,
            },
            "C": {
                "idleAfterFailure": agent.state.isStreaming is False,
                "freshRunAfterFailure": recovered.stopReason == "stop",
                "keyReread": [
                    request.headers["Authorization"] for request in spy.requests
                ]
                == ["Bearer first-key", "Bearer rotated-key"],
            },
        }
    finally:
        setattr(httpx, "AsyncClient", original)


class _BarrierByteStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
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
        await self.allow_close.wait()
        self.closed.set()


class _BarrierTransport(httpx.AsyncBaseTransport):
    def __init__(self, blocked: bool) -> None:
        self.stream = _BarrierByteStream() if blocked else None
        self.requests: list[httpx.Request] = []
        self.closed = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.stream is not None:
            return httpx.Response(200, stream=self.stream, request=request)
        return httpx.Response(200, content=_SUCCESS, request=request)

    async def aclose(self) -> None:
        self.closed.set()


class _TransportFactory:
    def __init__(self) -> None:
        self.transports: list[_BarrierTransport] = []
        self.retries: list[int] = []
        self.created = asyncio.Event()

    def __call__(self, *, retries: int) -> _BarrierTransport:
        self.retries.append(retries)
        transport = _BarrierTransport(blocked=not self.transports)
        self.transports.append(transport)
        self.created.set()
        return transport


async def _cancellation() -> dict[str, object]:
    original_transport = httpx.AsyncHTTPTransport
    factory = _TransportFactory()
    setattr(httpx, "AsyncHTTPTransport", factory)
    try:
        os.environ["DEEPSEEK_API_KEY"] = "cancellation-key"
        models, model, _ = _deepseek()

        agent = _deepseek_agent(models, model)
        operation = asyncio.create_task(agent.prompt("cancel"))
        await factory.created.wait()
        blocked = factory.transports[0]
        assert blocked.stream is not None
        await blocked.stream.read_started.wait()
        agent.abort()
        await blocked.stream.close_started.wait()
        try:
            await agent.prompt("too early")
        except LifecycleError as error:
            busy_before_cleanup = error.code == "busy"
        else:
            busy_before_cleanup = False
        blocked.stream.allow_close.set()
        await operation
        terminal = agent.state.messages[-1]
        assert isinstance(terminal, AssistantMessage)
        cleanup_before_terminal = blocked.stream.closed.is_set() and blocked.closed.is_set()

        await agent.prompt("fresh")
        recovered = agent.state.messages[-1]
        assert isinstance(recovered, AssistantMessage)
        return {
            "A": "current_run_captured",
            "L": {"cleanupBeforeTerminal": cleanup_before_terminal},
            "T": {
                "stopReason": terminal.stopReason,
                "errorMessage": terminal.errorMessage,
            },
            "E": {
                "requestCounts": [
                    len(transport.requests) for transport in factory.transports
                ],
                "transportRetries": factory.retries,
            },
            "C": {
                "busyBeforeCleanup": busy_before_cleanup,
                "responseClosed": blocked.stream.closed.is_set(),
                "transportClosed": all(
                    transport.closed.is_set() for transport in factory.transports
                ),
                "freshRun": recovered.stopReason == "stop",
            },
        }
    finally:
        setattr(httpx, "AsyncHTTPTransport", original_transport)


async def _main() -> dict[str, object]:
    return {
        "reference.deepseek-failure-classification": await _classification(),
        "reference.deepseek-failure-reuse": await _reuse(),
        "reference.deepseek-cancellation-settlement": await _cancellation(),
    }


def main() -> None:
    previous_key = os.environ.get("DEEPSEEK_API_KEY")
    try:
        print(json.dumps(asyncio.run(_main()), sort_keys=True))
    finally:
        if previous_key is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = previous_key


if __name__ == "__main__":
    main()
