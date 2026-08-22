from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import replace
import inspect
from typing import Literal, TypeAlias, cast, final

from ._models import Model, Provider, _create_model, _create_provider
from ._values import (
    AssistantMessage,
    AssistantMessageDoneEvent,
    AssistantMessageErrorEvent,
    AssistantMessageEvent,
    AssistantMessageStartEvent,
    AssistantMessageTextDeltaEvent,
    AssistantMessageTextEndEvent,
    AssistantMessageTextStartEvent,
    AssistantMessageToolCallEndEvent,
    AssistantMessageToolCallStartEvent,
    Context,
    JSONValue,
    StopReason,
    TextContent,
    ToolCall,
    Usage,
    UsageCost,
    _AssistantMessageEventValidator,
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

        response = replace(
            response,
            api=model.api,
            provider=model.provider,
            model=model.id,
        )
        partial = replace(response, content=())
        validator = _AssistantMessageEventValidator()
        start = AssistantMessageStartEvent(partial=partial)
        validator.accept(start)
        yield start
        for index, block in enumerate(response.content):
            if isinstance(block, TextContent):
                text_partial = replace(
                    partial,
                    content=(*partial.content, TextContent(text="")),
                )
                event: AssistantMessageEvent = AssistantMessageTextStartEvent(
                    contentIndex=index, partial=text_partial
                )
                validator.accept(event)
                yield event
                text_partial = replace(partial, content=(*partial.content, block))
                event = AssistantMessageTextDeltaEvent(
                    contentIndex=index,
                    delta=block.text,
                    partial=text_partial,
                )
                validator.accept(event)
                yield event
                event = AssistantMessageTextEndEvent(
                    contentIndex=index,
                    content=block.text,
                    partial=text_partial,
                )
                validator.accept(event)
                yield event
                partial = text_partial
            else:
                tool_partial = replace(partial, content=(*partial.content, block))
                event = AssistantMessageToolCallStartEvent(
                    contentIndex=index, partial=tool_partial
                )
                validator.accept(event)
                yield event
                event = AssistantMessageToolCallEndEvent(
                    contentIndex=index,
                    toolCall=block,
                    partial=tool_partial,
                )
                validator.accept(event)
                yield event
                partial = tool_partial
        if response.stopReason in ("stop", "length", "toolUse"):
            terminal: AssistantMessageEvent = AssistantMessageDoneEvent(
                reason=cast(Literal["stop", "length", "toolUse"], response.stopReason),
                message=response,
            )
        else:
            terminal = AssistantMessageErrorEvent(
                reason=cast(Literal["error", "aborted"], response.stopReason),
                error=response,
            )
        validator.accept(terminal)
        yield terminal


def fauxProvider() -> FauxProviderHandle:
    return FauxProviderHandle()


def fauxText(text: str) -> TextContent:
    return TextContent(text=text)


def fauxToolCall(
    *,
    id: str,
    name: str,
    arguments: dict[str, JSONValue],
    thoughtSignature: str | None = None,
) -> ToolCall:
    return ToolCall(
        id=id,
        name=name,
        arguments=arguments,
        thoughtSignature=thoughtSignature,
    )


def fauxAssistantMessage(
    content: str | TextContent | ToolCall | Iterable[TextContent | ToolCall],
    *,
    stopReason: StopReason = "stop",
    errorMessage: str | None = None,
) -> AssistantMessage:
    blocks: tuple[TextContent | ToolCall, ...]
    if isinstance(content, str):
        blocks = (fauxText(content),)
    elif isinstance(content, (TextContent, ToolCall)):
        blocks = (content,)
    else:
        blocks = tuple(content)
    return AssistantMessage(
        content=blocks,
        api="faux",
        provider="faux",
        model="faux-1",
        usage=Usage(
            input=0,
            output=0,
            cacheRead=0,
            cacheWrite=0,
            totalTokens=0,
            cost=UsageCost(
                input=0.0,
                output=0.0,
                cacheRead=0.0,
                cacheWrite=0.0,
                total=0.0,
            ),
        ),
        stopReason=stopReason,
        timestamp=0,
        errorMessage=errorMessage,
    )
