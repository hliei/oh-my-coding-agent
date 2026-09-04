from __future__ import annotations

from dataclasses import dataclass
import math
from typing import cast

from oh_my_core import AgentMessage
from oh_my_llm import (
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from oh_my_llm._canonical import encodeCanonical

from ._session_manager import (
    CompactionEntry,
    SessionEntry,
    SessionManager,
    SessionMessageEntry,
)


RESERVE_TOKENS = 16_384
KEEP_RECENT_TOKENS = 20_000
CONTEXT_WINDOW = 128_000
SUMMARY_MAX_TOKENS = 13_107
PREFIX_SUMMARY_MAX_TOKENS = 8_192

SUMMARIZATION_SYSTEM_PROMPT = (
    "You are a context summarization assistant. Your task is to read a "
    "conversation between a user and an AI assistant, then produce a structured "
    "summary following the exact format specified.\nDo NOT continue the "
    "conversation. Do NOT respond to any questions in the conversation. ONLY "
    "output the structured summary."
)

_SUMMARIZATION_PROMPT = """The messages above are a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:
## Goal
[What is the user trying to accomplish? Can be multiple items if the session covers different tasks.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]
## Critical Context
- [Any data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

_UPDATE_SUMMARIZATION_PROMPT = """The messages above are NEW conversation messages to incorporate into the existing summary provided in <previous-summary> tags.
Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- If something is no longer relevant, you may remove it

Use this EXACT format:
## Goal
[Preserve existing goals, add new ones if the task expanded]

## Constraints & Preferences
- [Preserve existing, add new ones discovered]

## Progress
### Done
- [x] [Include previously done items AND newly completed items]

### In Progress
- [ ] [Current work - update based on progress]

### Blocked
- [Current blockers - remove if resolved]

## Key Decisions
- **[Decision]**: [Brief rationale] (preserve all previous, add new)

## Next Steps
1. [Update based on current state]
## Critical Context
- [Preserve important context, add new if needed]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

_TURN_PREFIX_SUMMARIZATION_PROMPT = """This is the PREFIX of a turn that was too large to keep. The SUFFIX (recent work) is retained.

Summarize the prefix to provide context for the retained suffix:

## Original Request
[What did the user ask for in this turn?]

## Early Progress
- [Key decisions and work done in the prefix]

## Context for Suffix
- [Information needed to understand the retained recent work]

Be concise. Focus on what's needed to understand the kept suffix."""


@dataclass(frozen=True, slots=True)
class CompactionPreparation:
    first_kept_entry_id: str
    messages_to_summarize: tuple[AgentMessage, ...]
    turn_prefix_messages: tuple[AgentMessage, ...]
    tokens_before: int
    previous_summary: str | None
    read_files: tuple[str, ...]
    modified_files: tuple[str, ...]


def estimate_message_tokens(message: AgentMessage) -> int:
    characters = 0
    if isinstance(message, UserMessage):
        if isinstance(message.content, str):
            characters = len(message.content)
        else:
            for user_item in message.content:
                characters += len(user_item.text)
    elif isinstance(message, AssistantMessage):
        for item in message.content:
            if isinstance(item, TextContent):
                characters += len(item.text)
            elif isinstance(item, ToolCall):
                characters += len(item.name) + len(
                    _canonical_json(item.arguments)
                )
    elif isinstance(message, ToolResultMessage):
        characters = sum(len(item.text) for item in message.content)
    return math.ceil(characters / 4)


def estimate_context_tokens(
    messages: tuple[AgentMessage, ...],
) -> tuple[int, int | None]:
    last_usage_index: int | None = None
    usage_tokens = 0
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        usage_tokens = 0
        if isinstance(message, AssistantMessage):
            usage = message.usage
            usage_tokens = usage.totalTokens or (
                usage.input + usage.output + usage.cacheRead + usage.cacheWrite
            )
        if (
            isinstance(message, AssistantMessage)
            and message.stopReason not in ("aborted", "error")
            and usage_tokens > 0
        ):
            last_usage_index = index
            break
    if last_usage_index is None:
        return sum(estimate_message_tokens(message) for message in messages), None
    trailing = sum(
        estimate_message_tokens(message)
        for message in messages[last_usage_index + 1 :]
    )
    return usage_tokens + trailing, last_usage_index


def prepare_compaction(manager: SessionManager) -> CompactionPreparation | None:
    branch = manager.getBranch()
    if not branch or isinstance(branch[-1], CompactionEntry):
        return None

    previous_index = -1
    for index in range(len(branch) - 1, -1, -1):
        if isinstance(branch[index], CompactionEntry):
            previous_index = index
            break
    previous_summary: str | None = None
    boundary_start = 0
    if previous_index >= 0:
        previous = cast(CompactionEntry, branch[previous_index])
        previous_summary = previous.summary
        for index, entry in enumerate(branch):
            if entry.id == previous.firstKeptEntryId:
                boundary_start = index
                break
        else:
            boundary_start = previous_index + 1

    cut_point_list: list[int] = []
    for index in range(boundary_start, len(branch)):
        entry = branch[index]
        if isinstance(entry, SessionMessageEntry) and isinstance(
            entry.message, (UserMessage, AssistantMessage)
        ):
            cut_point_list.append(index)
    cut_points = tuple(cut_point_list)
    if not cut_points:
        return None

    accumulated = 0
    cut_index = cut_points[0]
    for index in range(len(branch) - 1, boundary_start - 1, -1):
        message = _entry_message(branch[index])
        if message is None:
            continue
        accumulated += estimate_message_tokens(message)
        if accumulated >= KEEP_RECENT_TOKENS:
            cut_index = next(
                (point for point in cut_points if point >= index), cut_points[0]
            )
            break

    while cut_index > boundary_start:
        previous_entry = branch[cut_index - 1]
        if isinstance(previous_entry, (CompactionEntry, SessionMessageEntry)):
            break
        cut_index -= 1

    history_end = cut_index
    cut_message = _entry_message(branch[cut_index])
    if isinstance(cut_message, AssistantMessage):
        for index in range(cut_index, boundary_start - 1, -1):
            message = _entry_message(branch[index])
            if isinstance(message, UserMessage):
                history_end = index
                break

    messages = tuple(
        message
        for entry in branch[boundary_start:history_end]
        if (message := _entry_message(entry)) is not None
    )
    prefix = tuple(
        message
        for entry in branch[history_end:cut_index]
        if (message := _entry_message(entry)) is not None
    )
    if not messages and not prefix:
        return None
    summarized = (*messages, *prefix)
    read_files, modified_files = _file_operations(summarized, branch, previous_index)
    tokens_before, _ = estimate_context_tokens(
        manager.buildSessionContext().messages
    )
    return CompactionPreparation(
        first_kept_entry_id=branch[cut_index].id,
        messages_to_summarize=messages,
        turn_prefix_messages=prefix,
        tokens_before=tokens_before,
        previous_summary=previous_summary,
        read_files=read_files,
        modified_files=modified_files,
    )


def build_summary_prompt(
    preparation: CompactionPreparation, custom_instructions: str | None
) -> str:
    conversation = _serialize_conversation(preparation.messages_to_summarize)
    prompt = f"<conversation>\n{conversation}\n</conversation>\n\n"
    base = (
        _SUMMARIZATION_PROMPT
        if preparation.previous_summary is None
        else _UPDATE_SUMMARIZATION_PROMPT
    )
    if preparation.previous_summary is not None:
        prompt += (
            f"<previous-summary>\n{preparation.previous_summary}\n"
            "</previous-summary>\n\n"
        )
    if custom_instructions:
        base = f"{base}\n\nAdditional focus: {custom_instructions}"
    return prompt + base


def build_turn_prefix_prompt(preparation: CompactionPreparation) -> str:
    conversation = _serialize_conversation(preparation.turn_prefix_messages)
    return (
        f"<conversation>\n{conversation}\n</conversation>\n\n"
        f"{_TURN_PREFIX_SUMMARIZATION_PROMPT}"
    )


def append_file_operations(
    summary: str, preparation: CompactionPreparation
) -> str:
    sections: list[str] = []
    if preparation.read_files:
        sections.append(
            f"<read-files>\n{'\n'.join(preparation.read_files)}\n</read-files>"
        )
    if preparation.modified_files:
        sections.append(
            "<modified-files>\n"
            f"{'\n'.join(preparation.modified_files)}\n</modified-files>"
        )
    return summary if not sections else f"{summary}\n\n{'\n\n'.join(sections)}"


def _entry_message(entry: SessionEntry) -> AgentMessage | None:
    return entry.message if isinstance(entry, SessionMessageEntry) else None


def _canonical_json(value: object) -> str:
    return encodeCanonical(value).decode("utf-8")


def _serialize_conversation(messages: tuple[AgentMessage, ...]) -> str:
    parts: list[str] = []
    for message in messages:
        if isinstance(message, UserMessage):
            text = (
                message.content
                if isinstance(message.content, str)
                else "".join(
                    item.text
                    for item in message.content
                )
            )
            if text:
                parts.append(f"[User]: {text}")
        elif isinstance(message, AssistantMessage):
            texts = [
                item.text for item in message.content if isinstance(item, TextContent)
            ]
            calls = [
                f"{item.name}({_canonical_json(item.arguments)})"
                for item in message.content
                if isinstance(item, ToolCall)
            ]
            if texts:
                parts.append(f"[Assistant]: {'\n'.join(texts)}")
            if calls:
                parts.append(f"[Assistant tool calls]: {'; '.join(calls)}")
        elif isinstance(message, ToolResultMessage):
            text = "".join(item.text for item in message.content)
            if text:
                truncated = text[:2000]
                if len(text) > 2000:
                    truncated += (
                        f"\n\n[... {len(text) - 2000} more characters truncated]"
                    )
                parts.append(f"[Tool result]: {truncated}")
    return "\n\n".join(parts)


def _file_operations(
    messages: tuple[AgentMessage, ...],
    branch: tuple[SessionEntry, ...],
    previous_index: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    read: set[str] = set()
    modified: set[str] = set()
    if previous_index >= 0:
        previous = cast(CompactionEntry, branch[previous_index])
        if not previous.fromHook and isinstance(previous.details, dict):
            read.update(_string_items(previous.details.get("readFiles")))
            modified.update(_string_items(previous.details.get("modifiedFiles")))
    for message in messages:
        if not isinstance(message, AssistantMessage):
            continue
        for item in message.content:
            if not isinstance(item, ToolCall):
                continue
            path = item.arguments.get("path")
            if type(path) is not str or not path:
                continue
            if item.name == "read":
                read.add(path)
            elif item.name in ("edit", "write"):
                modified.add(path)
    return tuple(sorted(read - modified)), tuple(sorted(modified))


def _string_items(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if type(item) is str)
