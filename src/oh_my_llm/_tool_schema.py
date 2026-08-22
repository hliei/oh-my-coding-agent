from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
import math
from typing import Any, NoReturn, cast

import re2  # type: ignore[import-untyped]


_DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
_SCHEMA_TYPES = frozenset(
    ("null", "boolean", "integer", "number", "string", "array", "object")
)
_GENERAL_KEYWORDS = frozenset(("type", "title", "description", "enum", "const", "anyOf"))
_KEYWORDS_BY_TYPE = {
    "object": frozenset(("properties", "required", "additionalProperties")),
    "array": frozenset(("items", "minItems", "maxItems")),
    "string": frozenset(("minLength", "maxLength", "pattern")),
    "integer": frozenset(
        ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")
    ),
    "number": frozenset(
        ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")
    ),
}
_SURROGATE_TOKEN_BASE = 0xF0000
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_ESCAPABLE_PATTERN_LITERALS = frozenset(r"^$\.*+?()[]{}|/-")


def _expand_utf16_units(value: str) -> str:
    expanded: list[str] = []
    for character in value:
        code_point = ord(character)
        if code_point <= 0xFFFF:
            expanded.append(character)
            continue
        offset = code_point - 0x10000
        high = 0xD800 + (offset >> 10)
        low = 0xDC00 + (offset & 0x3FF)
        expanded.extend(
            (
                chr(_SURROGATE_TOKEN_BASE + high - 0xD800),
                chr(_SURROGATE_TOKEN_BASE + low - 0xD800),
            )
        )
    return "".join(expanded)


def _normalize_pattern_escapes(pattern: str) -> str:
    output: list[str] = []
    index = 0
    in_class = False
    class_has_member = False
    class_allows_negation = False
    while index < len(pattern):
        character = pattern[index]
        if character != "\\":
            output.append(character)
            if character == "[" and not in_class:
                in_class = True
                class_has_member = False
                class_allows_negation = True
            elif in_class and not class_has_member and character == "^":
                if class_allows_negation:
                    class_allows_negation = False
                else:
                    class_has_member = True
            elif in_class and not class_has_member and character == "]":
                raise ValueError("pattern uses syntax outside the v0 Pattern Subset")
            elif character == "]" and in_class:
                in_class = False
            elif in_class:
                class_has_member = True
                class_allows_negation = False
            elif (
                character == "("
                and not in_class
                and pattern[index + 1 : index + 2] == "?"
                and pattern[index + 1 : index + 3] != "?:"
            ):
                raise ValueError("pattern uses syntax outside the v0 Pattern Subset")
            index += 1
            continue
        if index + 1 >= len(pattern):
            raise ValueError("pattern uses syntax outside the v0 Pattern Subset")
        if in_class:
            class_has_member = True
            class_allows_negation = False
        escaped = pattern[index + 1]
        if escaped == "0":
            if pattern[index + 2 : index + 3].isdigit():
                raise ValueError("pattern uses syntax outside the v0 Pattern Subset")
            output.append(r"\x00")
            index += 2
            continue
        if escaped in "123456789":
            raise ValueError("pattern uses syntax outside the v0 Pattern Subset")
        if escaped == "x":
            digits = pattern[index + 2 : index + 4]
            if len(digits) != 2 or any(digit not in _HEX_DIGITS for digit in digits):
                raise ValueError("pattern uses syntax outside the v0 Pattern Subset")
            output.append(f"\\x{digits}")
            index += 4
            continue
        if escaped == "u":
            digits = pattern[index + 2 : index + 6]
            if len(digits) != 4 or any(digit not in _HEX_DIGITS for digit in digits):
                raise ValueError("pattern uses syntax outside the v0 Pattern Subset")
            code_unit = int(digits, 16)
            if 0xD800 <= code_unit <= 0xDFFF:
                output.append(chr(_SURROGATE_TOKEN_BASE + code_unit - 0xD800))
            else:
                output.append(f"\\x{{{code_unit:x}}}")
            index += 6
            continue
        if escaped in "sS":
            if in_class:
                output.append("[:space:]" if escaped == "s" else "[:^space:]")
            else:
                output.append(
                    "[\\x09-\\x0d\\x20]"
                    if escaped == "s"
                    else "[^\\x09-\\x0d\\x20]"
                )
            index += 2
            continue
        if escaped in "dDwWfnrtv":
            output.extend(("\\", escaped))
            index += 2
            continue
        if escaped in "bB":
            if in_class:
                if escaped == "B":
                    raise ValueError("pattern uses syntax outside the v0 Pattern Subset")
                output.append(r"\x08")
            else:
                output.extend(("\\", escaped))
            index += 2
            continue
        if escaped in _ESCAPABLE_PATTERN_LITERALS:
            output.extend(("\\", escaped))
            index += 2
            continue
        raise ValueError("pattern uses syntax outside the v0 Pattern Subset")
    return "".join(output)


def _schema_type_error(path: str, explanation: str) -> NoReturn:
    raise TypeError(f"Tool.parameters{path}: {explanation}")


def _schema_value_error(path: str, explanation: str) -> NoReturn:
    raise ValueError(f"Tool.parameters{path}: {explanation}")


def _member(path: str, name: str) -> str:
    return f'{path}["{name}"]'


def _schema_types(value: object, path: str) -> tuple[str, ...]:
    if type(value) is str:
        values: tuple[str, ...] = (value,)
    elif type(value) is tuple:
        raw = cast(tuple[object, ...], value)
        if not raw:
            _schema_value_error(_member(path, "type"), "must not be empty")
        if any(type(item) is not str for item in raw):
            _schema_type_error(_member(path, "type"), "members must be strings")
        values = cast(tuple[str, ...], raw)
    else:
        _schema_type_error(_member(path, "type"), "must be a string or array")
    if len(values) != len(set(values)):
        _schema_value_error(_member(path, "type"), "members must be unique")
    if any(item not in _SCHEMA_TYPES for item in values):
        _schema_value_error(_member(path, "type"), "contains an unsupported type")
    return values


def _json_equal(left: object, right: object) -> bool:
    pending = [(left, right)]
    while pending:
        left, right = pending.pop()
        if type(left) is bool or type(right) is bool:
            if type(left) is not type(right) or left != right:
                return False
        elif type(left) in (int, float) and type(right) in (int, float):
            if cast(int | float, left) != cast(int | float, right):
                return False
        elif left is None or right is None or type(left) in (str,):
            if type(left) is not type(right) or left != right:
                return False
        elif type(left) in (list, tuple) and type(right) in (list, tuple):
            left_items = cast(list[object] | tuple[object, ...], left)
            right_items = cast(list[object] | tuple[object, ...], right)
            if len(left_items) != len(right_items):
                return False
            pending.extend(zip(left_items, right_items, strict=True))
        elif isinstance(left, Mapping) and isinstance(right, Mapping):
            if set(left) != set(right):
                return False
            pending.extend((left[key], right[key]) for key in left)
        else:
            return False
    return True


def _check_unique(values: tuple[object, ...], path: str) -> None:
    for index, value in enumerate(values):
        if any(_json_equal(value, prior) for prior in values[:index]):
            _schema_value_error(path, "members must be unique")


def _check_nonnegative_integer(value: object, path: str) -> None:
    if type(value) is not int:
        _schema_type_error(path, "must be a nonnegative integer")
    if value < 0:
        _schema_value_error(path, "must be a nonnegative integer")


def _check_number(value: object, path: str) -> None:
    if type(value) not in (int, float):
        _schema_type_error(path, "must be a number")
    if type(value) is float and not math.isfinite(value):
        _schema_value_error(path, "must be finite")


def _translate_pattern(pattern: str) -> str:
    output: list[str] = []
    escaped = False
    in_class = False
    for character in pattern:
        if escaped:
            output.extend(("\\", character))
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "[":
            in_class = True
            output.append(character)
        elif character == "]" and in_class:
            in_class = False
            output.append(character)
        elif character == "." and not in_class:
            output.append("[^\\n\\r\\x{2028}\\x{2029}]")
        elif character == "$" and not in_class:
            output.append(
                "(?:\\z|(?:\\r\\n|[\\n\\r\\x{2028}\\x{2029}])\\z)"
            )
        else:
            output.append(character)
    if escaped:
        output.append("\\")
    return "".join(output)


@lru_cache(maxsize=512)
def compile_pattern(pattern: str) -> Any:
    pattern = _normalize_pattern_escapes(_expand_utf16_units(pattern))
    try:
        return re2.compile(_translate_pattern(pattern))
    except re2.error as error:
        raise ValueError("pattern uses syntax outside the v0 Pattern Subset") from error


def pattern_matches(pattern: str, value: str) -> bool:
    compiled: Any = compile_pattern(pattern)
    return compiled.search(_expand_utf16_units(value)) is not None


def admit_tool_schema(value: object) -> None:
    if not isinstance(value, Mapping):
        _schema_type_error("", "must be an object schema")

    stack: list[tuple[Mapping[str, object], str, bool]] = [
        (cast(Mapping[str, object], value), "", True)
    ]
    while stack:
        schema, path, is_root = stack.pop()
        schema_type = schema.get("type")
        types = _schema_types(schema_type, path) if schema_type is not None else ()
        if is_root and (type(schema_type) is not str or schema_type != "object"):
            _schema_value_error(_member(path, "type"), 'must be exactly "object"')

        allowed = set(_GENERAL_KEYWORDS)
        if is_root:
            allowed.add("$schema")
        for schema_kind in types:
            allowed.update(_KEYWORDS_BY_TYPE.get(schema_kind, ()))
        for keyword in schema:
            if keyword not in allowed:
                _schema_value_error(_member(path, keyword), "is not admitted here")

        if "$schema" in schema:
            dialect = schema["$schema"]
            if type(dialect) is not str:
                _schema_type_error(_member(path, "$schema"), "must be a string")
            if dialect != _DRAFT_2020_12:
                _schema_value_error(
                    _member(path, "$schema"), "must select canonical Draft 2020-12"
                )
        for keyword in ("title", "description"):
            if keyword in schema and type(schema[keyword]) is not str:
                _schema_type_error(_member(path, keyword), "must be a string")

        if "enum" in schema:
            enum = schema["enum"]
            if type(enum) is not tuple:
                _schema_type_error(_member(path, "enum"), "must be an array")
            enum_values = cast(tuple[object, ...], enum)
            if not enum_values:
                _schema_value_error(_member(path, "enum"), "must not be empty")
            _check_unique(enum_values, _member(path, "enum"))

        if "anyOf" in schema:
            any_of = schema["anyOf"]
            if type(any_of) is not tuple:
                _schema_type_error(_member(path, "anyOf"), "must be an array")
            branches = cast(tuple[object, ...], any_of)
            if not branches:
                _schema_value_error(_member(path, "anyOf"), "must not be empty")
            for index in range(len(branches) - 1, -1, -1):
                branch = branches[index]
                if not isinstance(branch, Mapping):
                    _schema_type_error(
                        f'{_member(path, "anyOf")}[{index}]', "must be a schema object"
                    )
                stack.append(
                    (cast(Mapping[str, object], branch), f'{_member(path, "anyOf")}[{index}]', False)
                )

        if "properties" in schema:
            properties = schema["properties"]
            if not isinstance(properties, Mapping):
                _schema_type_error(_member(path, "properties"), "must be an object")
            for name, child in reversed(tuple(properties.items())):
                if not isinstance(child, Mapping):
                    _schema_type_error(
                        _member(_member(path, "properties"), name),
                        "must be a schema object",
                    )
                stack.append(
                    (
                        cast(Mapping[str, object], child),
                        _member(_member(path, "properties"), name),
                        False,
                    )
                )
        if "required" in schema:
            required = schema["required"]
            if type(required) is not tuple:
                _schema_type_error(_member(path, "required"), "must be an array")
            required_names = cast(tuple[object, ...], required)
            if any(type(name) is not str for name in required_names):
                _schema_type_error(_member(path, "required"), "members must be strings")
            if len(required_names) != len(set(required_names)):
                _schema_value_error(_member(path, "required"), "members must be unique")
        if "additionalProperties" in schema:
            additional = schema["additionalProperties"]
            if type(additional) is bool:
                pass
            elif isinstance(additional, Mapping):
                stack.append(
                    (
                        cast(Mapping[str, object], additional),
                        _member(path, "additionalProperties"),
                        False,
                    )
                )
            else:
                _schema_type_error(
                    _member(path, "additionalProperties"),
                    "must be a bool or schema object",
                )
        if "items" in schema:
            items = schema["items"]
            if not isinstance(items, Mapping):
                _schema_type_error(_member(path, "items"), "must be a schema object")
            stack.append(
                (cast(Mapping[str, object], items), _member(path, "items"), False)
            )
        for keyword in ("minItems", "maxItems", "minLength", "maxLength"):
            if keyword in schema:
                _check_nonnegative_integer(schema[keyword], _member(path, keyword))
        if "pattern" in schema:
            pattern = schema["pattern"]
            if type(pattern) is not str:
                _schema_type_error(_member(path, "pattern"), "must be a string")
            try:
                compile_pattern(pattern)
            except ValueError as error:
                _schema_value_error(_member(path, "pattern"), str(error))
        for keyword in (
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
        ):
            if keyword in schema:
                _check_number(schema[keyword], _member(path, keyword))
