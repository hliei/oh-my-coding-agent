from __future__ import annotations

from oh_my_core import AgentTool

from .bash import create_bash_tool
from .edit import create_edit_tool
from .read import create_read_tool
from .write import create_write_tool


BUILTIN_TOOL_SUMMARIES: tuple[str, str, str, str] = (
    "Read file contents",
    "Execute bash commands (ls, grep, find, etc.)",
    (
        "Make precise file edits with exact text replacement, including multiple "
        "disjoint edits in one call"
    ),
    "Create or overwrite files",
)


def builtin_tool_prompt_section() -> str:
    return "\n".join(BUILTIN_TOOL_SUMMARIES)


def product_session_tools(workspace: str) -> tuple[AgentTool, ...]:
    return (
        create_read_tool(workspace),
        create_bash_tool(workspace),
        create_edit_tool(workspace),
        create_write_tool(workspace),
    )
