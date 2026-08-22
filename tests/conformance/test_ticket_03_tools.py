from __future__ import annotations

from dataclasses import FrozenInstanceError
import asyncio
import json
from types import MappingProxyType
import math
from pathlib import Path
from typing import Any, cast

import pytest

from oh_my_core import AgentContext, AgentTool, AgentToolResult
from oh_my_llm import (
    Context,
    TextContent,
    Tool,
    ToolCall,
    validateToolArguments,
    validateToolCall,
)


OBJECT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "limit": {"type": ["integer", "null"], "minimum": 0},
    },
    "required": ["path"],
    "additionalProperties": False,
}
ROOT = Path(__file__).parents[2]


def test_tool_is_an_immutable_owned_public_value() -> None:
    source = OBJECT_SCHEMA.copy()
    source["properties"] = dict(source["properties"])
    tool = Tool(name="read", description="Read text", parameters=source)
    source["properties"]["later"] = {"type": "string"}

    assert tool.name == "read"
    assert tool.description == "Read text"
    assert isinstance(tool.parameters, MappingProxyType)
    assert "later" not in cast(Any, tool.parameters["properties"])
    assert Context(messages=(), tools=[tool]).tools == (tool,)  # type: ignore[arg-type]
    with pytest.raises(FrozenInstanceError):
        tool.name = "write"  # type: ignore[misc]


@pytest.mark.parametrize(
    "parameters",
    (
        {},
        {"type": "string"},
        {"type": "object", "$schema": "http://json-schema.org/draft-07/schema#"},
        {"type": "object", "unknown": True},
        {"type": "object", "$ref": "#"},
        {"type": "object", "$defs": {}},
        {"type": "object", "oneOf": [{"type": "object"}]},
        {"type": "object", "if": {"type": "object"}},
        {"type": "object", "items": {"type": "string"}},
        {"type": "string", "properties": {}},
        {"type": "array", "items": [{"type": "string"}]},
        {"type": "object", "required": ["x", "x"]},
        {"type": "object", "properties": {"x": True}},
        {"type": "object", "anyOf": []},
        {"type": "object", "enum": []},
    ),
)
def test_tool_rejects_every_schema_outside_the_closed_subset(
    parameters: object,
) -> None:
    with pytest.raises((TypeError, ValueError), match=r"^Tool\.parameters"):
        Tool(name="bad", description="bad", parameters=parameters)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "pattern",
    (
        r"(?:src|tests)/[\w.-]+\.py$",
        r"^[^\r\n]+$",
        r"café.\s\d\w",
        r"a{1,3}|b+|c?",
    ),
)
def test_pattern_subset_admits_portable_linear_time_syntax(pattern: str) -> None:
    Tool(
        name="match",
        description="match",
        parameters=cast(Any, {
            "type": "object",
            "properties": {"value": {"type": "string", "pattern": pattern}},
        }),
    )


def test_pattern_control_tokens_are_only_special_outside_escapes_and_classes() -> None:
    for pattern in (r"\(\?i", r"[(?i]", r"\$", r"[.]"):
        Tool(
            name="literal",
            description="literal",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string", "pattern": pattern}},
            },
        )


@pytest.mark.parametrize(
    "pattern",
    (
        r"(a)\1",
        r"a(?=b)",
        r"(?P<name>a)",
        r"(?<name>a)",
        r"(?i:a)",
        r"\p{L}",
        r"(?>a)",
        r"a++",
        r"[",
    ),
)
def test_pattern_subset_rejects_nonportable_or_backtracking_syntax(
    pattern: str,
) -> None:
    with pytest.raises(ValueError, match=r"^Tool\.parameters"):
        Tool(
            name="bad",
            description="bad",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string", "pattern": pattern}},
            },
        )


def test_validation_converts_recursively_into_fresh_mutable_working_trees() -> None:
    tool = Tool(
        name="convert",
        description="convert",
        parameters=cast(Any, {
            "type": "object",
            "properties": {
                "number": {"type": "number"},
                "integer": {"type": "integer"},
                "boolean": {"type": "boolean"},
                "string": {"type": "string"},
                "null": {"type": "null"},
                "nested": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": {"type": "integer"},
                    },
                },
            },
            "required": ["number", "integer", "boolean", "string", "null"],
        }),
    )
    call = ToolCall(
        id="call",
        name="convert",
        arguments=cast(Any, {
            "number": "1.5",
            "integer": "2",
            "boolean": "false",
            "string": True,
            "null": 0,
            "nested": [{"value": "3"}],
            "untouched": {"value": [1]},
        }),
    )

    first = validateToolArguments(tool, call)
    second = validateToolCall([tool], call)

    assert first == {
        "number": 1.5,
        "integer": 2,
        "boolean": False,
        "string": "true",
        "null": None,
        "nested": [{"value": 3}],
        "untouched": {"value": [1]},
    }
    assert type(first["number"]) is float
    assert type(first["integer"]) is int
    assert first is not second
    assert first["nested"] is not second["nested"]
    cast(dict[str, Any], cast(list[Any], first["nested"])[0])["value"] = 4
    assert cast(Any, cast(Any, call.arguments["nested"])[0])["value"] == "3"
    assert cast(Any, cast(list[Any], second["nested"])[0])["value"] == 3


def test_boolean_numeric_and_mathematical_equality_rules_remain_distinct() -> None:
    tool = Tool(
        name="numbers",
        description="numbers",
        parameters=cast(Any, {
            "type": "object",
            "properties": {
                "integralFloat": {"type": "integer"},
                "numberInt": {"type": "number"},
                "enumFloat": {"enum": [1.0]},
                "constZero": {"const": -0.0},
                "notBool": {"enum": [1]},
            },
            "required": [
                "integralFloat",
                "numberInt",
                "enumFloat",
                "constZero",
                "notBool",
            ],
        }),
    )
    valid = validateToolArguments(
        tool,
        ToolCall(
            id="call",
            name="numbers",
            arguments={
                "integralFloat": 1.0,
                "numberInt": 1,
                "enumFloat": 1,
                "constZero": 0,
                "notBool": 1.0,
            },
        ),
    )
    assert type(valid["integralFloat"]) is float
    assert type(valid["numberInt"]) is int
    assert math.copysign(1.0, valid["integralFloat"]) == 1.0

    with pytest.raises(ValueError, match=r"\[enum\]"):
        validateToolArguments(
            tool,
            ToolCall(
                id="call",
                name="numbers",
                arguments={
                    "integralFloat": 1.0,
                    "numberInt": 1,
                    "enumFloat": 1,
                    "constZero": 0,
                    "notBool": True,
                },
            ),
        )


@pytest.mark.parametrize(
    ("schema_type", "source", "expected", "expected_type"),
    (
        ("number", None, 0.0, float),
        ("number", True, 1.0, float),
        ("number", " 2.5 ", 2.5, float),
        ("integer", None, 0, int),
        ("integer", False, 0, int),
        ("integer", "2", 2, int),
        ("boolean", None, False, bool),
        ("boolean", "true", True, bool),
        ("boolean", "false", False, bool),
        ("boolean", 1, True, bool),
        ("boolean", 0.0, False, bool),
        ("string", None, "", str),
        ("string", True, "true", str),
        ("string", 2, "2", str),
        ("string", 2.5, "2.5", str),
        ("string", -0.0, "0", str),
        ("null", "", None, type(None)),
        ("null", 0, None, type(None)),
        ("null", 0.0, None, type(None)),
        ("null", False, None, type(None)),
    ),
)
def test_every_selected_primitive_conversion(
    schema_type: str,
    source: object,
    expected: object,
    expected_type: type[object],
) -> None:
    tool = Tool(
        name="primitive",
        description="primitive",
        parameters={
            "type": "object",
            "properties": {"value": {"type": schema_type}},
            "required": ("value",),
        },
    )
    working = validateToolArguments(
        tool,
        ToolCall(
            id="call", name="primitive", arguments=cast(Any, {"value": source})
        ),
    )
    assert working["value"] == expected
    assert type(working["value"]) is expected_type


@pytest.mark.parametrize(
    ("schema", "value", "keyword"),
    (
        ({"type": "array", "minItems": 2}, [1], "minItems"),
        ({"type": "array", "maxItems": 1}, [1, 2], "maxItems"),
        ({"type": "string", "maxLength": 1}, "xx", "maxLength"),
        ({"type": "number", "maximum": 1}, 2, "maximum"),
        ({"type": "number", "exclusiveMaximum": 1}, 1, "exclusiveMaximum"),
        ({"type": "integer", "exclusiveMinimum": 1}, 1, "exclusiveMinimum"),
        ({"type": "string"}, [], "type"),
        ({"const": {"fixed": True}}, {"fixed": False}, "const"),
    ),
)
def test_every_selected_constraint_reports_its_keyword(
    schema: dict[str, Any], value: object, keyword: str
) -> None:
    tool = Tool(
        name="constraint",
        description="constraint",
        parameters=cast(Any, {
            "type": "object",
            "description": "all selected keywords are admitted",
            "properties": {"value": schema},
            "required": ("value",),
        }),
    )
    with pytest.raises(ValueError, match=rf"#/value \[{keyword}\]"):
        validateToolArguments(
            tool,
            ToolCall(
                id="call", name="constraint", arguments=cast(Any, {"value": value})
            ),
        )


@pytest.mark.parametrize(
    ("schema_type", "source"),
    (
        ("number", " "),
        ("integer", "1.5"),
        ("boolean", "yes"),
        ("object", "{}"),
        ("array", "[]"),
    ),
)
def test_unlisted_conversions_are_rejected(schema_type: str, source: object) -> None:
    tool = Tool(
        name="no-convert",
        description="no-convert",
        parameters={
            "type": "object",
            "properties": {"value": {"type": schema_type}},
            "required": ("value",),
        },
    )
    with pytest.raises(ValueError, match=r"#/value \[type\]"):
        validateToolArguments(
            tool,
            ToolCall(
                id="call", name="no-convert", arguments=cast(Any, {"value": source})
            ),
        )


def test_any_of_selects_first_independently_converted_valid_branch() -> None:
    tool = Tool(
        name="union",
        description="union",
        parameters=cast(Any, {
            "type": "object",
            "properties": {
                "value": {
                    "anyOf": [
                        {"type": "integer", "minimum": 2},
                        {"type": "number", "minimum": 1},
                    ]
                }
            },
            "required": ["value"],
        }),
    )
    integer = validateToolArguments(
        tool, ToolCall(id="a", name="union", arguments={"value": "2"})
    )
    number = validateToolArguments(
        tool, ToolCall(id="b", name="union", arguments={"value": "1.5"})
    )
    assert integer["value"] == 2 and type(integer["value"]) is int
    assert number["value"] == 1.5 and type(number["value"]) is float


def test_patterns_use_ascii_shorthand_ecmascript_dot_anchor_and_search() -> None:
    def pattern_tool(pattern: str) -> Tool:
        return Tool(
            name="pattern",
            description="pattern",
            parameters=cast(Any, {
                "type": "object",
                "properties": {"value": {"type": "string", "pattern": pattern}},
                "required": ["value"],
            }),
        )

    for pattern, admitted, rejected in (
        (r"\w+", "xx a_9 yy", "é"),
        (r".", "é", "\n"),
        (r".", "x", "\r"),
        (r"^b", "before", "a\nb"),
        (r"b$", "ab\n", "ab\nx"),
        (r"needle", "hay needle stack", "haystack"),
    ):
        tool = pattern_tool(pattern)
        validateToolArguments(
            tool, ToolCall(id="ok", name="pattern", arguments={"value": admitted})
        )
        with pytest.raises(ValueError, match=r"\[pattern\]"):
            validateToolArguments(
                tool,
                ToolCall(id="bad", name="pattern", arguments={"value": rejected}),
            )


def test_validation_failure_is_deterministic_aggregate_and_fully_redacted() -> None:
    secret_value = "ARGUMENT_SECRET_7a5f"
    secret_schema = "SCHEMA_SECRET_28cb"
    tool = Tool(
        name='bad"\nname',
        description="validation",
        parameters=cast(Any, {
            "type": "object",
            "title": secret_schema,
            "properties": {
                "a/b~c": {
                    "type": "string",
                    "minLength": 3,
                    "maxLength": 1,
                    "pattern": "^safe$",
                },
                "choice": {
                    "anyOf": [
                        {"type": "integer", "minimum": 10},
                        {"type": "boolean"},
                    ]
                },
                "number": {
                    "type": "number",
                    "minimum": 3,
                    "exclusiveMinimum": 4,
                },
            },
            "required": ["missing"],
            "additionalProperties": False,
        }),
    )
    call = ToolCall(
        id="call",
        name='bad"\nname',
        arguments={
            "a/b~c": "x",
            "choice": secret_value,
            "number": 2,
            "extra": secret_value,
        },
    )

    with pytest.raises(ValueError) as first_error:
        validateToolArguments(tool, call)
    with pytest.raises(ValueError) as second_error:
        validateToolArguments(tool, call)

    expected = "\n".join(
        (
            f"Validation failed for tool {json.dumps(tool.name, ensure_ascii=False)}:",
            "  - #/a~1b~0c [minLength]: must contain at least the declared number of characters",
            "  - #/a~1b~0c [pattern]: must match the declared pattern",
            "  - #/choice [anyOf]: must match at least one branch",
            "  - #/extra [additionalProperties]: additional property is not admitted",
            "  - #/missing [required]: required property is missing",
            "  - #/number [minimum]: must be at least the declared bound",
            "  - #/number [exclusiveMinimum]: must be greater than the declared bound",
        )
    )
    assert str(first_error.value) == expected
    assert str(second_error.value) == expected
    assert secret_value not in expected
    assert secret_schema not in expected
    assert repr(call.arguments) not in expected
    assert repr(tool.parameters) not in expected


def test_lookup_name_agreement_and_carrier_failures_have_selected_classes() -> None:
    tool = Tool(
        name="read",
        description="read",
        parameters={"type": "object"},
    )
    call = ToolCall(id="call", name="read", arguments={})

    with pytest.raises(LookupError, match=r'^No Tool found for name "read"$'):
        validateToolCall([], call)
    with pytest.raises(LookupError, match=r'^Multiple Tools found for name "read"$'):
        validateToolCall([tool, tool], call)
    with pytest.raises(ValueError, match="^Tool name does not match Tool Call name$"):
        validateToolArguments(
            Tool(name="write", description="write", parameters={"type": "object"}),
            call,
        )
    with pytest.raises(TypeError):
        validateToolCall(cast(Any, [object()]), call)
    with pytest.raises(TypeError):
        validateToolCall([tool], cast(Any, {}))
    with pytest.raises(TypeError):
        validateToolArguments(cast(Any, {}), call)


def test_deep_schema_and_arguments_do_not_leak_python_recursion_limits() -> None:
    depth = 1200
    schema: dict[str, Any] = {"type": "string", "const": "leaf"}
    arguments: object = "leaf"
    for _ in range(depth):
        schema = {
            "type": "object",
            "properties": {"next": schema},
            "required": ["next"],
            "additionalProperties": False,
        }
        arguments = {"next": arguments}
    tool = Tool(name="deep", description="deep", parameters=schema)
    call = ToolCall(id="call", name="deep", arguments=cast(Any, arguments))

    working: object = validateToolArguments(tool, call)
    for _ in range(depth):
        working = cast(dict[str, object], working)["next"]
    assert working == "leaf"


def test_deep_any_of_conversion_is_iterative() -> None:
    depth = 1100
    schema: dict[str, Any] = {"const": "leaf"}
    arguments: object = "leaf"
    for _ in range(depth):
        schema = {
            "anyOf": (
                {
                    "type": "object",
                    "properties": {"next": schema},
                    "required": ("next",),
                },
            )
        }
        arguments = {"next": arguments}
    tool = Tool(
        name="deep-union",
        description="deep-union",
        parameters={
            "type": "object",
            "properties": {"value": schema},
            "required": ("value",),
        },
    )
    call = ToolCall(
        id="call", name="deep-union", arguments=cast(Any, {"value": arguments})
    )

    working: object = validateToolArguments(tool, call)["value"]
    for _ in range(depth):
        working = cast(dict[str, object], working)["next"]
    assert working == "leaf"


def test_agent_tool_carries_strict_four_argument_async_protocol() -> None:
    raw_seen: list[dict[str, object]] = []
    calls: list[tuple[object, ...]] = []
    updates: list[AgentToolResult] = []
    class ActiveSignal:
        @property
        def aborted(self) -> bool:
            return False

        async def wait(self) -> None:
            await asyncio.Future[None]()

    signal = ActiveSignal()

    def prepare(arguments: dict[str, object]) -> dict[str, object]:
        raw_seen.append(arguments)
        arguments["prepared"] = "4"
        return arguments

    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        active_signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        calls.append((tool_call_id, params, active_signal, on_update))
        partial = AgentToolResult(
            content=(TextContent(text="working"),),
            details={"phase": 1},
        )
        assert on_update(partial) is None
        return AgentToolResult(
            content=(TextContent(text="done"),),
            details={"ok": True},
            terminate=True,
        )

    tool = AgentTool(
        name="strict",
        label="Strict Tool",
        description="strict",
        parameters=cast(Any, {
            "type": "object",
            "properties": {"prepared": {"type": "integer"}},
            "required": ["prepared"],
        }),
        prepareArguments=prepare,
        execute=execute,
        executionMode="sequential",
    )
    raw: dict[str, object] = {"input": 1}
    assert tool.prepareArguments is not None
    prepared = tool.prepareArguments(raw.copy())
    params = validateToolArguments(
        tool,
        ToolCall(id="call-1", name="strict", arguments=cast(Any, prepared)),
    )
    async def invoke() -> AgentToolResult:
        return await tool.execute("call-1", params, signal, updates.append)

    result = asyncio.run(invoke())

    assert isinstance(tool, Tool)
    assert tool.label == "Strict Tool"
    assert tool.executionMode == "sequential"
    assert raw_seen[0] is not raw
    assert calls == [("call-1", params, signal, updates.append)]
    assert [item.content[0].text for item in updates] == ["working"]
    assert result.content[0].text == "done"
    assert result.terminate is True
    assert not hasattr(result, "isError")
    assert AgentContext(systemPrompt="", messages=(), tools=[tool]).tools == (tool,)  # type: ignore[arg-type]


def test_agent_tool_result_is_text_only_owned_and_has_identity_free_value_semantics() -> None:
    details = {"nested": [1]}
    result = AgentToolResult(
        content=[TextContent(text="ok")],  # type: ignore[arg-type]
        details=cast(Any, details),
    )
    equal = AgentToolResult(
        content=(TextContent(text="ok"),), details=cast(Any, {"nested": [1]})
    )
    details["nested"].append(2)

    assert result == equal
    assert hash(result) == hash(equal)
    assert cast(Any, result.details)["nested"] == (1,)
    assert result.terminate is None
    with pytest.raises(FrozenInstanceError):
        result.terminate = True  # type: ignore[misc]
    with pytest.raises(TypeError, match=r"^AgentToolResult\.content\[0\]:"):
        AgentToolResult(content=cast(Any, [object()]), details=None)
    with pytest.raises(TypeError, match=r"^AgentToolResult\.terminate:"):
        AgentToolResult(content=(), details=None, terminate=1)  # type: ignore[arg-type]


def test_ticket_03_conformance_authorities_are_linked_to_public_observations() -> None:
    matrix = json.loads((ROOT / "conformance/obligation-matrix.json").read_text())
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    required = {
        "omh-v0.tool-schema-validation": "reference.tool-schema-validation",
        "omh-v0.tool-callable-values": "reference.tool-callable-values",
        "omh-v0.strict-python-tool-callable": "reference.strict-python-tool-callable",
        "omh-v0.redacted-tool-validation-errors": "reference.redacted-tool-validation-errors",
    }
    rows = {row["id"]: row for row in matrix["obligations"]}
    cases = {case["id"]: case for case in corpus["cases"]}
    assert required.keys() <= rows.keys()
    assert set(required.values()) <= cases.keys()
    for obligation, corpus_case in required.items():
        assert rows[obligation]["corpusCase"] == corpus_case
        assert rows[obligation]["executableCases"] == [
            "ticket-03-tools",
            "ticket-03-installed",
        ]
        assert cases[corpus_case]["obligation"] == obligation
