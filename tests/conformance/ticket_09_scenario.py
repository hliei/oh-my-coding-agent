from __future__ import annotations

import asyncio
from importlib import import_module
import inspect
import json
import os
from typing import Any

import httpx

from oh_my_llm import (
    AssistantMessageDoneEvent,
    Context,
    TextContent,
    UserMessage,
    createModels,
    fauxProvider,
)
from oh_my_llm.providers.deepseek import deepseekProvider


_TEXT_SSE = (
    b'data: {"id":"chatcmpl-1","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"Hel"},"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-1","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-1","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":12,"completion_tokens":2,"total_tokens":14,"prompt_cache_hit_tokens":4}}\n\n'
    b"data: [DONE]\n\n"
)


class _HttpSpy:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.requests: list[httpx.Request] = []
        self.client_kwargs: list[dict[str, Any]] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            content=self.body,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )


def _install_spy(spy: _HttpSpy) -> type[httpx.AsyncClient]:
    original = httpx.AsyncClient

    class FakeClient(original):  # type: ignore[valid-type,misc]
        def __init__(self, **kwargs: Any) -> None:
            spy.client_kwargs.append(dict(kwargs))
            kwargs = dict(kwargs)
            kwargs["transport"] = httpx.MockTransport(spy._handle)
            super().__init__(**kwargs)

    setattr(httpx, "AsyncClient", FakeClient)
    return original


async def _collect(stream: Any) -> list[Any]:
    return [event async for event in stream]


def _factory_observation() -> dict[str, Any]:
    assert tuple(inspect.signature(deepseekProvider).parameters) == ()
    provider = deepseekProvider()
    models = createModels()
    assert models.getProviders() == ()
    models.setProvider(provider)
    model = models.getModel("deepseek", "deepseek-v4-flash")
    assert model is not None
    try:
        import_module("oh_my_llm.providers.openai")
        openai_missing = False
    except ModuleNotFoundError:
        openai_missing = True
    return {
        "A": "admitted",
        "L": [],
        "T": {
            "providerId": provider.id,
            "providerName": provider.name,
            "modelId": model.id,
            "modelName": model.name,
            "api": model.api,
            "provider": model.provider,
            "childPath": "oh_my_llm.providers.deepseek",
            "foreignProviderMissing": openai_missing,
        },
        "E": [],
        "C": "not_applicable",
    }


def _auth_observation() -> dict[str, Any]:
    secret = "super-secret-deepseek-key"
    models = createModels()
    models.setProvider(deepseekProvider())
    model = models.getModel("deepseek", "deepseek-v4-flash")
    assert model is not None
    os.environ.pop("DEEPSEEK_API_KEY", None)
    unconfigured = asyncio.run(models.getAuth(model))
    os.environ["DEEPSEEK_API_KEY"] = secret
    configured = asyncio.run(models.getAuth(model))
    faux = fauxProvider()
    models.setProvider(faux.provider)
    faux_model = faux.getModel()
    assert faux_model is not None
    faux_auth = asyncio.run(models.getAuth(faux_model))
    leaked = secret in f"{configured!s}{configured!r}{faux_auth!s}"
    return {
        "A": "admitted",
        "L": [],
        "T": {
            "configured": {"source": None if configured is None else configured.source},
            "unconfigured": unconfigured,
            "faux": {"source": None if faux_auth is None else faux_auth.source},
            "secretLeaked": leaked,
        },
        "E": [],
        "C": "not_applicable",
    }


def _request_and_stream_observation() -> tuple[dict[str, Any], dict[str, Any]]:
    spy = _HttpSpy(_TEXT_SSE)
    original = _install_spy(spy)
    os.environ["DEEPSEEK_API_KEY"] = "test-deepseek-key"
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
    try:
        models = createModels()
        models.setProvider(deepseekProvider())
        model = models.getModel("deepseek", "deepseek-v4-flash")
        assert model is not None
        context = Context(messages=(UserMessage(content="Hello", timestamp=0),))
        events = asyncio.run(_collect(models.streamSimple(model, context)))
        kwargs = spy.client_kwargs[0]
        request = spy.requests[0]
        body = json.loads(request.content)
        done = events[-1]
        assert isinstance(done, AssistantMessageDoneEvent)
        message = done.message
        request_observation = {
            "A": "admitted",
            "L": [],
            "T": {
                "method": request.method,
                "url": str(request.url),
                "model": body["model"],
                "stream": body["stream"],
                "thinking": body["thinking"],
                "streamOptions": body["stream_options"],
            },
            "E": {
                "requestCount": len(spy.requests),
                "trustEnv": kwargs["trust_env"],
                "timeout": kwargs["timeout"],
                "followRedirects": kwargs.get("follow_redirects"),
                "retries": kwargs["transport"]._pool._retries,
            },
            "C": "not_applicable",
        }
        stream_observation = {
            "A": "admitted",
            "L": [event.type for event in events],
            "T": {
                "text": message.content[0].text
                if isinstance(message.content[0], TextContent)
                else "",
                "stopReason": message.stopReason,
                "input": message.usage.input,
                "output": message.usage.output,
                "cacheRead": message.usage.cacheRead,
                "totalTokens": message.usage.totalTokens,
                "costTotal": message.usage.cost.total,
            },
            "E": {"requestCount": len(spy.requests), "secretLeaked": "test-deepseek-key" in str(message)},
            "C": "not_applicable",
        }
        return request_observation, stream_observation
    finally:
        setattr(httpx, "AsyncClient", original)


def main() -> None:
    previous_key = os.environ.get("DEEPSEEK_API_KEY")
    previous_proxy = os.environ.get("HTTPS_PROXY")
    try:
        request_observation, stream_observation = _request_and_stream_observation()
        actual = {
            "reference.deepseek-factory-catalog": _factory_observation(),
            "reference.deepseek-auth-observation": _auth_observation(),
            "reference.deepseek-request-bytes": request_observation,
            "reference.deepseek-text-stream-terminal": stream_observation,
        }
        print(json.dumps(actual, sort_keys=True))
    finally:
        if previous_key is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = previous_key
        if previous_proxy is None:
            os.environ.pop("HTTPS_PROXY", None)
        else:
            os.environ["HTTPS_PROXY"] = previous_proxy


if __name__ == "__main__":
    main()
