from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, fields, is_dataclass
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, cast, get_args

import pytest

import oh_my_llm
from oh_my_llm import (
    AssistantMessage,
    AssistantMessageDoneEvent,
    AssistantMessageErrorEvent,
    AssistantMessageEvent,
    AssistantMessageStartEvent,
    AssistantMessageTextDeltaEvent,
    AssistantMessageTextEndEvent,
    AssistantMessageTextStartEvent,
    AssistantMessageToolCallDeltaEvent,
    AssistantMessageToolCallEndEvent,
    AssistantMessageToolCallStartEvent,
    Context,
    JSONValue,
    Message,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
    createModels,
    fauxAssistantMessage,
    fauxProvider,
    fauxToolCall,
)
from oh_my_llm._canonical import decodeCanonical, encodeCanonical
from oh_my_llm._values import _AssistantMessageEventValidator


SAFE_INTEGER = 2**53 - 1
ROOT = Path(__file__).parents[2]


def _usage(*, cache_write: int = 0, cache_write_1h: int | None = None) -> Usage:
    return Usage(
        input=0,
        output=0,
        cacheRead=0,
        cacheWrite=cache_write,
        totalTokens=0,
        cacheWrite1h=cache_write_1h,
        cost=UsageCost(
            input=0.0,
            output=0.0,
            cacheRead=0.0,
            cacheWrite=0.0,
            total=0.0,
        ),
    )


def _assistant(
    content: object = (),
    *,
    stop_reason: str = "stop",
    error_message: str | None = None,
) -> AssistantMessage:
    return AssistantMessage(
        content=content,  # type: ignore[arg-type]
        api="faux",
        provider="faux",
        model="faux-1",
        usage=_usage(),
        stopReason=stop_reason,  # type: ignore[arg-type]
        timestamp=0,
        errorMessage=error_message,
    )


def test_public_message_value_surface_is_closed_and_nominal() -> None:
    public_records = (
        TextContent,
        ToolCall,
        UserMessage,
        AssistantMessage,
        ToolResultMessage,
        Usage,
        UsageCost,
        Context,
        AssistantMessageStartEvent,
        AssistantMessageTextStartEvent,
        AssistantMessageTextDeltaEvent,
        AssistantMessageTextEndEvent,
        AssistantMessageToolCallStartEvent,
        AssistantMessageToolCallDeltaEvent,
        AssistantMessageToolCallEndEvent,
        AssistantMessageDoneEvent,
        AssistantMessageErrorEvent,
    )
    for record in public_records:
        assert is_dataclass(record)
        assert getattr(record, "__dataclass_params__").frozen
        assert record.__slots__
        assert all(field.kw_only or not field.init for field in fields(record))

    expected = {
        "JSONValue",
        "TextContent",
        "ToolCall",
        "UserMessage",
        "AssistantMessage",
        "ToolResultMessage",
        "Message",
        "Usage",
        "UsageCost",
        "Context",
        "AssistantMessageEvent",
        "AssistantMessageStartEvent",
        "AssistantMessageTextStartEvent",
        "AssistantMessageTextDeltaEvent",
        "AssistantMessageTextEndEvent",
        "AssistantMessageToolCallStartEvent",
        "AssistantMessageToolCallDeltaEvent",
        "AssistantMessageToolCallEndEvent",
        "AssistantMessageDoneEvent",
        "AssistantMessageErrorEvent",
    }
    assert expected <= set(oh_my_llm.__all__)
    assert not hasattr(oh_my_llm, "encodeCanonical")
    assert not hasattr(oh_my_llm, "decodeCanonical")
    assert not hasattr(oh_my_llm, "ContentBlock")
    assert set(get_args(Message)) == {UserMessage, AssistantMessage, ToolResultMessage}
    assert len(get_args(AssistantMessageEvent)) == 9


def test_records_snapshot_nested_containers_and_are_frozen() -> None:
    source: dict[str, Any] = {"nested": [{"value": 1}, -0.0]}
    call = ToolCall(id="call", name="read", arguments=source)
    source["nested"][0]["value"] = 2
    source["nested"].append("later")

    nested = cast(Any, call.arguments["nested"])
    assert isinstance(call.arguments, MappingProxyType)
    assert nested[0]["value"] == 1
    assert len(nested) == 2
    with pytest.raises(TypeError):
        call.arguments["new"] = None  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        call.name = "write"  # type: ignore[misc]

    text = TextContent(text="hello")
    user_source = [text]
    user = UserMessage(content=user_source, timestamp=0)  # type: ignore[arg-type]
    user_source.clear()
    assert user.content == (text,)
    assert user.content[0] is text


def test_json_value_domain_accepts_boundaries_and_rejects_unsafe_carriers() -> None:
    valid_arguments: Any = {
        "null": None,
        "false": False,
        "text": "",
        "minimum": -SAFE_INTEGER,
        "maximum": SAFE_INTEGER,
        "float": 1.0,
        "negativeZero": -0.0,
        "array": [True, {"nested": "ok"}],
    }
    call = ToolCall(
        id="",
        name="",
        arguments=valid_arguments,
    )
    assert math.copysign(1.0, call.arguments["negativeZero"]) == -1.0  # type: ignore[arg-type]

    rejected: tuple[tuple[object, type[Exception], str], ...] = (
        ({"value": object()}, TypeError, 'ToolCall.arguments["value"]: '),
        ({1: "value"}, TypeError, "ToolCall.arguments: "),
        ({"value": b"bytes"}, TypeError, 'ToolCall.arguments["value"]: '),
        ({"value": SAFE_INTEGER + 1}, ValueError, 'ToolCall.arguments["value"]: '),
        ({"value": float("nan")}, ValueError, 'ToolCall.arguments["value"]: '),
        ({"value": float("inf")}, ValueError, 'ToolCall.arguments["value"]: '),
        ({"value": "\ud800"}, ValueError, 'ToolCall.arguments["value"]: '),
    )
    for arguments, error_type, prefix in rejected:
        with pytest.raises(error_type, match=f"^{re.escape(prefix)}"):
            ToolCall(id="call", name="tool", arguments=arguments)  # type: ignore[arg-type]

    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match=r'^ToolCall.arguments\["cycle"\]\[0\]: '):
        ToolCall(id="call", name="tool", arguments=cast(Any, {"cycle": cyclic}))

    ordered_failures: dict[Any, Any] = {"first": object(), 1: "later"}
    with pytest.raises(TypeError, match=r'^ToolCall.arguments\["first"\]: '):
        ToolCall(id="call", name="tool", arguments=cast(Any, ordered_failures))


def test_record_validation_distinguishes_carrier_and_invariant_failures() -> None:
    with pytest.raises(TypeError, match=r"^TextContent.text: "):
        TextContent(text=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match=r"^Usage.input: "):
        Usage(
            input=cast(Any, True),
            output=0,
            cacheRead=0,
            cacheWrite=0,
            totalTokens=0,
            cost=_usage().cost,
        )
    with pytest.raises(ValueError, match=r"^Usage.input: "):
        Usage(
            input=-1,
            output=0,
            cacheRead=0,
            cacheWrite=0,
            totalTokens=0,
            cost=_usage().cost,
        )
    with pytest.raises(ValueError, match=r"^Usage.cacheWrite1h: "):
        _usage(cache_write=1, cache_write_1h=2)
    with pytest.raises(TypeError, match=r"^Usage.cost: "):
        Usage(
            input=0,
            output=0,
            cacheRead=0,
            cacheWrite=0,
            totalTokens=0,
            cost=None,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match=r"^AssistantMessage.stopReason: "):
        _assistant(stop_reason="unknown")
    with pytest.raises(ValueError, match=r"^AssistantMessage.errorMessage: "):
        _assistant(stop_reason="error")
    with pytest.raises(ValueError, match=r"^AssistantMessage.errorMessage: "):
        _assistant(error_message="unexpected")
    with pytest.raises(TypeError, match=r"^ToolResultMessage.isError: "):
        ToolResultMessage(
            toolCallId="call",
            toolName="tool",
            content=(),
            details=None,
            isError=1,  # type: ignore[arg-type]
            timestamp=0,
        )
    with pytest.raises(TypeError, match=r"^Usage.cacheWrite1h: "):
        Usage(
            input=0,
            output=0,
            cacheRead=0,
            cacheWrite=0,
            totalTokens=0,
            cacheWrite1h="0",  # type: ignore[arg-type]
            cost=_usage().cost,
        )
    with pytest.raises(TypeError, match=r"^Usage.output: "):
        Usage(input=0, cacheRead=0, cacheWrite=0, totalTokens=0, cost=_usage().cost)  # type: ignore[call-arg]
    with pytest.raises(TypeError, match=r"^Usage.extra: "):
        Usage(
            input=0,
            output=0,
            cacheRead=0,
            cacheWrite=0,
            totalTokens=0,
            cost=_usage().cost,
            extra=0,  # type: ignore[call-arg]
        )


def test_record_equality_and_hash_preserve_carriers_and_mapping_semantics() -> None:
    left = ToolCall(id="call", name="tool", arguments={"a": 1, "b": (0.0,)})
    reordered = ToolCall(id="call", name="tool", arguments={"b": (0.0,), "a": 1})
    float_one = ToolCall(id="call", name="tool", arguments={"a": 1.0, "b": (0.0,)})
    negative_zero = ToolCall(id="call", name="tool", arguments={"a": 1, "b": (-0.0,)})

    assert left == reordered
    assert hash(left) == hash(reordered)
    assert left != float_one
    assert left != negative_zero
    assert len({left, reordered, float_one, negative_zero}) == 3
    assert TextContent(text="same") != UserMessage(content="same", timestamp=0)


def test_message_fields_preserve_absence_null_and_empty_values() -> None:
    text = TextContent(text="", textSignature="")
    call = ToolCall(id="", name="", arguments={}, thoughtSignature="")
    assistant = _assistant((text, call))
    user_text = UserMessage(content="", timestamp=-SAFE_INTEGER)
    user_blocks = UserMessage(content=[text], timestamp=SAFE_INTEGER)  # type: ignore[arg-type]
    tool_result = ToolResultMessage(
        toolCallId="",
        toolName="",
        content=[],  # type: ignore[arg-type]
        details=None,
        isError=False,
        timestamp=0,
    )
    context_missing = Context(messages=())
    context_empty = Context(
        messages=cast(Any, [user_text, user_blocks, assistant, tool_result]),
        systemPrompt="",
        tools=cast(Any, []),
    )

    assert assistant.content == (text, call)
    assert user_text.content == ""
    assert user_blocks.content == (text,)
    assert tool_result.details is None
    assert context_missing.systemPrompt is None and context_missing.tools is None
    assert context_empty.systemPrompt == "" and context_empty.tools == ()


@pytest.mark.parametrize(
    ("reason", "error_message"),
    (
        ("stop", None),
        ("length", None),
        ("toolUse", None),
        ("error", "failed"),
        ("aborted", "cancelled"),
    ),
)
def test_all_stop_reasons_preserve_their_error_correlation(
    reason: str, error_message: str | None
) -> None:
    message = _assistant(stop_reason=reason, error_message=error_message)
    assert message.stopReason == reason
    assert message.errorMessage == error_message


def test_assistant_message_events_enforce_cumulative_lifecycle() -> None:
    empty = _assistant()
    text_start_partial = _assistant((TextContent(text=""),))
    text_delta_partial = _assistant((TextContent(text="hello"),))
    call = ToolCall(id="call", name="read", arguments={"path": "README.md"})
    tool_start_partial = _assistant((TextContent(text="hello"), call))
    final = _assistant((TextContent(text="hello"), call), stop_reason="toolUse")

    events: tuple[AssistantMessageEvent, ...] = (
        AssistantMessageStartEvent(partial=empty),
        AssistantMessageTextStartEvent(contentIndex=0, partial=text_start_partial),
        AssistantMessageTextDeltaEvent(
            contentIndex=0,
            delta="hello",
            partial=text_delta_partial,
        ),
        AssistantMessageTextEndEvent(
            contentIndex=0,
            content="hello",
            partial=text_delta_partial,
        ),
        AssistantMessageToolCallStartEvent(contentIndex=1, partial=tool_start_partial),
        AssistantMessageToolCallDeltaEvent(
            contentIndex=1,
            delta='',
            partial=tool_start_partial,
        ),
        AssistantMessageToolCallEndEvent(
            contentIndex=1,
            toolCall=call,
            partial=tool_start_partial,
        ),
        AssistantMessageDoneEvent(reason="toolUse", message=final),
    )
    validator = _AssistantMessageEventValidator()
    for event in events:
        validator.accept(event)
    assert validator.result is final

    with pytest.raises(ValueError, match=r"^AssistantMessageEvent.type: "):
        validator.accept(AssistantMessageDoneEvent(reason="toolUse", message=final))

    invalid = _AssistantMessageEventValidator()
    invalid.accept(AssistantMessageStartEvent(partial=empty))
    invalid.accept(
        AssistantMessageTextStartEvent(contentIndex=0, partial=text_start_partial)
    )
    with pytest.raises(ValueError, match=r"^AssistantMessageEvent.partial.content\[0\]: "):
        invalid.accept(
            AssistantMessageTextDeltaEvent(
                contentIndex=0,
                delta="x",
                partial=_assistant((TextContent(text="not x"),)),
            )
        )

    failure = _assistant(stop_reason="error", error_message="provider failed")
    pre_start_error = _AssistantMessageEventValidator()
    error_event = AssistantMessageErrorEvent(reason="error", error=failure)
    pre_start_error.accept(error_event)
    assert pre_start_error.result is failure


def test_content_start_cannot_smuggle_an_unopened_later_index() -> None:
    validator = _AssistantMessageEventValidator()
    validator.accept(AssistantMessageStartEvent(partial=_assistant()))
    smuggled = _assistant((TextContent(text=""), TextContent(text="later")))
    with pytest.raises(ValueError, match=r"^AssistantMessageEvent.partial.content\[1\]: "):
        validator.accept(
            AssistantMessageTextStartEvent(contentIndex=0, partial=smuggled)
        )


def test_event_constructors_reject_local_inconsistency() -> None:
    empty = _assistant()
    with pytest.raises(ValueError, match=r"^AssistantMessageStartEvent.partial.content: "):
        AssistantMessageStartEvent(partial=_assistant((TextContent(text="x"),)))
    with pytest.raises(ValueError, match=r"^AssistantMessageTextStartEvent.contentIndex: "):
        AssistantMessageTextStartEvent(contentIndex=-1, partial=empty)
    with pytest.raises(ValueError, match=r"^AssistantMessageDoneEvent.reason: "):
        AssistantMessageDoneEvent(reason="length", message=empty)
    failure = _assistant(stop_reason="aborted", error_message="cancelled")
    with pytest.raises(ValueError, match=r"^AssistantMessageErrorEvent.reason: "):
        AssistantMessageErrorEvent(reason="error", error=failure)


def test_tool_call_delta_finalization_uses_strict_json_without_requiring_canonical_bytes() -> None:
    empty_call = ToolCall(id="call", name="read", arguments={})
    final_call = ToolCall(id="call", name="read", arguments={"path": "README.md"})
    empty = _assistant()
    started = _assistant((empty_call,))
    refined = _assistant((final_call,))
    final = _assistant((final_call,), stop_reason="toolUse")
    events: tuple[AssistantMessageEvent, ...] = (
        AssistantMessageStartEvent(partial=empty),
        AssistantMessageToolCallStartEvent(contentIndex=0, partial=started),
        AssistantMessageToolCallDeltaEvent(
            contentIndex=0,
            delta=' { "path" : "README.md" } ',
            partial=refined,
        ),
        AssistantMessageToolCallEndEvent(
            contentIndex=0,
            toolCall=final_call,
            partial=refined,
        ),
        AssistantMessageDoneEvent(reason="toolUse", message=final),
    )
    validator = _AssistantMessageEventValidator()
    for event in events:
        validator.accept(event)
    assert validator.result is final


def test_tool_call_delta_partial_arguments_follow_only_complete_object_prefixes() -> None:
    empty_call = ToolCall(id="call", name="read", arguments={})
    wrong_call = ToolCall(id="call", name="read", arguments={"wrong": 1})
    validator = _AssistantMessageEventValidator()
    validator.accept(AssistantMessageStartEvent(partial=_assistant()))
    validator.accept(
        AssistantMessageToolCallStartEvent(
            contentIndex=0,
            partial=_assistant((empty_call,)),
        )
    )
    with pytest.raises(ValueError, match=r"^AssistantMessageEvent.partial.content\[0\]: "):
        validator.accept(
            AssistantMessageToolCallDeltaEvent(
                contentIndex=0,
                delta="{}",
                partial=_assistant((wrong_call,)),
            )
        )

    incomplete = _AssistantMessageEventValidator()
    incomplete.accept(AssistantMessageStartEvent(partial=_assistant()))
    incomplete.accept(
        AssistantMessageToolCallStartEvent(
            contentIndex=0,
            partial=_assistant((empty_call,)),
        )
    )
    with pytest.raises(ValueError, match=r"^AssistantMessageEvent.partial.content\[0\]: "):
        incomplete.accept(
            AssistantMessageToolCallDeltaEvent(
                contentIndex=0,
                delta='{',
                partial=_assistant((wrong_call,)),
            )
        )


def test_public_faux_streams_structured_zero_delta_tool_call_values() -> None:
    async def collect() -> tuple[AssistantMessageEvent, ...]:
        faux = fauxProvider()
        models = createModels()
        models.setProvider(faux.provider)
        model = faux.getModel()
        assert model is not None
        call = fauxToolCall(
            id="call",
            name="read",
            arguments={"path": "README.md"},
            thoughtSignature="opaque",
        )
        faux.setResponses((fauxAssistantMessage(call, stopReason="toolUse"),))
        return tuple(
            [event async for event in models.streamSimple(model, Context(messages=()))]
        )

    events = asyncio.run(collect())
    assert tuple(event.type for event in events) == (
        "start",
        "toolcall_start",
        "toolcall_end",
        "done",
    )
    assert isinstance(events[-1], AssistantMessageDoneEvent)
    assert isinstance(events[-2], AssistantMessageToolCallEndEvent)
    assert events[-1].message.content[0] is events[-2].toolCall


def test_private_canonical_codec_has_type_preserving_golden_bytes() -> None:
    value = ToolCall(
        id="call",
        name="tool",
        arguments={"z": -0.0, "a": 1, "b": 1.0, "emoji": "😀", "line": "\n"},
    )
    expected = (
        b'{"arguments":{"a":1,"b":1.0,"emoji":"\xf0\x9f\x98\x80",'
        b'"line":"\\n","z":-0.0},"id":"call","name":"tool","type":"toolCall"}'
    )
    encoded = encodeCanonical(value)
    assert encoded == expected
    decoded = decodeCanonical(encoded, ToolCall)
    assert type(decoded) is ToolCall
    assert decoded == value
    assert encodeCanonical(decoded) == encoded

    context = Context(messages=(), systemPrompt="", tools=())
    assert encodeCanonical(context) == b'{"messages":[],"systemPrompt":"","tools":[]}'
    missing = Context(messages=())
    assert encodeCanonical(missing) == b'{"messages":[]}'
    assert decodeCanonical(encodeCanonical(missing), Context) == missing


def test_private_canonical_codec_round_trips_every_completed_record_and_union() -> None:
    text = TextContent(text="hello", textSignature="sig")
    call = ToolCall(
        id="call",
        name="read",
        arguments={"path": "README.md"},
        thoughtSignature="thought",
    )
    usage = _usage(cache_write=1, cache_write_1h=1)
    user = UserMessage(content=(text,), timestamp=1)
    assistant = AssistantMessage(
        content=(text, call),
        api="faux",
        provider="faux",
        model="faux-1",
        responseModel="response-model",
        responseId="response-id",
        usage=usage,
        stopReason="toolUse",
        timestamp=2,
    )
    tool_result = ToolResultMessage(
        toolCallId="call",
        toolName="read",
        content=(text,),
        details={"bytes": 1},
        isError=False,
        timestamp=3,
    )
    context = Context(messages=(user, assistant, tool_result), systemPrompt="system", tools=())
    partial0 = _assistant()
    partial_text0 = _assistant((TextContent(text=""),))
    partial_text = _assistant((text,))
    partial_tool = _assistant((text, call))
    failure = _assistant(stop_reason="error", error_message="failed")
    events: tuple[AssistantMessageEvent, ...] = (
        AssistantMessageStartEvent(partial=partial0),
        AssistantMessageTextStartEvent(contentIndex=0, partial=partial_text0),
        AssistantMessageTextDeltaEvent(contentIndex=0, delta="hello", partial=partial_text),
        AssistantMessageTextEndEvent(contentIndex=0, content="hello", partial=partial_text),
        AssistantMessageToolCallStartEvent(contentIndex=1, partial=partial_tool),
        AssistantMessageToolCallDeltaEvent(contentIndex=1, delta="", partial=partial_tool),
        AssistantMessageToolCallEndEvent(contentIndex=1, toolCall=call, partial=partial_tool),
        AssistantMessageDoneEvent(reason="toolUse", message=assistant),
        AssistantMessageErrorEvent(reason="error", error=failure),
    )
    records: tuple[object, ...] = (
        text,
        call,
        usage.cost,
        usage,
        user,
        assistant,
        tool_result,
        context,
        *events,
    )
    for record in records:
        encoded = encodeCanonical(record)
        assert decodeCanonical(encoded, type(record)) == record
        assert encodeCanonical(decodeCanonical(encoded, type(record))) == encoded
    for message in (user, assistant, tool_result):
        assert decodeCanonical(encodeCanonical(message), Message) == message
    for event in events:
        assert decodeCanonical(encodeCanonical(event), AssistantMessageEvent) == event


@pytest.mark.parametrize(
    "data",
    (
        b' {"text":"x","type":"text"}',
        b'{"type":"text","text":"x"}',
        b'{"text":"\\u0078","type":"text"}',
        b'{"text":"x","text":"x","type":"text"}',
        b'{"text":"x","type":"text"}\n',
        b'\xef\xbb\xbf{"text":"x","type":"text"}',
        b'{"text":"\xff","type":"text"}',
        b'{"extra":0,"text":"x","type":"text"}',
        b'{"text":"x","type":"unknown"}',
    ),
)
def test_private_canonical_decoder_rejects_noncanonical_or_invalid_bytes(data: bytes) -> None:
    with pytest.raises(ValueError):
        decodeCanonical(data, TextContent)


def test_private_canonical_codec_preserves_numeric_spellings() -> None:
    values = (
        (1, b"1"),
        (1.0, b"1.0"),
        (0.0, b"0.0"),
        (-0.0, b"-0.0"),
        (1e-7, b"1e-7"),
        (1e20, b"1e20"),
    )
    for value, expected in values:
        assert encodeCanonical(value) == expected
        decoded = decodeCanonical(expected, JSONValue)
        assert type(decoded) is type(value)
        if isinstance(value, float) and value == 0.0:
            assert math.copysign(1.0, cast(float, decoded)) == math.copysign(1.0, value)
        else:
            assert decoded == value


def test_json_snapshot_and_canonical_codec_do_not_leak_recursion_limit() -> None:
    nested: object = None
    for _ in range(1_500):
        nested = [nested]
    value = ToolCall(
        id="deep", name="tool", arguments=cast(Any, {"deep": nested})
    )
    encoded = encodeCanonical(value)
    decoded = decodeCanonical(encoded, ToolCall)
    assert decoded == value
    assert encodeCanonical(decoded) == encoded


def test_ticket_02_conformance_authorities_are_linked_to_public_observations() -> None:
    matrix = json.loads((ROOT / "conformance/obligation-matrix.json").read_text())
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    expected = {
        case["id"]: case.get("omhExpectation", case["observations"])
        for case in corpus["cases"]
    }
    required = {
        "omh-v0.message-public-value-records": "reference.message-public-value-records",
        "omh-v0.json-value-domain": "reference.json-value-domain",
        "omh-v0.optional-message-fields": "reference.optional-message-fields",
        "omh-v0.canonical-message-bytes": "reference.canonical-message-bytes",
        "omh-v0.assistant-message-event-variants": "reference.assistant-message-event-variants",
    }
    rows = {row["id"]: row for row in matrix["obligations"]}
    assert required.keys() <= rows.keys()
    for obligation, corpus_case in required.items():
        assert rows[obligation]["corpusCase"] == corpus_case
        assert rows[obligation]["executableCases"] == ["ticket-02-values"]

    record_names = [
        "TextContent",
        "ToolCall",
        "UserMessage",
        "AssistantMessage",
        "ToolResultMessage",
        "Usage",
        "UsageCost",
        "Context",
    ]
    event_traces = {
        "doneTrace": [
            "start",
            "text_start",
            "text_delta",
            "text_end",
            "toolcall_start",
            "toolcall_delta",
            "toolcall_end",
            "done",
        ],
        "preStartErrorTrace": ["error"],
    }
    actual = {
        "reference.message-public-value-records": {
            "A": "admitted",
            "L": [],
            "T": {
                "records": record_names,
                "contentKinds": ["text", "toolCall"],
                "messageRoles": ["user", "assistant", "toolResult"],
                "stopReasons": ["stop", "length", "toolUse", "error", "aborted"],
                "immutable": True,
                "hashable": True,
            },
            "E": {"nestedAliasesRetained": False},
            "C": "values_remain_stable",
        },
        "reference.json-value-domain": {
            "A": {
                "accepted": [
                    "null",
                    "bool",
                    "unicode-string",
                    "safe-int",
                    "finite-float",
                    "tuple",
                    "string-mapping",
                ],
                "rejected": [
                    "cycle",
                    "non-string-key",
                    "unsafe-int",
                    "nonfinite-float",
                    "surrogate",
                    "arbitrary-object",
                ],
            },
            "L": [],
            "T": "immutable-owned-json",
            "E": [],
            "C": "deep-iterative-round-trip",
        },
        "reference.optional-message-fields": {
            "A": "admitted",
            "L": [],
            "T": {
                "missingAttributesReadAs": "None",
                "requiredDetailsNull": True,
                "emptyValuesPreserved": True,
            },
            "E": {
                "missingContextBytes": '{"messages":[]}',
                "presentEmptyContextBytes": '{"messages":[],"systemPrompt":"","tools":[]}',
            },
            "C": "round_trip_distinctions_preserved",
        },
        "reference.canonical-message-bytes": {
            "A": "canonical-only",
            "L": [],
            "T": {
                "numericCarriers": ["int", "float", "positive-zero", "negative-zero"],
                "unicode": "unescaped-scalars",
                "mappingOrder": "unicode-code-point",
            },
            "E": {"codecPublic": False, "reencodeByteIdentical": True},
            "C": "noncanonical-and-invalid-input-rejected",
        },
        "reference.assistant-message-event-variants": {
            "A": "admitted",
            "L": event_traces,
            "T": {"uniqueTerminal": True, "terminalPayloadIdentity": True},
            "E": [],
            "C": "no-event-after-terminal",
        },
    }
    assert {case_id: expected[case_id] for case_id in actual} == actual
