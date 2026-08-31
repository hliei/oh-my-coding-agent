from __future__ import annotations

from datetime import datetime

from ._prompt_resources import PromptResourceSnapshot
from ._project_rules import ProjectRuleSnapshot
from ._tools.registry import BUILTIN_TOOLS


_IDENTITY = (
    "You are an expert coding assistant operating inside omh, a coding agent "
    "harness. You help users by reading files, executing commands, editing "
    "code, and writing new files."
)
_GUIDELINES = (
    "- Use bash for file operations like ls, rg, find\n"
    "- Be concise in your responses\n"
    "- Show file paths clearly when working with files\n"
    "- After the final mutation, claim that the modification succeeded only "
    "after observing the applicable named check's successful Tool Result. "
    "Otherwise report unverified or failed verification."
)
_SKILL_INTRO = (
    "\n\nThe following skills provide specialized instructions for specific tasks.\n"
    "Use the read tool to load a skill's file when the task matches its description.\n"
    "When a skill file references a relative path, resolve it against the skill "
    "directory (parent of SKILL.md / dirname of the path) and use that absolute "
    "path in tool commands.\n"
    "\n"
    "<available_skills>"
)


def _local_date() -> str:
    now = datetime.now()
    return f"{now.year:04d}-{now.month:02d}-{now.day:02d}"


def build_system_prompt(
    cwd: str,
    snapshot: PromptResourceSnapshot,
    project_rules: ProjectRuleSnapshot,
    extension_tools: tuple[tuple[str, str], ...] = (),
) -> str:
    tools = "\n".join(
        f"- {name}: {summary}"
        for name, summary in (*BUILTIN_TOOLS, *extension_tools)
    )
    prompt = (
        f"{_IDENTITY}\n"
        f"\n"
        f"Available tools:\n"
        f"{tools}\n"
        f"\n"
        f"Guidelines:\n"
        f"{_GUIDELINES}"
    )
    prompt += _format_project_rules(project_rules)
    prompt += _format_skills(snapshot)
    prompt += f"\nCurrent date: {_local_date()}"
    prompt += f"\nCurrent working directory: {cwd}"
    return prompt


def _format_project_rules(snapshot: ProjectRuleSnapshot) -> str:
    if not snapshot.rules:
        return ""
    lines = [
        "\n\n<project_context>\n",
        "Project-specific instructions and guidelines:\n",
    ]
    for rule in snapshot.rules:
        lines.append(f'<project_instructions path="{rule.path}">')
        lines.append(rule.content)
        lines.append("</project_instructions>\n")
    lines.append("</project_context>\n")
    return "\n".join(lines)


def _format_skills(snapshot: PromptResourceSnapshot) -> str:
    visible = [
        skill
        for skill in snapshot.skills
        if not skill.disable_model_invocation
    ]
    if not visible:
        return ""
    lines = [_SKILL_INTRO]
    for skill in visible:
        lines.append("  <skill>")
        lines.append(f"    <name>{_escape_xml(skill.name)}</name>")
        lines.append(
            f"    <description>{_escape_xml(skill.description)}</description>"
        )
        lines.append(f"    <location>{_escape_xml(skill.location)}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


def _escape_xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
