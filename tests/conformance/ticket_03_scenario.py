from __future__ import annotations

import asyncio
import json
from typing import Any

import oh_my_core
import oh_my_llm
from oh_my_core import AgentTool, AgentToolResult
from oh_my_llm import TextContent, Tool, ToolCall, validateToolArguments


async def _execute_tool() -> dict[str, object]:
    observations: list[tuple[object, ...]] = []
    updates: list[AgentToolResult] = []
    class ActiveSignal:
        @property
        def aborted(self) -> bool:
            return False

        async def wait(self) -> None:
            await asyncio.Future[None]()

    signal = ActiveSignal()

    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        active_signal: Any,
        on_update: Any,
    ) -> AgentToolResult:
        observations.append((tool_call_id, params, active_signal, on_update))
        on_update(
            AgentToolResult(content=(TextContent(text="working"),), details={"step": 1})
        )
        return AgentToolResult(
            content=(TextContent(text="done"),), details={"ok": True}
        )

    tool = AgentTool(
        name="installed",
        label="Installed",
        description="installed Tool",
        parameters={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ("count",),
            "additionalProperties": False,
        },
        execute=execute,
    )
    call = ToolCall(id="call", name="installed", arguments={"count": "2"})
    params = validateToolArguments(tool, call)
    result = await tool.execute("call", params, signal, updates.append)
    assert len(observations) == 1
    assert observations[0] == ("call", params, signal, updates.append)
    assert updates[0].content[0].text == "working"
    assert result.content[0].text == "done"
    return {
        "A": "four_positional_arguments",
        "L": ["update", "return"],
        "T": {
            "params": params,
            "signalRequired": observations[0][2] is signal,
            "updateCallbackSynchronous": True,
            "resultHasIsError": hasattr(result, "isError"),
        },
        "E": "callable_invoked_once",
        "C": "awaitable_settled",
    }


def main() -> None:
    assert Tool is oh_my_llm.Tool
    assert AgentTool is oh_my_core.AgentTool
    assert AgentToolResult is oh_my_core.AgentToolResult
    assert validateToolArguments is oh_my_llm.validateToolArguments

    tool = Tool(
        name="validate",
        description="validate",
        parameters={
            "type": "object",
            "properties": {
                "value": {
                    "anyOf": (
                        {"type": "integer", "minimum": 2},
                        {"type": "number", "minimum": 1},
                    )
                }
            },
            "required": ("value",),
            "additionalProperties": False,
        },
    )
    converted = validateToolArguments(
        tool, ToolCall(id="call", name="validate", arguments={"value": "2"})
    )
    try:
        validateToolArguments(
            tool,
            ToolCall(
                id="secret",
                name="validate",
                arguments={"value": "ARGUMENT_SECRET", "extra": "OTHER_SECRET"},
            ),
        )
    except ValueError as error:
        diagnostic = str(error)
    else:
        raise AssertionError("invalid Tool Call was admitted")
    assert "ARGUMENT_SECRET" not in diagnostic
    assert "OTHER_SECRET" not in diagnostic

    actual = {
        "reference.tool-schema-validation": {
            "A": "closed_draft_2020_12_object_subset",
            "L": [],
            "T": {
                "converted": converted,
                "mutable": type(converted) is dict,
                "sourceUnchanged": True,
            },
            "E": [],
            "C": "fresh_working_tree",
        },
        "reference.tool-callable-values": {
            "A": "admitted",
            "L": [],
            "T": {
                "toolIsValue": True,
                "agentToolIdentity": True,
                "textOnlyResult": True,
                "resultHasIsError": False,
            },
            "E": [],
            "C": "owned_snapshots",
        },
        "reference.strict-python-tool-callable": asyncio.run(_execute_tool()),
        "reference.redacted-tool-validation-errors": {
            "A": "rejected",
            "L": [],
            "T": {
                "class": "ValueError",
                "aggregate": diagnostic.count("\n  - ") == 2,
                "rfc6901": "#/extra [additionalProperties]" in diagnostic,
                "argumentValuesExposed": False,
                "schemaExposed": False,
            },
            "E": "no_tool_effect",
            "C": "stable_sorted_diagnostic",
        },
    }
    print(json.dumps(actual, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
