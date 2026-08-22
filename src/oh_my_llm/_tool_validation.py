from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import math
from typing import Any, cast

from ._tool_schema import pattern_matches
from ._values import Tool, ToolCall


_SAFE_INTEGER = 2**53 - 1
_KEYWORD_ORDER = {
    keyword: index
    for index, keyword in enumerate(
        (
            "type",
            "enum",
            "const",
            "anyOf",
            "required",
            "additionalProperties",
            "minItems",
            "maxItems",
            "minLength",
            "maxLength",
            "pattern",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
        )
    )
}
_ISSUE_TEMPLATES = {
    "type": "must match the declared type",
    "enum": "must equal one of the admitted values",
    "const": "must equal the declared constant",
    "anyOf": "must match at least one branch",
    "required": "required property is missing",
    "additionalProperties": "additional property is not admitted",
    "minItems": "must contain at least the declared number of items",
    "maxItems": "must contain at most the declared number of items",
    "minLength": "must contain at least the declared number of characters",
    "maxLength": "must contain at most the declared number of characters",
    "pattern": "must match the declared pattern",
    "minimum": "must be at least the declared bound",
    "maximum": "must be at most the declared bound",
    "exclusiveMinimum": "must be greater than the declared bound",
    "exclusiveMaximum": "must be less than the declared bound",
}


@dataclass(frozen=True, slots=True)
class _Issue:
    pointer: str
    keyword: str


def _pointer(parent: str, segment: str | int) -> str:
    encoded = str(segment).replace("~", "~0").replace("/", "~1")
    return f"{parent}/{encoded}"


def _read(parent: list[Any] | dict[str, Any], key: int | str) -> Any:
    if type(parent) is list:
        assert type(key) is int
        return parent[key]
    assert type(key) is str
    return cast(dict[str, Any], parent)[key]


def _write(
    parent: list[Any] | dict[str, Any], key: int | str, value: object
) -> None:
    if type(parent) is list:
        assert type(key) is int
        parent[key] = value
    else:
        assert type(key) is str
        cast(dict[str, Any], parent)[key] = value


def _mutable_copy(value: object) -> object:
    root: list[object] = [None]
    stack: list[tuple[object, list[Any] | dict[str, Any], int | str]] = [
        (value, root, 0)
    ]
    while stack:
        current, parent, key = stack.pop()
        if isinstance(current, Mapping):
            copied: dict[str, Any] = {}
            _write(parent, key, copied)
            for name, item in reversed(tuple(current.items())):
                stack.append((item, copied, name))
        elif type(current) in (tuple, list):
            source = cast(tuple[object, ...] | list[object], current)
            copied_list: list[Any] = [None] * len(source)
            _write(parent, key, copied_list)
            for index in range(len(source) - 1, -1, -1):
                stack.append((source[index], copied_list, index))
        else:
            _write(parent, key, current)
    return root[0]


def _type_matches(value: object, schema_type: str) -> bool:
    if schema_type == "null":
        return value is None
    if schema_type == "boolean":
        return type(value) is bool
    if schema_type == "integer":
        if type(value) is int:
            return -_SAFE_INTEGER <= value <= _SAFE_INTEGER
        if type(value) is float:
            return math.isfinite(value) and value.is_integer() and abs(value) <= _SAFE_INTEGER
        return False
    if schema_type == "number":
        return type(value) is int or type(value) is float
    if schema_type == "string":
        return type(value) is str
    if schema_type == "array":
        return type(value) is list
    return type(value) is dict


def _schema_types(schema: Mapping[str, object]) -> tuple[str, ...]:
    value = schema.get("type")
    if type(value) is str:
        return (value,)
    if type(value) is tuple:
        return cast(tuple[str, ...], value)
    return ()


def _javascript_string(value: object) -> str | None:
    if value is None:
        return ""
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if type(value) is float:
        if value == 0.0:
            return "0"
        if value.is_integer() and abs(value) < 1e21:
            return str(int(value))
        token = repr(value).lower()
        if "e" in token:
            mantissa, exponent = token.split("e", 1)
            token = f"{mantissa}e{int(exponent):+d}"
        return token
    return None


def _convert_primitive(value: object, schema_type: str) -> object:
    if schema_type in ("integer", "number"):
        if value is None:
            return 0 if schema_type == "integer" else 0.0
        if type(value) is bool:
            converted_bool = 1 if value else 0
            return converted_bool if schema_type == "integer" else float(converted_bool)
        if type(value) is str and value.strip():
            try:
                number = float(value.strip())
            except ValueError:
                return value
            if not math.isfinite(number):
                return value
            if schema_type == "integer":
                if number.is_integer() and abs(number) <= _SAFE_INTEGER:
                    return int(number)
                return value
            return number
    elif schema_type == "boolean":
        if value is None:
            return False
        if value == "true":
            return True
        if value == "false":
            return False
        if type(value) in (int, float) and value in (0, 1):
            return bool(value)
    elif schema_type == "string":
        converted_string = _javascript_string(value)
        if converted_string is not None:
            return converted_string
    elif schema_type == "null":
        if value == "" or value is False or (
            type(value) in (int, float) and value == 0
        ):
            return None
    return value


def _json_equal(left: object, right: object) -> bool:
    pending = [(left, right)]
    while pending:
        left, right = pending.pop()
        if type(left) is bool or type(right) is bool:
            if type(left) is not type(right) or left != right:
                return False
        elif type(left) in (int, float) and type(right) in (int, float):
            if left != right:
                return False
        elif left is None or right is None or type(left) is str:
            if type(left) is not type(right) or left != right:
                return False
        elif type(left) is list and type(right) in (list, tuple):
            right_sequence = cast(list[object] | tuple[object, ...], right)
            if len(left) != len(right_sequence):
                return False
            pending.extend(zip(left, right_sequence, strict=True))
        elif type(left) is tuple and type(right) in (list, tuple):
            left_sequence = cast(tuple[object, ...], left)
            right_sequence = cast(list[object] | tuple[object, ...], right)
            if len(left_sequence) != len(right_sequence):
                return False
            pending.extend(zip(left_sequence, right_sequence, strict=True))
        elif isinstance(left, Mapping) and isinstance(right, Mapping):
            if set(left) != set(right):
                return False
            pending.extend((left[name], right[name]) for name in left)
        else:
            return False
    return True


def _bound_valid(keyword: str, value: int | float, bound: object) -> bool:
    assert type(bound) in (int, float)
    numeric_bound = cast(int | float, bound)
    if keyword == "minimum":
        return value >= numeric_bound
    if keyword == "maximum":
        return value <= numeric_bound
    if keyword == "exclusiveMinimum":
        return value > numeric_bound
    return value < numeric_bound


@dataclass(slots=True)
class _Evaluation:
    value: object | None = None
    issues: list[_Issue] | None = None


@dataclass(slots=True)
class _EvaluationFrame:
    schema: Mapping[str, object]
    value: object
    pointer: str
    result: _Evaluation
    issues: list[_Issue]
    any_index: int = 0


@dataclass(frozen=True, slots=True)
class _ChildEvaluation:
    frame: _EvaluationFrame
    parent: list[Any] | dict[str, Any]
    key: int | str
    result: _Evaluation


_EvaluationTask = tuple[
    str,
    _EvaluationFrame,
    _Evaluation | _ChildEvaluation | None,
]


def _begin_direct_evaluation(
    frame: _EvaluationFrame, tasks: list[_EvaluationTask]
) -> None:
    schema = frame.schema
    current = frame.value
    pointer = frame.pointer
    schema_types = _schema_types(schema)
    if schema_types and not any(_type_matches(current, item) for item in schema_types):
        for schema_type in schema_types:
            candidate = _convert_primitive(current, schema_type)
            if _type_matches(candidate, schema_type):
                current = candidate
                frame.value = candidate
                break

    type_valid = not schema_types or any(
        _type_matches(current, schema_type) for schema_type in schema_types
    )
    if not type_valid:
        frame.issues.append(_Issue(pointer, "type"))
    enum = schema.get("enum")
    if type(enum) is tuple and not any(_json_equal(current, item) for item in enum):
        frame.issues.append(_Issue(pointer, "enum"))
    if "const" in schema and not _json_equal(current, schema["const"]):
        frame.issues.append(_Issue(pointer, "const"))

    children: list[
        tuple[Mapping[str, object], list[Any] | dict[str, Any], int | str, str]
    ] = []
    if type_valid and type(current) is dict and "object" in schema_types:
        properties = schema.get("properties")
        property_schemas = (
            cast(Mapping[str, Mapping[str, object]], properties)
            if isinstance(properties, Mapping)
            else {}
        )
        required = schema.get("required")
        if type(required) is tuple:
            for name in cast(tuple[str, ...], required):
                if name not in current:
                    frame.issues.append(_Issue(_pointer(pointer, name), "required"))
        for name, child_schema in property_schemas.items():
            if name in current:
                children.append((child_schema, current, name, _pointer(pointer, name)))
        additional = schema.get("additionalProperties", True)
        for name in current:
            if name in property_schemas:
                continue
            child_pointer = _pointer(pointer, name)
            if additional is False:
                frame.issues.append(_Issue(child_pointer, "additionalProperties"))
            elif isinstance(additional, Mapping):
                children.append(
                    (cast(Mapping[str, object], additional), current, name, child_pointer)
                )
    elif type_valid and type(current) is list and "array" in schema_types:
        if "minItems" in schema and len(current) < cast(int, schema["minItems"]):
            frame.issues.append(_Issue(pointer, "minItems"))
        if "maxItems" in schema and len(current) > cast(int, schema["maxItems"]):
            frame.issues.append(_Issue(pointer, "maxItems"))
        items = schema.get("items")
        if isinstance(items, Mapping):
            item_schema = cast(Mapping[str, object], items)
            children.extend(
                (item_schema, current, index, _pointer(pointer, index))
                for index in range(len(current))
            )
    elif type_valid and type(current) is str and "string" in schema_types:
        if "minLength" in schema and len(current) < cast(int, schema["minLength"]):
            frame.issues.append(_Issue(pointer, "minLength"))
        if "maxLength" in schema and len(current) > cast(int, schema["maxLength"]):
            frame.issues.append(_Issue(pointer, "maxLength"))
        pattern = schema.get("pattern")
        if type(pattern) is str and not pattern_matches(pattern, current):
            frame.issues.append(_Issue(pointer, "pattern"))
    elif type_valid and type(current) in (int, float) and (
        "integer" in schema_types or "number" in schema_types
    ):
        for keyword in (
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
        ):
            if keyword in schema and not _bound_valid(
                keyword, cast(int | float, current), schema[keyword]
            ):
                frame.issues.append(_Issue(pointer, keyword))

    tasks.append(("finish", frame, None))
    for child_schema, parent, key, child_pointer in reversed(children):
        child_result = _Evaluation()
        tasks.append(
            (
                "after_child",
                frame,
                _ChildEvaluation(frame, parent, key, child_result),
            )
        )
        tasks.append(
            (
                "start",
                _EvaluationFrame(
                    child_schema,
                    _read(parent, key),
                    child_pointer,
                    child_result,
                    [],
                ),
                None,
            )
        )


def _evaluate(schema: Mapping[str, object], value: object) -> _Evaluation:
    root_result = _Evaluation()
    tasks: list[_EvaluationTask] = [
        (
            "start",
            _EvaluationFrame(schema, _mutable_copy(value), "#", root_result, []),
            None,
        )
    ]
    while tasks:
        operation, frame, payload = tasks.pop()
        if operation == "start":
            any_of = frame.schema.get("anyOf")
            if type(any_of) is tuple:
                tasks.append(("try_any", frame, None))
            else:
                tasks.append(("direct", frame, None))
        elif operation == "try_any":
            branches = cast(tuple[Mapping[str, object], ...], frame.schema["anyOf"])
            if frame.any_index == len(branches):
                frame.issues.append(_Issue(frame.pointer, "anyOf"))
                tasks.append(("direct", frame, None))
                continue
            branch_result = _Evaluation()
            tasks.append(("after_any", frame, branch_result))
            tasks.append(
                (
                    "start",
                    _EvaluationFrame(
                        branches[frame.any_index],
                        _mutable_copy(frame.value),
                        frame.pointer,
                        branch_result,
                        [],
                    ),
                    None,
                )
            )
        elif operation == "after_any":
            assert isinstance(payload, _Evaluation)
            assert payload.issues is not None
            if not payload.issues:
                frame.value = payload.value
                tasks.append(("direct", frame, None))
            else:
                frame.any_index += 1
                tasks.append(("try_any", frame, None))
        elif operation == "direct":
            _begin_direct_evaluation(frame, tasks)
        elif operation == "after_child":
            assert isinstance(payload, _ChildEvaluation)
            assert payload.result.issues is not None
            _write(payload.parent, payload.key, payload.result.value)
            frame.issues.extend(payload.result.issues)
        else:
            frame.result.value = frame.value
            frame.result.issues = frame.issues
    return root_result


def _raise_validation(tool: Tool, issues: list[_Issue]) -> None:
    issues.sort(key=lambda issue: (issue.pointer, _KEYWORD_ORDER[issue.keyword]))
    quoted_name = json.dumps(tool.name, ensure_ascii=False)
    lines = [f"Validation failed for tool {quoted_name}:"]
    lines.extend(
        f"  - {issue.pointer} [{issue.keyword}]: {_ISSUE_TEMPLATES[issue.keyword]}"
        for issue in issues
    )
    raise ValueError("\n".join(lines))


def validateToolArguments(tool: Tool, toolCall: ToolCall) -> dict[str, object]:
    if not isinstance(tool, Tool):
        raise TypeError("tool must be a Tool")
    if type(toolCall) is not ToolCall:
        raise TypeError("toolCall must be a ToolCall")
    if tool.name != toolCall.name:
        raise ValueError("Tool name does not match Tool Call name")
    evaluation = _evaluate(tool.parameters, toolCall.arguments)
    converted = evaluation.value
    if type(converted) is not dict:
        raise RuntimeError("admitted object schema did not produce an object")
    assert evaluation.issues is not None
    issues = evaluation.issues
    if issues:
        _raise_validation(tool, issues)
    return cast(dict[str, object], converted)


def validateToolCall(
    tools: Sequence[Tool], toolCall: ToolCall
) -> dict[str, object]:
    if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes, bytearray)):
        raise TypeError("tools must be a Sequence of Tool values")
    if type(toolCall) is not ToolCall:
        raise TypeError("toolCall must be a ToolCall")
    if any(not isinstance(tool, Tool) for tool in tools):
        raise TypeError("tools must contain only Tool values")
    matches = [tool for tool in tools if tool.name == toolCall.name]
    quoted_name = json.dumps(toolCall.name, ensure_ascii=False)
    if not matches:
        raise LookupError(f"No Tool found for name {quoted_name}")
    if len(matches) != 1:
        raise LookupError(f"Multiple Tools found for name {quoted_name}")
    return validateToolArguments(matches[0], toolCall)
