from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias, final


StopReason: TypeAlias = Literal["stop", "length", "toolUse", "error", "aborted"]


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class UsageCost:
    input: float = 0.0
    output: float = 0.0
    cacheRead: float = 0.0
    cacheWrite: float = 0.0
    total: float = 0.0


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class Usage:
    input: int = 0
    output: int = 0
    cacheRead: int = 0
    cacheWrite: int = 0
    totalTokens: int = 0
    cost: UsageCost = field(default_factory=UsageCost)


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class TextContent:
    text: str
    type: Literal["text"] = field(init=False, default="text")
    textSignature: str | None = None


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class UserMessage:
    content: str | tuple[TextContent, ...]
    timestamp: int
    role: Literal["user"] = field(init=False, default="user")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class AssistantMessage:
    content: tuple[TextContent, ...]
    api: str
    provider: str
    model: str
    usage: Usage
    stopReason: StopReason
    timestamp: int
    responseModel: str | None = None
    responseId: str | None = None
    errorMessage: str | None = None
    role: Literal["assistant"] = field(init=False, default="assistant")


Message: TypeAlias = UserMessage | AssistantMessage


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class Context:
    messages: tuple[Message, ...]
    systemPrompt: str | None = None
    tools: tuple[object, ...] | None = None


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class AssistantMessageStartEvent:
    partial: AssistantMessage
    type: Literal["start"] = field(init=False, default="start")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class AssistantMessageTextStartEvent:
    contentIndex: int
    partial: AssistantMessage
    type: Literal["text_start"] = field(init=False, default="text_start")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class AssistantMessageTextDeltaEvent:
    contentIndex: int
    delta: str
    partial: AssistantMessage
    type: Literal["text_delta"] = field(init=False, default="text_delta")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class AssistantMessageTextEndEvent:
    contentIndex: int
    content: str
    partial: AssistantMessage
    type: Literal["text_end"] = field(init=False, default="text_end")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class AssistantMessageDoneEvent:
    reason: StopReason
    message: AssistantMessage
    type: Literal["done"] = field(init=False, default="done")


AssistantMessageEvent: TypeAlias = (
    AssistantMessageStartEvent
    | AssistantMessageTextStartEvent
    | AssistantMessageTextDeltaEvent
    | AssistantMessageTextEndEvent
    | AssistantMessageDoneEvent
)
