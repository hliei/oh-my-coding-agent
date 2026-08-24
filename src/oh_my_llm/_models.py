from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import os
from typing import final

from ._errors import ModelsError
from ._values import (
    AuthResult,
    AssistantMessage,
    AssistantMessageDoneEvent,
    AssistantMessageErrorEvent,
    AssistantMessageEvent,
    Context,
    SimpleStreamOptions,
    StreamOptions,
)
from ._streams import (
    AbortSignal,
    EventStream,
    _active_abort_signal,
    _bind_abort_signal,
    _create_event_stream,
)


StreamSimpleFn = Callable[
    ["Model", Context, SimpleStreamOptions | None],
    AsyncIterator[AssistantMessageEvent],
]
_MODEL_TOKEN = object()
_PROVIDER_TOKEN = object()
_MODELS_TOKEN = object()


@final
class Model:
    __slots__ = ("_api", "_id", "_name", "_provider")

    def __init__(
        self,
        *,
        id: str,
        name: str,
        api: str,
        provider: str,
        _token: object,
    ) -> None:
        if _token is not _MODEL_TOKEN:
            raise TypeError("Model values are factory-produced")
        self._id = id
        self._name = name
        self._api = api
        self._provider = provider

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._name

    @property
    def api(self) -> str:
        return self._api

    @property
    def provider(self) -> str:
        return self._provider


@final
class Provider:
    __slots__ = ("_id", "_models", "_name", "_streamSimple")

    def __init__(
        self,
        *,
        id: str,
        name: str,
        models: tuple[Model, ...],
        streamSimple: StreamSimpleFn,
        _token: object,
    ) -> None:
        if _token is not _PROVIDER_TOKEN:
            raise TypeError("Provider handles are factory-produced")
        self._id = id
        self._name = name
        self._models = models
        self._streamSimple = streamSimple

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._name


class Models:
    __slots__ = ("_providers",)

    def __init__(self, *, _token: object) -> None:
        if _token is not _MODELS_TOKEN:
            raise TypeError("Models collections are created by createModels()")
        self._providers: dict[str, Provider] = {}

    def getProviders(self) -> tuple[Provider, ...]:
        return tuple(self._providers.values())

    def getProvider(self, id: str) -> Provider | None:
        return self._providers.get(id)

    def getModels(self, provider: str | None = None) -> tuple[Model, ...]:
        if provider is not None:
            selected = self._providers.get(provider)
            return () if selected is None else selected._models
        return tuple(model for item in self._providers.values() for model in item._models)

    def getModel(self, provider: str, id: str) -> Model | None:
        return next((model for model in self.getModels(provider) if model.id == id), None)

    def _require_provider(self, model: Model) -> Provider:
        provider = self._providers.get(model.provider)
        if provider is None or all(candidate is not model for candidate in provider._models):
            raise LookupError(f"Unknown model: {model.provider}/{model.id}")
        return provider

    async def getAuth(self, model: Model) -> AuthResult | None:
        self._require_provider(model)
        if model.provider == "deepseek":
            key = os.environ.get("DEEPSEEK_API_KEY")
            if key:
                return AuthResult(source="DEEPSEEK_API_KEY")
            return None
        return AuthResult(source=None)

    def streamSimple(
        self,
        model: Model,
        context: Context,
        options: SimpleStreamOptions | None = None,
    ) -> AsyncIterator[AssistantMessageEvent]:
        provider = self._require_provider(model)

        async def owned_stream() -> AsyncIterator[AssistantMessageEvent]:
            signal = _active_abort_signal()
            if signal is not None and signal.aborted:
                raise asyncio.CancelledError
            async for event in provider._streamSimple(model, context, options):
                if signal is not None and signal.aborted:
                    if not (
                        isinstance(event, AssistantMessageErrorEvent)
                        and event.reason == "aborted"
                    ):
                        raise asyncio.CancelledError
                yield event
                if (
                    isinstance(event, AssistantMessageErrorEvent)
                    and event.reason == "aborted"
                ):
                    return

        return owned_stream()

    def stream(
        self,
        model: Model,
        context: Context,
        options: StreamOptions | None = None,
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]:
        async def producer(
            emit: Callable[[AssistantMessageEvent], Awaitable[None]],
            signal: AbortSignal,
        ) -> AssistantMessage:
            with _bind_abort_signal(signal):
                return await self._consume_simple(model, context, options, emit)

        return _create_event_stream(producer)

    async def complete(
        self,
        model: Model,
        context: Context,
        options: StreamOptions | None = None,
    ) -> AssistantMessage:
        return await self.stream(model, context, options).result()

    async def completeSimple(
        self,
        model: Model,
        context: Context,
        options: SimpleStreamOptions | None = None,
    ) -> AssistantMessage:
        return await self._consume_simple(model, context, options, None)

    async def _consume_simple(
        self,
        model: Model,
        context: Context,
        options: StreamOptions | None,
        emit: Callable[[AssistantMessageEvent], Awaitable[None]] | None,
    ) -> AssistantMessage:
        terminal: AssistantMessage | None = None
        async for event in self.streamSimple(model, context, options):
            if emit is not None:
                await emit(event)
            if isinstance(event, AssistantMessageDoneEvent):
                terminal = event.message
            elif isinstance(event, AssistantMessageErrorEvent):
                terminal = event.error
        if terminal is None:
            raise ModelsError("stream", "Model stream ended without a terminal value")
        return terminal


@final
class MutableModels(Models):
    def setProvider(self, provider: Provider) -> None:
        if not isinstance(provider, Provider):
            raise TypeError("provider must be a Provider")
        self._providers[provider.id] = provider


def createModels() -> MutableModels:
    return MutableModels(_token=_MODELS_TOKEN)


def _create_model(*, id: str, name: str, api: str, provider: str) -> Model:
    return Model(id=id, name=name, api=api, provider=provider, _token=_MODEL_TOKEN)


def _create_provider(
    *,
    id: str,
    name: str,
    models: tuple[Model, ...],
    streamSimple: StreamSimpleFn,
) -> Provider:
    return Provider(
        id=id,
        name=name,
        models=models,
        streamSimple=streamSimple,
        _token=_PROVIDER_TOKEN,
    )
