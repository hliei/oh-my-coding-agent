from __future__ import annotations

import asyncio
import inspect
from importlib import import_module
import importlib.util
import json
from typing import Any

import httpx
import pytest

from oh_my_llm import (
    AuthResult,
    AssistantMessageDoneEvent,
    AssistantMessageStartEvent,
    AssistantMessageTextDeltaEvent,
    AssistantMessageTextEndEvent,
    AssistantMessageTextStartEvent,
    Context,
    Model,
    ModelsError,
    Provider,
    TextContent,
    UserMessage,
    createModels,
    fauxProvider,
)
from oh_my_llm.providers.deepseek import deepseekProvider


def test_deepseek_factory_exposes_the_static_one_model_catalog() -> None:
    assert tuple(inspect.signature(deepseekProvider).parameters) == ()
    with pytest.raises(TypeError):
        deepseekProvider("https://example.invalid")  # type: ignore[call-arg]

    provider = deepseekProvider()
    assert type(provider) is Provider
    assert provider.id == "deepseek"
    assert provider.name == "DeepSeek"
    assert {name for name in dir(provider) if not name.startswith("_")} == {"id", "name"}

    models = createModels()
    assert tuple(inspect.signature(createModels).parameters) == ()
    assert models.getProviders() == ()
    assert models.getModels() == ()
    assert models.getModel("deepseek", "deepseek-v4-flash") is None

    models.setProvider(provider)
    assert models.getProvider("deepseek") is provider
    catalog = models.getModels("deepseek")
    assert len(catalog) == 1
    model = catalog[0]
    assert models.getModel("deepseek", "deepseek-v4-flash") is model
    assert type(model) is Model
    assert model.id == "deepseek-v4-flash"
    assert model.name == "DeepSeek V4 Flash"
    assert model.api == "openai-completions"
    assert model.provider == "deepseek"
    assert {name for name in dir(model) if not name.startswith("_")} == {
        "api",
        "id",
        "name",
        "provider",
    }
    with pytest.raises(TypeError, match="factory-produced"):
        Model(
            id="deepseek-v4-flash",
            name="DeepSeek V4 Flash",
            api="openai-completions",
            provider="deepseek",
            _token=object(),
        )
    assert not hasattr(provider, "baseUrl")
    assert not hasattr(provider, "headers")
    assert not hasattr(provider, "apiKey")
    assert not hasattr(model, "aliases")
    assert not hasattr(model, "capabilities")
    assert not hasattr(models, "refresh")
    assert importlib.util.find_spec("oh_my_llm.providers.deepseek") is not None
    with pytest.raises(ModuleNotFoundError):
        import_module("oh_my_llm.providers.openai")


def test_get_auth_rereads_the_environment_and_never_exposes_secret_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    secret = "super-secret-deepseek-key"
    models = createModels()
    deepseek = deepseekProvider()
    models.setProvider(deepseek)
    model = models.getModel("deepseek", "deepseek-v4-flash")
    assert model is not None

    pending = models.getAuth(model)
    assert inspect.isawaitable(pending)
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    configured = asyncio.run(pending)
    assert configured == AuthResult(source="DEEPSEEK_API_KEY")
    assert configured.source == "DEEPSEEK_API_KEY"
    assert secret not in str(configured)
    assert secret not in repr(configured)
    assert not hasattr(configured, "auth")
    assert not hasattr(configured, "apiKey")

    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert asyncio.run(models.getAuth(model)) is None
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    assert asyncio.run(models.getAuth(model)) is None
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    assert asyncio.run(models.getAuth(model)) == AuthResult(source="DEEPSEEK_API_KEY")

    faux = fauxProvider()
    models.setProvider(faux.provider)
    faux_model = faux.getModel()
    assert faux_model is not None
    faux_auth = asyncio.run(models.getAuth(faux_model))
    assert faux_auth == AuthResult(source=None)
    assert secret not in str(faux_auth)
    assert asyncio.run(models.getAuth(model)) == AuthResult(source="DEEPSEEK_API_KEY")


def _sse(*payloads: dict[str, Any] | str) -> bytes:
    chunks: list[bytes] = []
    for payload in payloads:
        if payload == "[DONE]":
            chunks.append(b"data: [DONE]\n\n")
        else:
            chunks.append(b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n")
    return b"".join(chunks)


class _HttpSpy:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status
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


_TEXT_SSE = _sse(
    {
        "id": "chatcmpl-1",
        "model": "deepseek-v4-flash",
        "choices": [{"index": 0, "delta": {"content": "Hel"}, "finish_reason": None}],
    },
    {
        "id": "chatcmpl-1",
        "model": "deepseek-v4-flash",
        "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": None}],
    },
    {
        "id": "chatcmpl-1",
        "model": "deepseek-v4-flash",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 2,
            "total_tokens": 14,
            "prompt_cache_hit_tokens": 4,
        },
    },
    "[DONE]",
)


def test_missing_auth_and_foreign_models_fail_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _HttpSpy(_TEXT_SSE)
    spy.install(monkeypatch)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    models = createModels()
    models.setProvider(deepseekProvider())
    model = models.getModel("deepseek", "deepseek-v4-flash")
    assert model is not None
    context = Context(messages=(UserMessage(content="Hello", timestamp=0),))

    dormant = models.streamSimple(model, context)
    assert spy.requests == []
    assert spy.client_kwargs == []

    with pytest.raises(ModelsError) as caught:
        asyncio.run(_collect(dormant))
    assert caught.value.code == "auth"
    assert spy.requests == []
    assert spy.client_kwargs == []

    faux = fauxProvider()
    foreign = faux.getModel()
    assert foreign is not None
    with pytest.raises(LookupError):
        asyncio.run(_collect(models.streamSimple(foreign, context)))
    assert spy.requests == []
    assert spy.client_kwargs == []


def test_fragmented_text_stream_sends_one_non_thinking_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _HttpSpy(_TEXT_SSE)
    spy.install(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    models = createModels()
    models.setProvider(deepseekProvider())
    model = models.getModel("deepseek", "deepseek-v4-flash")
    assert model is not None
    context = Context(messages=(UserMessage(content="Hello", timestamp=0),))

    events = asyncio.run(_collect(models.streamSimple(model, context)))
    assert len(spy.client_kwargs) == 1
    kwargs = spy.client_kwargs[0]
    assert kwargs["trust_env"] is False
    assert kwargs["timeout"] is None
    assert kwargs.get("follow_redirects") is False
    transport = kwargs["transport"]
    assert isinstance(transport, httpx.AsyncHTTPTransport)
    assert transport._pool._retries == 0
    assert len(spy.requests) == 1
    request = spy.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.deepseek.com/chat/completions"
    assert request.headers["Authorization"] == "Bearer test-deepseek-key"
    assert request.headers["Accept-Encoding"] == "identity"
    assert "application/json" in request.headers["Content-Type"]
    assert json.loads(request.content) == {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "Hello"}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "thinking": {"type": "disabled"},
    }

    assert [type(event) for event in events] == [
        AssistantMessageStartEvent,
        AssistantMessageTextStartEvent,
        AssistantMessageTextDeltaEvent,
        AssistantMessageTextDeltaEvent,
        AssistantMessageTextEndEvent,
        AssistantMessageDoneEvent,
    ]
    assert events[0].partial.content == ()
    assert events[0].partial.usage.totalTokens == 0
    assert events[2].delta == "Hel"
    assert events[3].delta == "lo"
    assert events[4].content == "Hello"
    done = events[-1]
    assert isinstance(done, AssistantMessageDoneEvent)
    assert done.reason == "stop"
    message = done.message
    assert message.content == (TextContent(text="Hello"),)
    assert message.api == "openai-completions"
    assert message.provider == "deepseek"
    assert message.model == "deepseek-v4-flash"
    assert message.responseId == "chatcmpl-1"
    assert message.responseModel == "deepseek-v4-flash"
    assert message.stopReason == "stop"
    assert message.errorMessage is None
    assert message.usage.input == 8
    assert message.usage.output == 2
    assert message.usage.cacheRead == 4
    assert message.usage.cacheWrite == 0
    assert message.usage.totalTokens == 14
    assert message.usage.cost.input == 1.12e-6
    assert message.usage.cost.output == 5.6e-7
    assert message.usage.cost.cacheRead == 1.12e-8
    assert message.usage.cost.cacheWrite == 0.0
    assert message.usage.cost.total == 1.6912e-6
    assert "test-deepseek-key" not in str(message)
    assert "test-deepseek-key" not in "".join(str(event) for event in events)

    terminal = asyncio.run(models.completeSimple(model, context))
    assert terminal == message
    assert len(spy.requests) == 2


async def _collect(stream: Any) -> list[Any]:
    return [event async for event in stream]
