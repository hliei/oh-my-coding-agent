from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields
import json
import math
import re
from typing import Any, TypeVar, cast, overload

from ._values import (
    AssistantMessage,
    AssistantMessageEvent,
    Context,
    JSONValue,
    Message,
    TextContent,
    Tool,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
    _PublicValueRecord,
    _ASSISTANT_EVENT_SPECS,
    _ASSISTANT_EVENT_SPECS_BY_TYPE,
    _check_unicode,
    _finite_float,
    _safe_integer,
    _snapshot_json,
)


_T = TypeVar("_T")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_MISSING = object()


def _quote(value: str) -> bytes:
    _check_unicode(value, "Canonical", "value")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _float_token(value: float) -> bytes:
    _finite_float(value, "Canonical", "value", nonnegative=False)
    token = repr(value).lower()
    if "e" in token:
        mantissa, exponent = token.split("e", 1)
        token = f"{mantissa}e{int(exponent)}"
    return token.encode("ascii")


def _record_items(value: _PublicValueRecord) -> list[tuple[str, object]]:
    result: list[tuple[str, object]] = []
    for item in fields(cast(Any, value)):
        field_value = getattr(value, item.name)
        if field_value is None and item.default is None:
            continue
        result.append((item.name, field_value))
    result.sort(key=lambda pair: pair[0])
    return result


def encodeCanonical(value: object) -> bytes:
    output = bytearray()
    stack: list[object] = [value]
    while stack:
        current = stack.pop()
        if type(current) is bytes:
            output.extend(current)
        elif current is None:
            output.extend(b"null")
        elif type(current) is bool:
            output.extend(b"true" if current else b"false")
        elif type(current) is int:
            output.extend(
                str(
                    _safe_integer(
                        current, "Canonical", "value", nonnegative=False
                    )
                ).encode("ascii")
            )
        elif type(current) is float:
            output.extend(_float_token(current))
        elif type(current) is str:
            output.extend(_quote(current))
        elif isinstance(current, _PublicValueRecord):
            items = _record_items(current)
            output.extend(b"{")
            tokens: list[object] = []
            for index, (key, item_value) in enumerate(items):
                if index:
                    tokens.append(b",")
                tokens.extend((_quote(key), b":", item_value))
            tokens.append(b"}")
            stack.extend(reversed(tokens))
        elif type(current) is tuple:
            output.extend(b"[")
            tokens = []
            for index, item_value in enumerate(current):
                if index:
                    tokens.append(b",")
                tokens.append(item_value)
            tokens.append(b"]")
            stack.extend(reversed(tokens))
        elif isinstance(current, Mapping):
            items = list(current.items())
            for key, _ in items:
                if type(key) is not str:
                    raise TypeError("Canonical.value: mapping keys must be strings")
                _check_unicode(key, "Canonical", "value")
            items.sort(key=lambda pair: pair[0])
            output.extend(b"{")
            tokens = []
            for index, (key, item_value) in enumerate(items):
                if index:
                    tokens.append(b",")
                tokens.extend((_quote(key), b":", item_value))
            tokens.append(b"}")
            stack.extend(reversed(tokens))
        else:
            raise TypeError("Canonical.value: unsupported carrier")
    return bytes(output)


class _Frame:
    __slots__ = ("container", "key", "kind", "state")

    def __init__(self, kind: str, container: list[Any] | dict[str, Any]) -> None:
        self.kind = kind
        self.container = container
        self.state = "first"
        self.key: str | None = None


def _scan_string(text: str, start: int) -> tuple[str, int]:
    index = start + 1
    escaped = False
    while index < len(text):
        character = text[index]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == '"':
            token = text[start : index + 1]
            try:
                value = json.loads(token)
            except (TypeError, ValueError) as error:
                raise ValueError("invalid canonical JSON string") from error
            if type(value) is not str:
                raise ValueError("invalid canonical JSON string")
            _check_unicode(value, "Canonical", "value")
            return value, index + 1
        index += 1
    raise ValueError("unterminated canonical JSON string")


def _skip_whitespace(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _parse_json(data: bytes) -> object:
    if not isinstance(data, bytes):
        raise TypeError("Canonical.data: must be bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("canonical input must be strict UTF-8") from error
    if text.startswith("\ufeff"):
        raise ValueError("canonical input must not contain a BOM")

    index = 0
    root: object = _MISSING
    stack: list[_Frame] = []

    def attach(value: object) -> None:
        nonlocal root
        if not stack:
            if root is not _MISSING:
                raise ValueError("canonical input has multiple roots")
            root = value
            return
        parent = stack[-1]
        if parent.kind == "array":
            if parent.state not in ("first", "value"):
                raise ValueError("canonical array did not expect a value")
            assert isinstance(parent.container, list)
            parent.container.append(value)
            parent.state = "comma"
        else:
            if parent.state != "value" or parent.key is None:
                raise ValueError("canonical object did not expect a value")
            assert isinstance(parent.container, dict)
            parent.container[parent.key] = value
            parent.key = None
            parent.state = "comma"

    def parse_value() -> None:
        nonlocal index
        if index >= len(text):
            raise ValueError("canonical input ended before a value")
        character = text[index]
        if character == "{":
            object_value: dict[str, Any] = {}
            attach(object_value)
            stack.append(_Frame("object", object_value))
            index += 1
            return
        if character == "[":
            array: list[Any] = []
            attach(array)
            stack.append(_Frame("array", array))
            index += 1
            return
        if character == '"':
            string_value, index = _scan_string(text, index)
            attach(string_value)
            return
        for literal, literal_value in (
            ("null", None),
            ("true", True),
            ("false", False),
        ):
            if text.startswith(literal, index):
                index += len(literal)
                attach(literal_value)
                return
        match = _NUMBER.match(text, index)
        if match is None:
            raise ValueError("canonical input contains an invalid value")
        token = match.group(0)
        index = match.end()
        if any(marker in token for marker in ".eE"):
            number: object = float(token)
            _finite_float(number, "Canonical", "value", nonnegative=False)
        else:
            number = int(token)
            _safe_integer(number, "Canonical", "value", nonnegative=False)
        attach(number)

    while True:
        index = _skip_whitespace(text, index)
        if stack:
            frame = stack[-1]
            if frame.kind == "array":
                if frame.state == "first" and index < len(text) and text[index] == "]":
                    stack.pop()
                    index += 1
                    continue
                if frame.state in ("first", "value"):
                    parse_value()
                    continue
                if index < len(text) and text[index] == "]":
                    stack.pop()
                    index += 1
                    continue
                if index < len(text) and text[index] == ",":
                    frame.state = "value"
                    index += 1
                    continue
                raise ValueError("canonical array requires a comma or end")

            assert isinstance(frame.container, dict)
            if frame.state == "first" and index < len(text) and text[index] == "}":
                stack.pop()
                index += 1
                continue
            if frame.state in ("first", "key"):
                if index >= len(text) or text[index] != '"':
                    raise ValueError("canonical object requires a string key")
                key, index = _scan_string(text, index)
                if key in frame.container:
                    raise ValueError("canonical object contains a duplicate key")
                frame.key = key
                frame.state = "colon"
                continue
            if frame.state == "colon":
                if index >= len(text) or text[index] != ":":
                    raise ValueError("canonical object requires a colon")
                frame.state = "value"
                index += 1
                continue
            if frame.state == "value":
                parse_value()
                continue
            if index < len(text) and text[index] == "}":
                stack.pop()
                index += 1
                continue
            if index < len(text) and text[index] == ",":
                frame.state = "key"
                index += 1
                continue
            raise ValueError("canonical object requires a comma or end")

        if root is _MISSING:
            parse_value()
            continue
        index = _skip_whitespace(text, index)
        if index != len(text):
            raise ValueError("canonical input has trailing data")
        return root


def _object(value: object) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError("canonical record must be an object")
    return value


def _array(value: object) -> list[Any]:
    if type(value) is not list:
        raise ValueError("canonical sequence must be an array")
    return value


def _fields(
    value: object,
    *,
    required: set[str],
    optional: set[str] | None = None,
    tag: tuple[str, str] | None = None,
) -> dict[str, Any]:
    raw = _object(value)
    allowed = required | (optional or set()) | ({tag[0]} if tag else set())
    if set(raw) - allowed:
        raise ValueError("canonical record contains unknown fields")
    if required - set(raw):
        raise ValueError("canonical record is missing required fields")
    if tag is not None and raw.get(tag[0]) != tag[1]:
        raise ValueError("canonical record has an invalid discriminator")
    return raw


def _content(value: object) -> TextContent | ToolCall:
    raw = _object(value)
    tag = raw.get("type")
    if tag == "text":
        return cast(TextContent, _construct(TextContent, raw))
    if tag == "toolCall":
        return cast(ToolCall, _construct(ToolCall, raw))
    raise ValueError("canonical content has an invalid discriminator")


def _message(value: object) -> Message:
    raw = _object(value)
    role = raw.get("role")
    if role == "user":
        return cast(UserMessage, _construct(UserMessage, raw))
    if role == "assistant":
        return cast(AssistantMessage, _construct(AssistantMessage, raw))
    if role == "toolResult":
        return cast(ToolResultMessage, _construct(ToolResultMessage, raw))
    raise ValueError("canonical Message has an invalid discriminator")


def _event(value: object) -> AssistantMessageEvent:
    raw = _object(value)
    raw_type = raw.get("type")
    spec = _ASSISTANT_EVENT_SPECS.get(raw_type) if type(raw_type) is str else None
    if spec is None:
        raise ValueError("canonical event has an invalid discriminator")
    variant = spec[0]
    return cast(AssistantMessageEvent, _construct(variant, raw))


def _construct(expected: object, value: object) -> Any:
    if expected is JSONValue:
        return _snapshot_json(value, "Canonical", "value")
    if expected is Message:
        return _message(value)
    if expected is AssistantMessageEvent:
        return _event(value)

    if expected is TextContent:
        raw = _fields(
            value,
            required={"text"},
            optional={"textSignature"},
            tag=("type", "text"),
        )
        return TextContent(
            text=raw["text"],
            **({"textSignature": raw["textSignature"]} if "textSignature" in raw else {}),
        )
    if expected is ToolCall:
        raw = _fields(
            value,
            required={"id", "name", "arguments"},
            optional={"thoughtSignature"},
            tag=("type", "toolCall"),
        )
        return ToolCall(
            id=raw["id"],
            name=raw["name"],
            arguments=raw["arguments"],
            **(
                {"thoughtSignature": raw["thoughtSignature"]}
                if "thoughtSignature" in raw
                else {}
            ),
        )
    if expected is Tool:
        raw = _fields(
            value,
            required={"name", "description", "parameters"},
        )
        return Tool(
            name=raw["name"],
            description=raw["description"],
            parameters=raw["parameters"],
        )
    if expected is UsageCost:
        raw = _fields(
            value,
            required={"input", "output", "cacheRead", "cacheWrite", "total"},
        )
        return UsageCost(**raw)
    if expected is Usage:
        raw = _fields(
            value,
            required={"input", "output", "cacheRead", "cacheWrite", "totalTokens", "cost"},
            optional={"cacheWrite1h"},
        )
        return Usage(
            input=raw["input"],
            output=raw["output"],
            cacheRead=raw["cacheRead"],
            cacheWrite=raw["cacheWrite"],
            totalTokens=raw["totalTokens"],
            cost=_construct(UsageCost, raw["cost"]),
            **({"cacheWrite1h": raw["cacheWrite1h"]} if "cacheWrite1h" in raw else {}),
        )
    if expected is UserMessage:
        raw = _fields(value, required={"content", "timestamp"}, tag=("role", "user"))
        content = raw["content"]
        if type(content) is list:
            content = tuple(_construct(TextContent, item) for item in content)
        return UserMessage(content=content, timestamp=raw["timestamp"])
    if expected is AssistantMessage:
        raw = _fields(
            value,
            required={"content", "api", "provider", "model", "usage", "stopReason", "timestamp"},
            optional={"responseModel", "responseId", "errorMessage"},
            tag=("role", "assistant"),
        )
        kwargs = {
            name: raw[name]
            for name in ("responseModel", "responseId", "errorMessage")
            if name in raw
        }
        return AssistantMessage(
            content=tuple(_content(item) for item in _array(raw["content"])),
            api=raw["api"],
            provider=raw["provider"],
            model=raw["model"],
            usage=_construct(Usage, raw["usage"]),
            stopReason=raw["stopReason"],
            timestamp=raw["timestamp"],
            **kwargs,
        )
    if expected is ToolResultMessage:
        raw = _fields(
            value,
            required={"toolCallId", "toolName", "content", "details", "isError", "timestamp"},
            tag=("role", "toolResult"),
        )
        return ToolResultMessage(
            toolCallId=raw["toolCallId"],
            toolName=raw["toolName"],
            content=tuple(_construct(TextContent, item) for item in _array(raw["content"])),
            details=raw["details"],
            isError=raw["isError"],
            timestamp=raw["timestamp"],
        )
    if expected is Context:
        raw = _fields(
            value,
            required={"messages"},
            optional={"systemPrompt", "tools"},
        )
        kwargs = {name: raw[name] for name in ("systemPrompt",) if name in raw}
        if "tools" in raw:
            kwargs["tools"] = tuple(
                _construct(Tool, item) for item in _array(raw["tools"])
            )
        return Context(
            messages=tuple(_message(item) for item in _array(raw["messages"])),
            **kwargs,
        )

    event_type = cast(type[Any], expected)
    if event_type in _ASSISTANT_EVENT_SPECS_BY_TYPE:
        tag, required = _ASSISTANT_EVENT_SPECS_BY_TYPE[event_type]
        raw = _fields(value, required=set(required), tag=("type", tag))
        kwargs = dict(raw)
        kwargs.pop("type", None)
        if "partial" in kwargs:
            kwargs["partial"] = _construct(AssistantMessage, kwargs["partial"])
        if "message" in kwargs:
            kwargs["message"] = _construct(AssistantMessage, kwargs["message"])
        if "error" in kwargs:
            kwargs["error"] = _construct(AssistantMessage, kwargs["error"])
        if "toolCall" in kwargs:
            kwargs["toolCall"] = _construct(ToolCall, kwargs["toolCall"])
        return event_type(**kwargs)
    raise TypeError("Canonical.expected: unsupported root")


def _decodeJSONValue(data: bytes) -> JSONValue:
    raw = _parse_json(data)
    return _snapshot_json(raw, "Canonical", "value")


@overload
def decodeCanonical(data: bytes, expected: type[_T]) -> _T: ...


@overload
def decodeCanonical(data: bytes, expected: object) -> Any: ...


def decodeCanonical(data: bytes, expected: object) -> Any:
    raw = _parse_json(data)
    try:
        value = _construct(expected, raw)
        reencoded = encodeCanonical(value)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid canonical value") from error
    if reencoded != data:
        raise ValueError("canonical input does not use the unique byte spelling")
    return value
