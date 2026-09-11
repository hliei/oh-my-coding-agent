from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal, NotRequired, Protocol, Required, TypeVar, TypedDict, overload


AttributeValue = (
    str
    | bool
    | int
    | float
    | list[str]
    | list[bool]
    | list[int | float]
)
SpanAttributes = dict[str, AttributeValue | None]


class SpanOptions(TypedDict, total=False):
    name: Required[str]
    attributes: NotRequired[SpanAttributes]


class _OkSpanStatus(TypedDict):
    status: Required[Literal["ok"]]


class _SpanErrorDetails(TypedDict):
    name: Required[str]
    message: Required[str]


class _ErrorSpanStatus(TypedDict, total=False):
    status: Required[Literal["error"]]
    error: NotRequired[_SpanErrorDetails]


SpanStatus = _OkSpanStatus | _ErrorSpanStatus


class RecordedTelemetryEvent(TypedDict):
    name: str
    attributes: dict[str, AttributeValue]


class RecordedTelemetrySpan(TypedDict, total=False):
    id: Required[int]
    parentId: Required[int | None]
    name: Required[str]
    attributes: Required[dict[str, AttributeValue]]
    events: Required[list[RecordedTelemetryEvent]]
    status: Required[SpanStatus]
    settled: Required[bool]
    endSequence: NotRequired[int]


TelemetryAttributeType = Literal[
    "string",
    "number",
    "boolean",
    "string[]",
    "number[]",
    "boolean[]",
]


class TelemetryAttributeMetadata(TypedDict, total=False):
    description: Required[str]
    sensitive: NotRequired[bool]
    cardinality: NotRequired[Literal["low", "high"]]


class _StringAttributeDefinition(TelemetryAttributeMetadata, total=False):
    type: Required[Literal["string"]]
    values: NotRequired[list[str]]
    examples: NotRequired[list[str]]


class _NumberAttributeDefinition(TelemetryAttributeMetadata, total=False):
    type: Required[Literal["number"]]
    values: NotRequired[list[int | float]]
    examples: NotRequired[list[int | float]]


class _BooleanAttributeDefinition(TelemetryAttributeMetadata, total=False):
    type: Required[Literal["boolean"]]
    values: NotRequired[list[bool]]
    examples: NotRequired[list[bool]]


class _StringListAttributeDefinition(TelemetryAttributeMetadata, total=False):
    type: Required[Literal["string[]"]]
    elementValues: NotRequired[list[str]]
    examples: NotRequired[list[list[str]]]


class _NumberListAttributeDefinition(TelemetryAttributeMetadata, total=False):
    type: Required[Literal["number[]"]]
    elementValues: NotRequired[list[int | float]]
    examples: NotRequired[list[list[int | float]]]


class _BooleanListAttributeDefinition(TelemetryAttributeMetadata, total=False):
    type: Required[Literal["boolean[]"]]
    elementValues: NotRequired[list[bool]]
    examples: NotRequired[list[list[bool]]]


TelemetryAttributeDefinition = (
    _StringAttributeDefinition
    | _NumberAttributeDefinition
    | _BooleanAttributeDefinition
    | _StringListAttributeDefinition
    | _NumberListAttributeDefinition
    | _BooleanListAttributeDefinition
)


class _RequiredStringAttributeDefinition(_StringAttributeDefinition, total=False):
    required: Required[bool]


class _RequiredNumberAttributeDefinition(_NumberAttributeDefinition, total=False):
    required: Required[bool]


class _RequiredBooleanAttributeDefinition(_BooleanAttributeDefinition, total=False):
    required: Required[bool]


class _RequiredStringListAttributeDefinition(
    _StringListAttributeDefinition, total=False
):
    required: Required[bool]


class _RequiredNumberListAttributeDefinition(
    _NumberListAttributeDefinition, total=False
):
    required: Required[bool]


class _RequiredBooleanListAttributeDefinition(
    _BooleanListAttributeDefinition, total=False
):
    required: Required[bool]


_RequiredAttributeDefinition = (
    _RequiredStringAttributeDefinition
    | _RequiredNumberAttributeDefinition
    | _RequiredBooleanAttributeDefinition
    | _RequiredStringListAttributeDefinition
    | _RequiredNumberListAttributeDefinition
    | _RequiredBooleanListAttributeDefinition
)
TelemetryStartAttributeDefinition = _RequiredAttributeDefinition
TelemetryEventAttributeDefinition = _RequiredAttributeDefinition


class TelemetryEventDefinition(TypedDict):
    description: str
    attributes: dict[str, TelemetryEventAttributeDefinition]


class _AnyParentDefinition(TypedDict):
    kind: Literal["any"]


class _RootOrExternalParentDefinition(TypedDict):
    kind: Literal["root_or_external"]


class _SpansParentDefinition(TypedDict):
    kind: Literal["spans"]
    spans: list[str]


TelemetryParentDefinition = (
    _AnyParentDefinition | _RootOrExternalParentDefinition | _SpansParentDefinition
)


class _SpanStatusMetadata(TypedDict):
    default: Literal["ok"]
    errorWhen: str


class TelemetrySpanDefinition(TypedDict, total=False):
    description: Required[str]
    parents: Required[TelemetryParentDefinition]
    startAttributes: Required[dict[str, TelemetryStartAttributeDefinition]]
    endAttributes: Required[dict[str, TelemetryAttributeDefinition]]
    events: NotRequired[dict[str, TelemetryEventDefinition]]
    status: Required[_SpanStatusMetadata]


class TelemetrySchemaDefinition(TypedDict):
    version: int
    spans: dict[str, TelemetrySpanDefinition]


_T = TypeVar("_T")
_SchemaT = TypeVar("_SchemaT", bound=TelemetrySchemaDefinition)


def defineTelemetrySchema(schema: _SchemaT) -> _SchemaT:
    return schema


class TelemetryContext(Protocol):
    @overload
    def startSpan(
        self,
        options: SpanOptions,
        callback: Callable[[TelemetrySpan], Awaitable[_T]],
    ) -> Awaitable[_T]: ...

    @overload
    def startSpan(
        self,
        options: SpanOptions,
        callback: Callable[[TelemetrySpan], _T],
    ) -> Awaitable[_T]: ...


class TelemetrySpan(TelemetryContext, Protocol):
    def addEvent(
        self, name: str, attributes: SpanAttributes | None = None
    ) -> None: ...

    def setAttributes(self, attributes: SpanAttributes) -> None: ...

    def setStatus(self, status: SpanStatus) -> None: ...
