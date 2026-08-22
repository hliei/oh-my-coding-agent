from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import replace
import inspect
from typing import TypeAlias, final

from ._models import Model, Provider, _create_model, _create_provider
from ._values import (
    AssistantMessage,
    AssistantMessageDoneEvent,
    AssistantMessageEvent,
    AssistantMessageStartEvent,
    AssistantMessageTextDeltaEvent,
    AssistantMessageTextEndEvent,
    AssistantMessageTextStartEvent,
    Context,
    StopReason,
    TextContent,
    Usage,
)


FauxResponseFactory: TypeAlias = Callable[
    [Context, object | None, "_FauxState", Model],
    AssistantMessage | Awaitable[AssistantMessage],
]
FauxResponseStep: TypeAlias = AssistantMessage | FauxResponseFactory


@final
class _FauxState:
    __slots__ = ("_callCount",)

    def __init__(self) -> None:
        self._callCount = 0

    @property
    def callCount(self) -> int:
        return self._callCount


@final
class FauxProviderHandle:
    __slots__ = ("_models", "_pending", "_provider", "_state")

    def __init__(self) -> None:
        self._state = _FauxState()
        self._pending: list[FauxResponseStep] = []
        model = _create_model(id="faux-1", name="Faux Model", api="faux", provider="faux")
        self._models = (model,)
        self._provider = _create_provider(
            id="faux",
            name="faux",
            models=self._models,
            streamSimple=self._streamSimple,
        )

    @property
    def provider(self) -> Provider:
        return self._provider

    @property
    def models(self) -> tuple[Model, ...]:
        return self._models

    @property
    def state(self) -> _FauxState:
        return self._state

    def getModel(self, modelId: str | None = None) -> Model | None:
        if modelId is None:
            return self._models[0]
        return next((model for model in self._models if model.id == modelId), None)

    def setResponses(self, responses: Iterable[FauxResponseStep]) -> None:
        self._pending = list(responses)

    def appendResponses(self, responses: Iterable[FauxResponseStep]) -> None:
        self._pending.extend(responses)

    def getPendingResponseCount(self) -> int:
        return len(self._pending)

    async def _streamSimple(
        self,
        model: Model,
        context: Context,
    ) -> AsyncIterator[AssistantMessageEvent]:
        self._state._callCount += 1
        if not self._pending:
            raise RuntimeError("No more faux responses queued")
        step = self._pending.pop(0)
        response = step(context, None, self._state, model) if callable(step) else step
        if inspect.isawaitable(response):
            response = await response
        if not isinstance(response, AssistantMessage):
            raise TypeError("Faux response must be an AssistantMessage")

        partial = replace(
            response,
            content=(),
            api=model.api,
            provider=model.provider,
            model=model.id,
        )
        yield AssistantMessageStartEvent(partial=partial)
        for index, block in enumerate(response.content):
            text_partial = replace(
                partial,
                content=(*partial.content, TextContent(text="")),
            )
            yield AssistantMessageTextStartEvent(contentIndex=index, partial=text_partial)
            text_partial = replace(partial, content=(*partial.content, block))
            yield AssistantMessageTextDeltaEvent(
                contentIndex=index,
                delta=block.text,
                partial=text_partial,
            )
            yield AssistantMessageTextEndEvent(
                contentIndex=index,
                content=block.text,
                partial=text_partial,
            )
            partial = text_partial
        yield AssistantMessageDoneEvent(reason=response.stopReason, message=response)


def fauxProvider() -> FauxProviderHandle:
    return FauxProviderHandle()


def fauxText(text: str) -> TextContent:
    return TextContent(text=text)


def fauxAssistantMessage(
    content: str | TextContent | Iterable[TextContent],
    *,
    stopReason: StopReason = "stop",
    errorMessage: str | None = None,
) -> AssistantMessage:
    blocks: tuple[TextContent, ...]
    if isinstance(content, str):
        blocks = (fauxText(content),)
    elif isinstance(content, TextContent):
        blocks = (content,)
    else:
        blocks = tuple(content)
    return AssistantMessage(
        content=blocks,
        api="faux",
        provider="faux",
        model="faux-1",
        usage=Usage(),
        stopReason=stopReason,
        timestamp=0,
        errorMessage=errorMessage,
    )
