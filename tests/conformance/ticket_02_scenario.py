from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, fields, is_dataclass
import json
import math
import os
from types import MappingProxyType
from typing import Any, cast, get_args

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
    fauxText,
    fauxToolCall,
)
from oh_my_llm.providers.deepseek import deepseekProvider


_SAFE_INTEGER = 2**53 - 1


def _assert_records() -> None:
    records = (
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
    for record in records:
        assert is_dataclass(record)
        assert getattr(record, "__dataclass_params__").frozen
        assert record.__slots__
        assert all(field.kw_only or not field.init for field in fields(record))
    assert set(get_args(Message)) == {UserMessage, AssistantMessage, ToolResultMessage}
    assert len(get_args(AssistantMessageEvent)) == 9
    assert not hasattr(oh_my_llm, "encodeCanonical")
    assert not hasattr(oh_my_llm, "decodeCanonical")

    source: dict[str, Any] = {"nested": [{"value": 1}, -0.0]}
    call = ToolCall(id="call", name="read", arguments=source)
    source["nested"][0]["value"] = 2
    source["nested"].append("later")
    nested = cast(tuple[Any, ...], call.arguments["nested"])
    assert isinstance(call.arguments, MappingProxyType)
    assert isinstance(nested, tuple) and nested[0]["value"] == 1
    assert len(nested) == 2
    try:
        call.name = "write"  # type: ignore[misc]
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("ToolCall was mutable")
    assert hash(call)


def _assert_json_domain() -> None:
    valid_arguments: Any = {
        "null": None,
        "bool": False,
        "unicode": "😀",
        "minimum": -_SAFE_INTEGER,
        "maximum": _SAFE_INTEGER,
        "float": 1.0,
        "negativeZero": -0.0,
        "array": [True, {"nested": "ok"}],
    }
    valid = ToolCall(
        id="",
        name="",
        arguments=valid_arguments,
    )
    assert math.copysign(1.0, cast(float, valid.arguments["negativeZero"])) == -1.0
    rejected: tuple[object, ...] = (
        {"value": object()},
        {1: "value"},
        {"value": _SAFE_INTEGER + 1},
        {"value": float("nan")},
        {"value": "\ud800"},
    )
    for arguments in rejected:
        try:
            ToolCall(id="call", name="tool", arguments=arguments)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError(f"unsafe JSON admitted: {arguments!r}")
    cyclic: list[object] = []
    cyclic.append(cyclic)
    try:
        ToolCall(id="call", name="tool", arguments=cast(Any, {"cycle": cyclic}))
    except ValueError:
        pass
    else:
        raise AssertionError("cyclic JSON admitted")
    nested: object = None
    for _ in range(1_500):
        nested = [nested]
    assert ToolCall(
        id="deep", name="tool", arguments=cast(Any, {"deep": nested})
    )


def _assert_optional_and_carrier_distinctions() -> None:
    text = TextContent(text="", textSignature="")
    call = ToolCall(id="", name="", arguments={}, thoughtSignature="")
    user_text = UserMessage(content="", timestamp=-_SAFE_INTEGER)
    user_blocks = UserMessage(content=(text,), timestamp=_SAFE_INTEGER)
    tool_result = ToolResultMessage(
        toolCallId="",
        toolName="",
        content=(),
        details=None,
        isError=False,
        timestamp=0,
    )
    missing = Context(messages=())
    present_empty = Context(
        messages=(user_text, user_blocks, tool_result), systemPrompt="", tools=()
    )
    assert missing.systemPrompt is None and missing.tools is None
    assert present_empty.systemPrompt == "" and present_empty.tools == ()
    left = ToolCall(id="call", name="tool", arguments={"a": 1, "b": (0.0,)})
    reordered = ToolCall(id="call", name="tool", arguments={"b": (0.0,), "a": 1})
    float_one = ToolCall(id="call", name="tool", arguments={"a": 1.0, "b": (0.0,)})
    negative_zero = ToolCall(id="call", name="tool", arguments={"a": 1, "b": (-0.0,)})
    assert left == reordered and hash(left) == hash(reordered)
    assert left != float_one and left != negative_zero


async def _event_trace() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    final = fauxAssistantMessage(
        (
            fauxText("hello"),
            fauxToolCall(id="call", name="read", arguments={"path": "README.md"}),
        ),
        stopReason="toolUse",
    )
    faux.setResponses((final, fauxAssistantMessage("failed", stopReason="error", errorMessage="provider failed")))
    models = createModels()
    models.setProvider(faux.provider)
    context = Context(messages=(UserMessage(content="go", timestamp=0),))
    stream = models.streamSimple(model, context)
    events = [event async for event in stream]
    assert [event.type for event in events] == [
        "start",
        "text_start",
        "text_delta",
        "text_end",
        "toolcall_start",
        "toolcall_end",
        "done",
    ]
    assert isinstance(events[-1], AssistantMessageDoneEvent)
    assert isinstance(events[-2], AssistantMessageToolCallEndEvent)
    assert events[-1].message.content[-1] is events[-2].toolCall
    tool_partial = fauxAssistantMessage(
        (fauxText("hello"), fauxToolCall(id="call", name="read", arguments={}))
    )
    tool_delta = AssistantMessageToolCallDeltaEvent(
        contentIndex=1,
        delta='{"path":"README.md"}',
        partial=tool_partial,
    )
    assert tool_delta.type == "toolcall_delta"
    prior_key = os.environ.pop("DEEPSEEK_API_KEY", None)
    deepseek_models = createModels()
    deepseek_models.setProvider(deepseekProvider())
    deepseek_model = deepseek_models.getModel("deepseek", "deepseek-v4-flash")
    assert deepseek_model is not None
    failure = [
        event async for event in deepseek_models.streamSimple(deepseek_model, context)
    ]
    if prior_key is not None:
        os.environ["DEEPSEEK_API_KEY"] = prior_key
    assert [event.type for event in failure] == ["error"]
    assert isinstance(failure[0], AssistantMessageErrorEvent)


def main() -> None:
    _assert_records()
    _assert_json_domain()
    _assert_optional_and_carrier_distinctions()
    asyncio.run(asyncio.wait_for(_event_trace(), timeout=10.0))
    actual = {
        "reference.message-public-value-records": {
            "A": "admitted",
            "L": [],
            "T": {
                "records": ["TextContent", "ToolCall", "UserMessage", "AssistantMessage", "ToolResultMessage", "Usage", "UsageCost", "Context"],
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
            "A": {"accepted": ["null", "bool", "unicode-string", "safe-int", "finite-float", "tuple", "string-mapping"], "rejected": ["cycle", "non-string-key", "unsafe-int", "nonfinite-float", "surrogate", "arbitrary-object"]},
            "L": [],
            "T": "immutable-owned-json",
            "E": [],
            "C": "deep-iterative-round-trip",
        },
        "reference.optional-message-fields": {
            "A": "admitted",
            "L": [],
            "T": {"missingAttributesReadAs": "None", "requiredDetailsNull": True, "emptyValuesPreserved": True},
            "E": {"missingContextBytes": "{\"messages\":[]}", "presentEmptyContextBytes": "{\"messages\":[],\"systemPrompt\":\"\",\"tools\":[]}"},
            "C": "round_trip_distinctions_preserved",
        },
        "reference.canonical-message-bytes": {
            "A": "canonical-only",
            "L": [],
            "T": {"numericCarriers": ["int", "float", "positive-zero", "negative-zero"], "unicode": "unescaped-scalars", "mappingOrder": "unicode-code-point"},
            "E": {"codecPublic": False, "reencodeByteIdentical": True},
            "C": "noncanonical-and-invalid-input-rejected",
        },
        "reference.assistant-message-event-variants": {
            "A": "admitted",
            "L": {"doneTrace": ["start", "text_start", "text_delta", "text_end", "toolcall_start", "toolcall_delta", "toolcall_end", "done"], "preStartErrorTrace": ["error"]},
            "T": {"uniqueTerminal": True, "terminalPayloadIdentity": True},
            "E": [],
            "C": "no-event-after-terminal",
        },
    }
    print(json.dumps(actual, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
