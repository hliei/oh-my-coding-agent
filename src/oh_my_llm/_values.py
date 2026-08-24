from __future__ import annotations

from collections.abc import Mapping
from dataclasses import MISSING, dataclass, field, fields
import json
import math
from types import MappingProxyType
from typing import Any, Literal, NoReturn, TypeAlias, cast, final


_SAFE_INTEGER = 2**53 - 1

StopReason: TypeAlias = Literal["stop", "length", "toolUse", "error", "aborted"]
_DoneReason: TypeAlias = Literal["stop", "length", "toolUse"]
_ErrorReason: TypeAlias = Literal["error", "aborted"]
JSONValue: TypeAlias = (
    None
    | bool
    | int
    | float
    | str
    | tuple["JSONValue", ...]
    | Mapping[str, "JSONValue"]
)


class _PublicValueMeta(type):
    def __call__(cls, *args: object, **kwargs: object) -> object:
        type_name = cls.__name__
        if args:
            raise TypeError(f"{type_name}.<constructor>: positional arguments are not admitted")

        record_fields = fields(cast(Any, cls))
        init_fields = {item.name: item for item in record_fields if item.init}
        for name in kwargs:
            if name not in init_fields:
                raise TypeError(f"{type_name}.{name}: unknown field")
        for item in record_fields:
            if (
                item.init
                and item.name not in kwargs
                and item.default is MISSING
                and item.default_factory is MISSING
            ):
                raise TypeError(f"{type_name}.{item.name}: missing required field")
        return super().__call__(**kwargs)


class _PublicValueRecord(metaclass=_PublicValueMeta):
    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        if type(self) is not type(other):
            return False
        from ._canonical import encodeCanonical

        return encodeCanonical(self) == encodeCanonical(other)

    def __hash__(self) -> int:
        from ._canonical import encodeCanonical

        return hash((type(self), encodeCanonical(self)))


def _fail_type(type_name: str, path: str, explanation: str) -> NoReturn:
    raise TypeError(f"{type_name}.{path}: {explanation}")


def _fail_value(type_name: str, path: str, explanation: str) -> NoReturn:
    raise ValueError(f"{type_name}.{path}: {explanation}")


def _check_unicode(value: str, type_name: str, path: str) -> str:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        _fail_value(type_name, path, "must contain only Unicode scalar values")
    return value


def _string(value: object, type_name: str, path: str) -> str:
    if type(value) is not str:
        _fail_type(type_name, path, "must be a string")
    return _check_unicode(value, type_name, path)


def _optional_string(value: object, type_name: str, path: str) -> str | None:
    if value is None:
        return None
    return _string(value, type_name, path)


def _safe_integer(value: object, type_name: str, path: str, *, nonnegative: bool) -> int:
    if type(value) is not int:
        _fail_type(type_name, path, "must be an integer")
    integer = value
    if not -_SAFE_INTEGER <= integer <= _SAFE_INTEGER:
        _fail_value(type_name, path, "must be an IEEE-754 safe integer")
    if nonnegative and integer < 0:
        _fail_value(type_name, path, "must be nonnegative")
    return integer


def _finite_float(value: object, type_name: str, path: str, *, nonnegative: bool) -> float:
    if type(value) is not float:
        _fail_type(type_name, path, "must be a float")
    number = value
    if not math.isfinite(number):
        _fail_value(type_name, path, "must be finite")
    if nonnegative and number < 0.0:
        _fail_value(type_name, path, "must be nonnegative")
    return number


def _bool(value: object, type_name: str, path: str) -> bool:
    if type(value) is not bool:
        _fail_type(type_name, path, "must be a bool")
    return value


def _sequence(
    value: object,
    allowed: tuple[type[object], ...],
    type_name: str,
    path: str,
) -> tuple[Any, ...]:
    if type(value) not in (list, tuple):
        _fail_type(type_name, path, "must be a list or tuple")
    result: tuple[Any, ...] = tuple(cast(list[Any] | tuple[Any, ...], value))
    for index, item in enumerate(result):
        if type(item) not in allowed:
            _fail_type(type_name, f"{path}[{index}]", "has an invalid record carrier")
    return result


def _json_path(path: str, key: str) -> str:
    return f"{path}[{json.dumps(key, ensure_ascii=False)}]"


@dataclass(slots=True)
class _JSONTask:
    operation: str
    current: Any
    path: str
    target: Any
    target_key: Any


def _snapshot_json(value: object, type_name: str, path: str) -> JSONValue:
    root: list[Any] = [None]
    active: set[int] = set()
    stack = [_JSONTask("visit", value, path, root, 0)]

    while stack:
        task = stack.pop()
        operation = task.operation
        current = task.current
        current_path = task.path
        target = task.target
        target_key = task.target_key
        if operation == "finish_tuple":
            active.remove(target_key)
            parent, parent_key = target
            parent[parent_key] = tuple(current)
            continue
        if operation == "finish_mapping":
            active.remove(target_key)
            parent, parent_key = target
            parent[parent_key] = MappingProxyType(current)
            continue
        if operation == "visit_mapping_item":
            key, item = current
            if type(key) is not str:
                _fail_type(type_name, current_path, "mapping keys must be strings")
            _check_unicode(key, type_name, _json_path(current_path, key))
            stack.append(
                _JSONTask(
                    "visit",
                    item,
                    _json_path(current_path, key),
                    target,
                    key,
                )
            )
            continue

        if current is None or type(current) is bool:
            target[target_key] = current
        elif type(current) is int:
            target[target_key] = _safe_integer(
                current, type_name, current_path, nonnegative=False
            )
        elif type(current) is float:
            target[target_key] = _finite_float(
                current, type_name, current_path, nonnegative=False
            )
        elif type(current) is str:
            target[target_key] = _check_unicode(current, type_name, current_path)
        elif type(current) in (list, tuple):
            identity = id(current)
            if identity in active:
                _fail_value(type_name, current_path, "must be acyclic")
            active.add(identity)
            source = list(current)
            output: list[Any] = [None] * len(source)
            stack.append(
                _JSONTask(
                    "finish_tuple",
                    output,
                    current_path,
                    (target, target_key),
                    identity,
                )
            )
            for index in range(len(source) - 1, -1, -1):
                stack.append(
                    _JSONTask(
                        "visit",
                        source[index],
                        f"{current_path}[{index}]",
                        output,
                        index,
                    )
                )
        elif isinstance(current, Mapping):
            identity = id(current)
            if identity in active:
                _fail_value(type_name, current_path, "must be acyclic")
            active.add(identity)
            items = list(current.items())
            output_mapping: dict[str, Any] = {}
            stack.append(
                _JSONTask(
                    "finish_mapping",
                    output_mapping,
                    current_path,
                    (target, target_key),
                    identity,
                )
            )
            for key, item in reversed(items):
                stack.append(
                    _JSONTask(
                        "visit_mapping_item",
                        (key, item),
                        current_path,
                        output_mapping,
                        key,
                    )
                )
        else:
            _fail_type(type_name, current_path, "has an invalid JSON carrier")

    return cast(JSONValue, root[0])


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class UsageCost(_PublicValueRecord):
    input: float
    output: float
    cacheRead: float
    cacheWrite: float
    total: float

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        for item in fields(self):
            object.__setattr__(
                self,
                item.name,
                _finite_float(
                    getattr(self, item.name), type_name, item.name, nonnegative=True
                ),
            )


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class Usage(_PublicValueRecord):
    input: int
    output: int
    cacheRead: int
    cacheWrite: int
    totalTokens: int
    cost: UsageCost
    cacheWrite1h: int | None = None

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        for name in ("input", "output", "cacheRead", "cacheWrite", "totalTokens"):
            object.__setattr__(
                self,
                name,
                _safe_integer(getattr(self, name), type_name, name, nonnegative=True),
            )
        if type(self.cost) is not UsageCost:
            _fail_type(type_name, "cost", "must be a UsageCost")
        if self.cacheWrite1h is not None:
            cache_write_1h = _safe_integer(
                self.cacheWrite1h, type_name, "cacheWrite1h", nonnegative=True
            )
            if cache_write_1h > self.cacheWrite:
                _fail_value(type_name, "cacheWrite1h", "must not exceed cacheWrite")


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class AuthResult(_PublicValueRecord):
    source: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source",
            _optional_string(self.source, type(self).__name__, "source"),
        )


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class TextContent(_PublicValueRecord):
    text: str
    type: Literal["text"] = field(init=False, default="text")
    textSignature: str | None = None

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        object.__setattr__(self, "text", _string(self.text, type_name, "text"))
        object.__setattr__(
            self,
            "textSignature",
            _optional_string(self.textSignature, type_name, "textSignature"),
        )


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class ToolCall(_PublicValueRecord):
    id: str
    name: str
    arguments: Mapping[str, JSONValue]
    type: Literal["toolCall"] = field(init=False, default="toolCall")
    thoughtSignature: str | None = None

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        object.__setattr__(self, "id", _string(self.id, type_name, "id"))
        object.__setattr__(self, "name", _string(self.name, type_name, "name"))
        if not isinstance(self.arguments, Mapping):
            _fail_type(type_name, "arguments", "must be a mapping")
        object.__setattr__(
            self,
            "arguments",
            _snapshot_json(self.arguments, type_name, "arguments"),
        )
        object.__setattr__(
            self,
            "thoughtSignature",
            _optional_string(self.thoughtSignature, type_name, "thoughtSignature"),
        )


_AssistantContent: TypeAlias = TextContent | ToolCall


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class UserMessage(_PublicValueRecord):
    content: str | tuple[TextContent, ...]
    timestamp: int
    role: Literal["user"] = field(init=False, default="user")

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        if type(self.content) is str:
            content: str | tuple[TextContent, ...] = _check_unicode(
                self.content, type_name, "content"
            )
        else:
            content = _sequence(self.content, (TextContent,), type_name, "content")
        object.__setattr__(self, "content", content)
        object.__setattr__(
            self,
            "timestamp",
            _safe_integer(self.timestamp, type_name, "timestamp", nonnegative=False),
        )


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class AssistantMessage(_PublicValueRecord):
    content: tuple[_AssistantContent, ...]
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

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        object.__setattr__(
            self,
            "content",
            _sequence(self.content, (TextContent, ToolCall), type_name, "content"),
        )
        for name in ("api", "provider", "model"):
            object.__setattr__(
                self, name, _string(getattr(self, name), type_name, name)
            )
        if type(self.usage) is not Usage:
            _fail_type(type_name, "usage", "must be a Usage")
        if type(self.stopReason) is not str:
            _fail_type(type_name, "stopReason", "must be a string")
        if self.stopReason not in ("stop", "length", "toolUse", "error", "aborted"):
            _fail_value(type_name, "stopReason", "is not an admitted StopReason")
        object.__setattr__(
            self,
            "timestamp",
            _safe_integer(self.timestamp, type_name, "timestamp", nonnegative=False),
        )
        for name in ("responseModel", "responseId", "errorMessage"):
            object.__setattr__(
                self,
                name,
                _optional_string(getattr(self, name), type_name, name),
            )
        if self.stopReason in ("stop", "length", "toolUse"):
            if self.errorMessage is not None:
                _fail_value(type_name, "errorMessage", "must be absent for a done reason")
        elif self.errorMessage is None:
            _fail_value(type_name, "errorMessage", "must be present for an error reason")


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class ToolResultMessage(_PublicValueRecord):
    toolCallId: str
    toolName: str
    content: tuple[TextContent, ...]
    details: JSONValue
    isError: bool
    timestamp: int
    role: Literal["toolResult"] = field(init=False, default="toolResult")

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        object.__setattr__(
            self, "toolCallId", _string(self.toolCallId, type_name, "toolCallId")
        )
        object.__setattr__(
            self, "toolName", _string(self.toolName, type_name, "toolName")
        )
        object.__setattr__(
            self,
            "content",
            _sequence(self.content, (TextContent,), type_name, "content"),
        )
        object.__setattr__(
            self, "details", _snapshot_json(self.details, type_name, "details")
        )
        object.__setattr__(self, "isError", _bool(self.isError, type_name, "isError"))
        object.__setattr__(
            self,
            "timestamp",
            _safe_integer(self.timestamp, type_name, "timestamp", nonnegative=False),
        )


Message: TypeAlias = UserMessage | AssistantMessage | ToolResultMessage


@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class Tool(_PublicValueRecord):
    name: str
    description: str
    parameters: Mapping[str, JSONValue]

    def __init_subclass__(cls) -> None:
        if cls.__module__ != "oh_my_core._tools" or cls.__name__ != "AgentTool":
            raise TypeError("Tool is sealed except for AgentTool")

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        object.__setattr__(self, "name", _string(self.name, type_name, "name"))
        object.__setattr__(
            self,
            "description",
            _string(self.description, type_name, "description"),
        )
        parameters = _snapshot_json(self.parameters, type_name, "parameters")
        if not isinstance(parameters, Mapping):
            _fail_type(type_name, "parameters", "must be a mapping")
        from ._tool_schema import admit_tool_schema

        admit_tool_schema(parameters)
        object.__setattr__(self, "parameters", parameters)


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class Context(_PublicValueRecord):
    messages: tuple[Message, ...]
    systemPrompt: str | None = None
    tools: tuple[Tool, ...] | None = None

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        object.__setattr__(
            self,
            "messages",
            _sequence(
                self.messages,
                (UserMessage, AssistantMessage, ToolResultMessage),
                type_name,
                "messages",
            ),
        )
        object.__setattr__(
            self,
            "systemPrompt",
            _optional_string(self.systemPrompt, type_name, "systemPrompt"),
        )
        if self.tools is not None:
            if type(self.tools) not in (list, tuple):
                _fail_type(type_name, "tools", "must be a list or tuple")
            tools = tuple(self.tools)
            if any(not isinstance(tool, Tool) for tool in tools):
                _fail_type(type_name, "tools", "must contain Tool values")
            model_tools = tuple(
                tool
                if type(tool) is Tool
                else Tool(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.parameters,
                )
                for tool in tools
            )
            object.__setattr__(
                self,
                "tools",
                model_tools,
            )


def _event_index(value: object, type_name: str) -> int:
    return _safe_integer(value, type_name, "contentIndex", nonnegative=True)


def _partial_block(
    partial: object,
    index: int,
    block_type: type[object],
    type_name: str,
) -> _AssistantContent:
    if type(partial) is not AssistantMessage:
        _fail_type(type_name, "partial", "must be an AssistantMessage")
    message = partial
    if index >= len(message.content):
        _fail_value(type_name, "contentIndex", "must identify partial content")
    block = message.content[index]
    if type(block) is not block_type:
        _fail_value(type_name, f"partial.content[{index}]", "has the wrong block kind")
    return block


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class AssistantMessageStartEvent(_PublicValueRecord):
    partial: AssistantMessage
    type: Literal["start"] = field(init=False, default="start")

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        if type(self.partial) is not AssistantMessage:
            _fail_type(type_name, "partial", "must be an AssistantMessage")
        if self.partial.content:
            _fail_value(type_name, "partial.content", "must be empty")


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class AssistantMessageTextStartEvent(_PublicValueRecord):
    contentIndex: int
    partial: AssistantMessage
    type: Literal["text_start"] = field(init=False, default="text_start")

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        index = _event_index(self.contentIndex, type_name)
        block = cast(
            TextContent, _partial_block(self.partial, index, TextContent, type_name)
        )
        if block.text != "":
            _fail_value(type_name, f"partial.content[{index}]", "must start empty")


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class AssistantMessageTextDeltaEvent(_PublicValueRecord):
    contentIndex: int
    delta: str
    partial: AssistantMessage
    type: Literal["text_delta"] = field(init=False, default="text_delta")

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        index = _event_index(self.contentIndex, type_name)
        object.__setattr__(self, "delta", _string(self.delta, type_name, "delta"))
        _partial_block(self.partial, index, TextContent, type_name)


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class AssistantMessageTextEndEvent(_PublicValueRecord):
    contentIndex: int
    content: str
    partial: AssistantMessage
    type: Literal["text_end"] = field(init=False, default="text_end")

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        index = _event_index(self.contentIndex, type_name)
        content = _string(self.content, type_name, "content")
        block = cast(
            TextContent, _partial_block(self.partial, index, TextContent, type_name)
        )
        if block.text != content:
            _fail_value(type_name, "content", "must equal the indexed partial text")


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class AssistantMessageToolCallStartEvent(_PublicValueRecord):
    contentIndex: int
    partial: AssistantMessage
    type: Literal["toolcall_start"] = field(init=False, default="toolcall_start")

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        _partial_block(
            self.partial,
            _event_index(self.contentIndex, type_name),
            ToolCall,
            type_name,
        )


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class AssistantMessageToolCallDeltaEvent(_PublicValueRecord):
    contentIndex: int
    delta: str
    partial: AssistantMessage
    type: Literal["toolcall_delta"] = field(init=False, default="toolcall_delta")

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        index = _event_index(self.contentIndex, type_name)
        object.__setattr__(self, "delta", _string(self.delta, type_name, "delta"))
        _partial_block(self.partial, index, ToolCall, type_name)


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class AssistantMessageToolCallEndEvent(_PublicValueRecord):
    contentIndex: int
    toolCall: ToolCall
    partial: AssistantMessage
    type: Literal["toolcall_end"] = field(init=False, default="toolcall_end")

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        index = _event_index(self.contentIndex, type_name)
        if type(self.toolCall) is not ToolCall:
            _fail_type(type_name, "toolCall", "must be a ToolCall")
        block = _partial_block(self.partial, index, ToolCall, type_name)
        if block != self.toolCall:
            _fail_value(type_name, "toolCall", "must equal the indexed partial block")


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class AssistantMessageDoneEvent(_PublicValueRecord):
    reason: _DoneReason
    message: AssistantMessage
    type: Literal["done"] = field(init=False, default="done")

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        if type(self.reason) is not str:
            _fail_type(type_name, "reason", "must be a string")
        if self.reason not in ("stop", "length", "toolUse"):
            _fail_value(type_name, "reason", "must be a done reason")
        if type(self.message) is not AssistantMessage:
            _fail_type(type_name, "message", "must be an AssistantMessage")
        if self.message.stopReason != self.reason:
            _fail_value(type_name, "reason", "must equal message.stopReason")


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class AssistantMessageErrorEvent(_PublicValueRecord):
    reason: _ErrorReason
    error: AssistantMessage
    type: Literal["error"] = field(init=False, default="error")

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        if type(self.reason) is not str:
            _fail_type(type_name, "reason", "must be a string")
        if self.reason not in ("error", "aborted"):
            _fail_value(type_name, "reason", "must be an error reason")
        if type(self.error) is not AssistantMessage:
            _fail_type(type_name, "error", "must be an AssistantMessage")
        if self.error.stopReason != self.reason:
            _fail_value(type_name, "reason", "must equal error.stopReason")


AssistantMessageEvent: TypeAlias = (
    AssistantMessageStartEvent
    | AssistantMessageTextStartEvent
    | AssistantMessageTextDeltaEvent
    | AssistantMessageTextEndEvent
    | AssistantMessageToolCallStartEvent
    | AssistantMessageToolCallDeltaEvent
    | AssistantMessageToolCallEndEvent
    | AssistantMessageDoneEvent
    | AssistantMessageErrorEvent
)

_ASSISTANT_EVENT_SPECS: dict[
    str, tuple[type[_PublicValueRecord], frozenset[str]]
] = {
    "start": (AssistantMessageStartEvent, frozenset({"partial"})),
    "text_start": (
        AssistantMessageTextStartEvent,
        frozenset({"contentIndex", "partial"}),
    ),
    "text_delta": (
        AssistantMessageTextDeltaEvent,
        frozenset({"contentIndex", "delta", "partial"}),
    ),
    "text_end": (
        AssistantMessageTextEndEvent,
        frozenset({"contentIndex", "content", "partial"}),
    ),
    "toolcall_start": (
        AssistantMessageToolCallStartEvent,
        frozenset({"contentIndex", "partial"}),
    ),
    "toolcall_delta": (
        AssistantMessageToolCallDeltaEvent,
        frozenset({"contentIndex", "delta", "partial"}),
    ),
    "toolcall_end": (
        AssistantMessageToolCallEndEvent,
        frozenset({"contentIndex", "toolCall", "partial"}),
    ),
    "done": (AssistantMessageDoneEvent, frozenset({"reason", "message"})),
    "error": (AssistantMessageErrorEvent, frozenset({"reason", "error"})),
}
_ASSISTANT_EVENT_TYPES = tuple(spec[0] for spec in _ASSISTANT_EVENT_SPECS.values())
_ASSISTANT_EVENT_SPECS_BY_TYPE = {
    event_type: (tag, required)
    for tag, (event_type, required) in _ASSISTANT_EVENT_SPECS.items()
}


class _AssistantMessageEventValidator:
    __slots__ = ("_ended", "_fragments", "_kinds", "_partial", "_result", "_started")

    def __init__(self) -> None:
        self._started = False
        self._ended: set[int] = set()
        self._kinds: dict[int, str] = {}
        self._fragments: dict[int, str] = {}
        self._partial: AssistantMessage | None = None
        self._result: AssistantMessage | None = None

    @property
    def result(self) -> AssistantMessage | None:
        return self._result

    def accept(self, event: AssistantMessageEvent) -> None:
        if type(event) not in _ASSISTANT_EVENT_TYPES:
            _fail_type("AssistantMessageEvent", "type", "is not an admitted event")
        if self._result is not None:
            _fail_value("AssistantMessageEvent", "type", "must not follow a terminal event")

        if isinstance(event, AssistantMessageErrorEvent):
            if self._started:
                self._check_terminal(event.error)
            self._result = event.error
            return
        if not self._started:
            if not isinstance(event, AssistantMessageStartEvent):
                _fail_value("AssistantMessageEvent", "type", "must start with start or error")
            self._started = True
            self._partial = event.partial
            return
        if isinstance(event, AssistantMessageStartEvent):
            _fail_value("AssistantMessageEvent", "type", "start must be unique")
        if isinstance(event, AssistantMessageDoneEvent):
            if self._ended != set(self._kinds):
                _fail_value("AssistantMessageEvent", "type", "done requires ended content")
            self._check_terminal(event.message)
            self._result = event.message
            return

        assert self._partial is not None
        content_event = event
        index = content_event.contentIndex
        partial = content_event.partial
        self._check_identity(partial)

        if isinstance(
            content_event,
            (AssistantMessageTextStartEvent, AssistantMessageToolCallStartEvent),
        ):
            if index != len(self._partial.content):
                _fail_value(
                    "AssistantMessageEvent",
                    "contentIndex",
                    "must open the next contiguous index",
                )
            if partial.content[:index] != self._partial.content:
                _fail_value(
                    "AssistantMessageEvent",
                    f"partial.content[{index}]",
                    "must append to the prior partial",
                )
            if len(partial.content) != index + 1:
                _fail_value(
                    "AssistantMessageEvent",
                    f"partial.content[{index + 1}]",
                    "must not appear before its own start event",
                )
            kind = (
                "text"
                if isinstance(content_event, AssistantMessageTextStartEvent)
                else "toolCall"
            )
            self._kinds[index] = kind
            if kind == "toolCall":
                self._fragments[index] = ""
        else:
            expected_kind = (
                "text"
                if isinstance(content_event, (
                    AssistantMessageTextDeltaEvent,
                    AssistantMessageTextEndEvent,
                ))
                else "toolCall"
            )
            if self._kinds.get(index) != expected_kind or index in self._ended:
                _fail_value(
                    "AssistantMessageEvent",
                    "contentIndex",
                    "does not identify an open matching block",
                )
            if len(partial.content) != len(self._partial.content):
                _fail_value(
                    "AssistantMessageEvent", "partial.content", "must retain prior indices"
                )
            for other_index, block in enumerate(partial.content):
                if other_index != index and block != self._partial.content[other_index]:
                    _fail_value(
                        "AssistantMessageEvent",
                        f"partial.content[{other_index}]",
                        "must remain unchanged",
                    )

        if isinstance(content_event, AssistantMessageTextDeltaEvent):
            before = self._partial.content[index]
            after = partial.content[index]
            assert type(before) is TextContent and type(after) is TextContent
            if after.text != before.text + content_event.delta:
                _fail_value(
                    "AssistantMessageEvent",
                    f"partial.content[{index}]",
                    "must cumulatively append delta",
                )
        elif isinstance(content_event, AssistantMessageToolCallDeltaEvent):
            from ._canonical import _decodeJSONValue, encodeCanonical

            fragments = self._fragments[index] + content_event.delta
            before = self._partial.content[index]
            after = partial.content[index]
            assert type(before) is ToolCall and type(after) is ToolCall
            expected_arguments = before.arguments
            try:
                decoded = _decodeJSONValue(fragments.encode("utf-8"))
            except ValueError:
                pass
            else:
                if isinstance(decoded, Mapping):
                    expected_arguments = decoded
            if encodeCanonical(after.arguments) != encodeCanonical(expected_arguments):
                _fail_value(
                    "AssistantMessageEvent",
                    f"partial.content[{index}]",
                    "arguments must follow the last complete object prefix",
                )
            self._fragments[index] = fragments
        elif isinstance(
            content_event,
            (AssistantMessageTextEndEvent, AssistantMessageToolCallEndEvent),
        ):
            if isinstance(content_event, AssistantMessageToolCallEndEvent):
                fragments = self._fragments[index]
                if fragments:
                    from ._canonical import _decodeJSONValue, encodeCanonical

                    decoded = _decodeJSONValue(fragments.encode("utf-8"))
                    if not isinstance(decoded, Mapping) or encodeCanonical(
                        decoded
                    ) != encodeCanonical(content_event.toolCall.arguments):
                        _fail_value(
                            "AssistantMessageEvent",
                            f"partial.content[{index}]",
                            "does not match finalized Tool Call arguments",
                        )
            self._ended.add(index)
        self._partial = partial

    def _check_identity(self, partial: AssistantMessage) -> None:
        assert self._partial is not None
        for name in ("api", "provider", "model"):
            if getattr(partial, name) != getattr(self._partial, name):
                _fail_value("AssistantMessageEvent", f"partial.{name}", "must remain fixed")
        for name in ("responseModel", "responseId"):
            before = getattr(self._partial, name)
            after = getattr(partial, name)
            if before is not None and after != before:
                _fail_value(
                    "AssistantMessageEvent", f"partial.{name}", "must remain fixed once present"
                )

    def _check_terminal(self, terminal: AssistantMessage) -> None:
        assert self._partial is not None
        self._check_identity(terminal)
        if terminal.content != self._partial.content:
            _fail_value(
                "AssistantMessageEvent", "partial.content", "must equal terminal content"
            )
