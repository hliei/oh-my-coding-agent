from __future__ import annotations

from typing import TypedDict

from oh_my_llm import JSONValue


_MAX_OUTPUT_LINES = 2000
_MAX_OUTPUT_BYTES = 51_200
_KIB = 1024
_MIB = 1024 * 1024


class TruncationFacts(TypedDict):
    content: str
    truncated: bool
    truncatedBy: str | None
    totalLines: int
    totalBytes: int
    outputLines: int
    outputBytes: int
    lastLinePartial: bool
    firstLineExceedsLimit: bool
    maxLines: int
    maxBytes: int


def _format_size(size: int) -> str:
    if size < _KIB:
        return f"{size}B"
    if size < _MIB:
        unit = _KIB
        suffix = "KB"
    else:
        unit = _MIB
        suffix = "MB"
    tenths = (size * 10 + unit // 2) // unit
    return f"{tenths // 10}.{tenths % 10}{suffix}"


def _utf8_byte_length(text: str) -> int:
    return len(text.encode("utf-8"))


def _split_lines_for_counting(content: str) -> list[str]:
    if content == "":
        return []
    lines = content.split("\n")
    if content.endswith("\n"):
        lines.pop()
    return lines


def _truncate_string_to_bytes_from_end(text: str, max_bytes: int) -> str:
    buffer = text.encode("utf-8")
    if len(buffer) <= max_bytes:
        return text
    start = len(buffer) - max_bytes
    while start < len(buffer) and buffer[start] & 0xC0 == 0x80:
        start += 1
    return buffer[start:].decode("utf-8")


def truncate_tail(
    content: str,
    *,
    max_lines: int = _MAX_OUTPUT_LINES,
    max_bytes: int = _MAX_OUTPUT_BYTES,
) -> TruncationFacts:
    total_bytes = _utf8_byte_length(content)
    lines = _split_lines_for_counting(content)
    total_lines = len(lines)
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return {
            "content": content,
            "truncated": False,
            "truncatedBy": None,
            "totalLines": total_lines,
            "totalBytes": total_bytes,
            "outputLines": total_lines,
            "outputBytes": total_bytes,
            "lastLinePartial": False,
            "firstLineExceedsLimit": False,
            "maxLines": max_lines,
            "maxBytes": max_bytes,
        }
    collected: list[str] = []
    output_bytes = 0
    truncated_by = "lines"
    last_line_partial = False
    index = total_lines - 1
    while index >= 0 and len(collected) < max_lines:
        line = lines[index]
        line_bytes = _utf8_byte_length(line) + (1 if collected else 0)
        if output_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            if not collected:
                truncated_line = _truncate_string_to_bytes_from_end(line, max_bytes)
                collected.insert(0, truncated_line)
                output_bytes = _utf8_byte_length(truncated_line)
                last_line_partial = True
            break
        collected.insert(0, line)
        output_bytes += line_bytes
        index -= 1
    if len(collected) >= max_lines and output_bytes <= max_bytes:
        truncated_by = "lines"
    output_content = "\n".join(collected)
    return {
        "content": output_content,
        "truncated": True,
        "truncatedBy": truncated_by,
        "totalLines": total_lines,
        "totalBytes": total_bytes,
        "outputLines": len(collected),
        "outputBytes": _utf8_byte_length(output_content),
        "lastLinePartial": last_line_partial,
        "firstLineExceedsLimit": False,
        "maxLines": max_lines,
        "maxBytes": max_bytes,
    }


def _truncation_json(facts: TruncationFacts) -> dict[str, JSONValue]:
    return {
        "content": facts["content"],
        "truncated": facts["truncated"],
        "truncatedBy": facts["truncatedBy"],
        "totalLines": facts["totalLines"],
        "totalBytes": facts["totalBytes"],
        "outputLines": facts["outputLines"],
        "outputBytes": facts["outputBytes"],
        "lastLinePartial": facts["lastLinePartial"],
        "firstLineExceedsLimit": facts["firstLineExceedsLimit"],
        "maxLines": facts["maxLines"],
        "maxBytes": facts["maxBytes"],
    }
