from __future__ import annotations

import asyncio
from collections.abc import Mapping
import errno
import json
import os
import stat
from typing import Any, cast

from oh_my_core import AgentTool, AgentToolResult
from oh_my_core._tools import _AgentToolOwnerCleanupError
from oh_my_llm import AbortSignal, JSONValue, TextContent

from .paths import resolve_literal_tool_path
from .truncation import _MAX_OUTPUT_BYTES, _MAX_OUTPUT_LINES, _format_size


_SAFE_INTEGER = 2**53 - 1
_PATH_PATTERN = "^[^\x00]+$"


def _quote(path: str) -> str:
    return json.dumps(path, ensure_ascii=False)


READ_DESCRIPTION = (
    "Read the contents of a UTF-8 text regular file. Output is truncated to "
    "2000 lines or 50KB (whichever is hit first). Use offset/limit for large "
    "files. When you need the full file, continue with offset until complete."
)


def _path_schema(description: str) -> dict[str, object]:
    return {
        "type": "string",
        "minLength": 1,
        "pattern": _PATH_PATTERN,
        "description": description,
    }


def _positive_line_schema(description: str) -> dict[str, object]:
    return {
        "type": "integer",
        "minimum": 1,
        "maximum": _SAFE_INTEGER,
        "description": description,
    }


READ_PARAMETERS: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path"],
    "properties": {
        "path": _path_schema("Path to the file to read (relative or absolute)"),
        "offset": _positive_line_schema(
            "Line number to start reading from (1-indexed)"
        ),
        "limit": _positive_line_schema("Maximum number of lines to read"),
    },
}


def _text_result(text: str, details: JSONValue = None) -> AgentToolResult:
    return AgentToolResult(
        content=(TextContent(text=text),),
        details=details,
        terminate=None,
    )


def _read_outcome(
    code: str,
    path: str,
    text: str,
    extra: Mapping[str, JSONValue] | None = None,
) -> AgentToolResult:
    details: dict[str, JSONValue] = {"code": code, "path": path}
    if extra is not None:
        details.update(extra)
    return _text_result(text, details)


def _stat_path(path: str) -> os.stat_result:
    return os.stat(path)


def _close_handle(handle: int) -> None:
    os.close(handle)


def _raise_if_cancelled(signal: AbortSignal) -> None:
    if signal.aborted:
        raise asyncio.CancelledError


async def _yield_for_cancellation(signal: AbortSignal) -> None:
    _raise_if_cancelled(signal)
    await asyncio.sleep(0)
    _raise_if_cancelled(signal)


def _classify_open_error(error: OSError, path: str) -> AgentToolResult:
    code = error.errno
    if code in (errno.ENOENT, errno.ENOTDIR):
        return _read_outcome(
            "not_found", path, f"Read path {_quote(path)} was not found"
        )
    if code in (errno.EACCES, errno.EPERM):
        return _read_outcome(
            "not_readable", path, f"Read path {_quote(path)} is not readable"
        )
    if code == errno.EISDIR:
        return _not_regular(path)
    raise error


def _not_regular(path: str) -> AgentToolResult:
    return _read_outcome(
        "not_regular", path, f"Read path {_quote(path)} is not a regular file"
    )


def _head_truncate(
    selected: tuple[str, ...],
    *,
    offset: int,
    file_line_count: int,
    caller_limited: bool,
) -> AgentToolResult:
    selected_text = "\n".join(selected)
    selected_bytes = len(selected_text.encode("utf-8"))
    start = offset
    collected: list[str] = []
    output_bytes = 0
    truncated_by: str | None = None
    first_line_exceeds = False
    for index, line in enumerate(selected):
        if len(collected) >= _MAX_OUTPUT_LINES:
            truncated_by = "lines"
            break
        piece = line if index == 0 else f"\n{line}"
        piece_bytes = len(piece.encode("utf-8"))
        if output_bytes + piece_bytes > _MAX_OUTPUT_BYTES:
            if not collected:
                first_line_exceeds = True
                truncated_by = "bytes"
            else:
                truncated_by = "bytes"
            break
        collected.append(line)
        output_bytes += piece_bytes
    raw = "\n".join(collected)
    output_lines = len(collected)
    if first_line_exceeds:
        first_size = len(selected[0].encode("utf-8"))
        notice = (
            f"[Line {start} is {_format_size(first_size)}, exceeds 50.0KB limit. "
            "Use bash for an explicit bounded byte-range read.]"
        )
        return _text_result(
            notice,
            {
                "truncation": {
                    "content": "",
                    "truncated": True,
                    "truncatedBy": "bytes",
                    "totalLines": len(selected),
                    "totalBytes": selected_bytes,
                    "outputLines": 0,
                    "outputBytes": 0,
                    "lastLinePartial": False,
                    "firstLineExceedsLimit": True,
                    "maxLines": _MAX_OUTPUT_LINES,
                    "maxBytes": _MAX_OUTPUT_BYTES,
                }
            },
        )
    if truncated_by is None:
        selected_end = start + len(selected) - 1 if selected else start - 1
        remaining = file_line_count - (offset - 1 + len(selected))
        if caller_limited and remaining > 0:
            next_offset = selected_end + 1
            return _text_result(
                f"{raw}\n\n[{remaining} more lines in file. Use offset={next_offset} to continue.]"
            )
        return _text_result(raw)
    end = start + output_lines - 1
    next_offset = end + 1
    total = len(selected)
    if truncated_by == "lines":
        notice = (
            f"[Showing lines {start}-{end} of {total}. "
            f"Use offset={next_offset} to continue.]"
        )
    else:
        notice = (
            f"[Showing lines {start}-{end} of {total} (50.0KB limit). "
            f"Use offset={next_offset} to continue.]"
        )
    return _text_result(
        f"{raw}\n\n{notice}",
        {
            "truncation": {
                "content": raw,
                "truncated": True,
                "truncatedBy": truncated_by,
                "totalLines": total,
                "totalBytes": selected_bytes,
                "outputLines": output_lines,
                "outputBytes": output_bytes,
                "lastLinePartial": False,
                "firstLineExceedsLimit": False,
                "maxLines": _MAX_OUTPUT_LINES,
                "maxBytes": _MAX_OUTPUT_BYTES,
            }
        },
    )


async def execute_read(
    workspace: str,
    tool_call_id: str,
    params: dict[str, object],
    signal: AbortSignal,
    on_update: Any,
) -> AgentToolResult:
    del tool_call_id, on_update
    path = cast(str, params["path"])
    offset = cast(int, params.get("offset", 1))
    limit_value = params.get("limit")
    limit = None if limit_value is None else cast(int, limit_value)
    resolved = resolve_literal_tool_path(workspace, path)
    await _yield_for_cancellation(signal)
    try:
        status = _stat_path(resolved)
    except OSError as error:
        _raise_if_cancelled(signal)
        return _classify_open_error(error, path)
    if not stat.S_ISREG(status.st_mode):
        _raise_if_cancelled(signal)
        return _not_regular(path)
    _raise_if_cancelled(signal)
    handle = None
    data = b""
    io_error: OSError | None = None
    early: AgentToolResult | None = None
    try:
        handle = os.open(resolved, os.O_RDONLY)
        _raise_if_cancelled(signal)
        opened = os.fstat(handle)
        if not stat.S_ISREG(opened.st_mode):
            early = _not_regular(path)
        else:
            chunks: list[bytes] = []
            while True:
                _raise_if_cancelled(signal)
                chunk = os.read(handle, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
    except OSError as error:
        io_error = error
    finally:
        if handle is not None:
            try:
                _close_handle(handle)
            except OSError as error:
                raise _AgentToolOwnerCleanupError(error) from error
    _raise_if_cancelled(signal)
    if io_error is not None:
        return _classify_open_error(io_error, path)
    if early is not None:
        return early
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _raise_if_cancelled(signal)
        return _read_outcome(
            "not_text", path, f"Read path {_quote(path)} is not valid UTF-8 text"
        )
    if "\x00" in text:
        _raise_if_cancelled(signal)
        return _read_outcome(
            "not_text", path, f"Read path {_quote(path)} is not valid UTF-8 text"
        )
    lines = text.split("\n")
    if offset > len(lines):
        _raise_if_cancelled(signal)
        return _read_outcome(
            "offset_out_of_range",
            path,
            f"Read offset {offset} is beyond end of {_quote(path)} ({len(lines)} lines)",
            extra={"offset": offset, "totalLines": len(lines)},
        )
    selected = (
        tuple(lines[offset - 1 : offset - 1 + limit])
        if limit is not None
        else tuple(lines[offset - 1 :])
    )
    result = _head_truncate(
        selected,
        offset=offset,
        file_line_count=len(lines),
        caller_limited=limit is not None,
    )
    _raise_if_cancelled(signal)
    return result


def create_read_tool(workspace: str) -> AgentTool:
    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: AbortSignal,
        on_update: Any,
    ) -> AgentToolResult:
        return await execute_read(workspace, tool_call_id, params, signal, on_update)

    return AgentTool(
        name="read",
        label="read",
        description=READ_DESCRIPTION,
        parameters=cast(Mapping[str, JSONValue], READ_PARAMETERS),
        execute=cast(Any, execute),
        prepareArguments=None,
        executionMode=None,
    )
