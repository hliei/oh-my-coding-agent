from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
import errno
import json
import os
import stat
from typing import Any, cast

from oh_my_coding_agent._edit_diff import (
    EditMatchError,
    apply_literal_edits,
    generate_diff_string,
    generate_unified_patch,
)
from oh_my_core import AgentTool, AgentToolResult
from oh_my_llm import AbortSignal, JSONValue, LifecycleError, TextContent


_SAFE_INTEGER = 2**53 - 1
_PATH_PATTERN = "^[^\x00]+$"
_MAX_OUTPUT_LINES = 2000
_MAX_OUTPUT_BYTES = 51_200
_KIB = 1024
_MIB = 1024 * 1024

READ_DESCRIPTION = (
    "Read the contents of a UTF-8 text regular file. Output is truncated to "
    "2000 lines or 50KB (whichever is hit first). Use offset/limit for large "
    "files. When you need the full file, continue with offset until complete."
)
BASH_DESCRIPTION = (
    "Execute a bash command in the current working directory. Returns stdout "
    "and stderr. Output is truncated to last 2000 lines or 50KB (whichever is "
    "hit first). If truncated, full output is saved to a temp file. Optionally "
    "provide a timeout in seconds."
)
EDIT_DESCRIPTION = (
    "Edit a single file using exact text replacement. Every edits[].oldText "
    "must match a unique, non-overlapping region of the original file. If two "
    "changes affect the same block or nearby lines, merge them into one edit "
    "instead of emitting overlapping edits. Do not include large unchanged "
    "regions just to connect distant changes."
)
WRITE_DESCRIPTION = (
    "Write content to a file. Creates the file if it doesn't exist, overwrites "
    "if it does. Automatically creates parent directories."
)

BUILTIN_TOOL_SUMMARIES: tuple[str, str, str, str] = (
    "Read file contents",
    "Execute bash commands (ls, grep, find, etc.)",
    "Make precise file edits with exact text replacement, including multiple disjoint edits in one call",
    "Create or overwrite files",
)


def builtin_tool_prompt_section() -> str:
    return "\n".join(BUILTIN_TOOL_SUMMARIES)


def resolve_literal_tool_path(workspace: str, path: str) -> str:
    joined = path if os.path.isabs(path) else os.path.join(workspace, path)
    return os.path.normpath(joined)


def _quote(path: str) -> str:
    return json.dumps(path, ensure_ascii=False)


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

BASH_PARAMETERS: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["command"],
    "properties": {
        "command": {
            "type": "string",
            "description": "Bash command to execute",
        },
        "timeout": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": 2147483.647,
            "description": "Timeout in seconds (optional, no default timeout)",
        },
    },
}

EDIT_PARAMETERS: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "edits"],
    "properties": {
        "path": _path_schema("Path to the file to edit (relative or absolute)"),
        "edits": {
            "type": "array",
            "minItems": 1,
            "description": (
                "One or more targeted replacements. Each edit is matched against "
                "the original file, not incrementally. Do not include overlapping "
                "or nested edits. If two changes touch the same block or nearby "
                "lines, merge them into one edit instead."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["oldText", "newText"],
                "properties": {
                    "oldText": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Exact text for one targeted replacement. It must be "
                            "unique in the original file and must not overlap with "
                            "any other edits[].oldText in the same call."
                        ),
                    },
                    "newText": {
                        "type": "string",
                        "description": "Replacement text for this targeted edit.",
                    },
                },
            },
        },
    },
}

WRITE_PARAMETERS: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "content"],
    "properties": {
        "path": _path_schema("Path to the file to write (relative or absolute)"),
        "content": {
            "type": "string",
            "description": "Content to write to the file",
        },
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


def _format_size(size: int) -> str:
    if size < _KIB:
        return f"{size}B"
    if size < _MIB:
        return f"{size / _KIB:.1f}KB"
    return f"{size / _MIB:.1f}MB"


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


async def _acquire_uninterruptibly(lock: asyncio.Lock) -> None:
    waiter = asyncio.ensure_future(lock.acquire())
    try:
        await waiter
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None:
            current.uncancel()
        await waiter
        raise


class _FileMutationQueue:
    def __init__(self) -> None:
        self._meta = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._refs: dict[str, int] = {}

    def key_for(self, resolved: str) -> str:
        try:
            if os.path.exists(resolved):
                return os.path.realpath(resolved)
        except OSError:
            pass
        return os.path.abspath(resolved)

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._meta:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            self._refs[key] = self._refs.get(key, 0) + 1
            return lock

    async def _drop(self, key: str) -> None:
        async with self._meta:
            remaining = self._refs[key] - 1
            if remaining == 0:
                del self._refs[key]
                del self._locks[key]
            else:
                self._refs[key] = remaining

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock = await self._lock_for(key)
        acquired = False
        try:
            try:
                await _acquire_uninterruptibly(lock)
            except asyncio.CancelledError:
                acquired = True
                raise
            acquired = True
            yield
        finally:
            if acquired:
                lock.release()
            await self._drop(key)


_MUTATION_QUEUE = _FileMutationQueue()


async def _after_queue_hold(signal: AbortSignal, key: str) -> None:
    del key
    await _yield_for_cancellation(signal)


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
                raise LifecycleError(
                    "cleanup",
                    "Agent loop cleanup failed",
                    causes=(error,),
                ) from error
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


def _makedirs(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _open_write(path: str) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)


def _write_outcome(
    code: str,
    path: str,
    text: str,
    phase: str,
    effect: str,
) -> AgentToolResult:
    return _text_result(
        text,
        {"code": code, "path": path, "phase": phase, "effect": effect},
    )


_WRITE_EFFECT = {
    "identity": "none",
    "parents": "parents_may_exist",
    "write": "target_may_be_partial",
}
_WRITE_SUFFIX = {
    "none": "",
    "parents_may_exist": "; some parent directories may have been created",
    "target_may_be_partial": "; the target may be partially or completely changed",
}


def _classify_write_error(
    error: OSError, path: str, phase: str
) -> AgentToolResult:
    code = error.errno
    effect = _WRITE_EFFECT[phase]
    suffix = _WRITE_SUFFIX[effect]
    if code == errno.ENOENT:
        return _write_outcome(
            "not_found",
            path,
            f"Write path {_quote(path)} was not found{suffix}",
            phase,
            effect,
        )
    if code in (
        errno.ENAMETOOLONG,
        errno.EINVAL,
        errno.ELOOP,
    ):
        return _write_outcome(
            "invalid_path",
            path,
            f"Write path {_quote(path)} is invalid{suffix}",
            phase,
            effect,
        )
    if code in (errno.ENOTDIR, errno.EEXIST):
        return _write_outcome(
            "parent_not_directory",
            path,
            f"Write parent of {_quote(path)} is not a directory{suffix}",
            phase,
            effect,
        )
    if code == errno.EISDIR:
        return _write_outcome(
            "target_is_directory",
            path,
            f"Write path {_quote(path)} is a directory{suffix}",
            phase,
            effect,
        )
    if code in (errno.EACCES, errno.EPERM, errno.EROFS):
        return _write_outcome(
            "not_writable",
            path,
            f"Write path {_quote(path)} is not writable{suffix}",
            phase,
            effect,
        )
    quota = getattr(errno, "EDQUOT", None)
    if code == errno.ENOSPC or (quota is not None and code == quota):
        return _write_outcome(
            "storage_full",
            path,
            (
                f"Write path {_quote(path)} could not be completed because "
                f"storage is full{suffix}"
            ),
            phase,
            effect,
        )
    raise error


def _lstat_path(path: str) -> os.stat_result:
    return os.lstat(path)


def _write_identity_outcome(resolved: str, path: str) -> AgentToolResult | None:
    try:
        status = _lstat_path(resolved)
    except FileNotFoundError:
        return None
    except OSError as error:
        return _classify_write_error(error, path, "identity")
    if not stat.S_ISLNK(status.st_mode):
        return None
    try:
        _stat_path(resolved)
    except OSError as error:
        return _classify_write_error(error, path, "identity")
    return None


def _write_file_bytes(handle: int, data: bytes) -> int:
    return os.write(handle, data)


async def _create_parents(parent: str, signal: AbortSignal) -> None:
    del signal
    _makedirs(parent)


async def _write_handle(
    handle: int, encoded: bytes, signal: AbortSignal
) -> None:
    offset = 0
    while offset < len(encoded):
        _raise_if_cancelled(signal)
        written = _write_file_bytes(handle, encoded[offset:])
        if written == 0:
            raise OSError("write made no progress")
        offset += written


async def execute_write(
    workspace: str,
    tool_call_id: str,
    params: dict[str, object],
    signal: AbortSignal,
    on_update: Any,
) -> AgentToolResult:
    del tool_call_id, on_update
    path = cast(str, params["path"])
    content = cast(str, params["content"])
    resolved = resolve_literal_tool_path(workspace, path)
    encoded = content.encode("utf-8")
    key = _MUTATION_QUEUE.key_for(resolved)
    async with _MUTATION_QUEUE.hold(key):
        await _after_queue_hold(signal, key)
        identity = _write_identity_outcome(resolved, path)
        _raise_if_cancelled(signal)
        if identity is not None:
            return identity
        parent = os.path.dirname(resolved)
        if parent:
            try:
                await _create_parents(parent, signal)
            except OSError as error:
                _raise_if_cancelled(signal)
                return _classify_write_error(error, path, "parents")
        await _yield_for_cancellation(signal)
        handle = None
        io_error: OSError | None = None
        try:
            handle = _open_write(resolved)
            await _write_handle(handle, encoded, signal)
        except OSError as error:
            io_error = error
        finally:
            if handle is not None:
                try:
                    _close_handle(handle)
                except OSError as error:
                    raise LifecycleError(
                        "cleanup",
                        "Agent loop cleanup failed",
                        causes=(error,),
                    ) from error
        _raise_if_cancelled(signal)
        if io_error is not None:
            return _classify_write_error(io_error, path, "write")
        return _text_result(
            f"Successfully wrote {len(encoded)} bytes to {_quote(path)}"
        )


_BOM = "\ufeff"
_EDIT_SUFFIX = {
    "none": "",
    "target_may_be_partial": "; the target may be partially or completely changed",
}


def _edit_outcome(
    code: str,
    path: str,
    text: str,
    phase: str,
    effect: str,
    extra: Mapping[str, JSONValue] | None = None,
) -> AgentToolResult:
    details: dict[str, JSONValue] = {
        "code": code,
        "path": path,
        "phase": phase,
        "effect": effect,
    }
    if extra is not None:
        details.update(extra)
    return _text_result(text, details)


def _classify_edit_error(
    error: OSError, path: str, phase: str, *, started: bool = False
) -> AgentToolResult:
    code = error.errno
    effect = (
        "target_may_be_partial" if phase == "write" and started else "none"
    )
    suffix = _EDIT_SUFFIX[effect]
    quoted = _quote(path)
    if code in (errno.ENOENT, errno.ENOTDIR):
        return _edit_outcome(
            "not_found",
            path,
            f"Edit path {quoted} was not found{suffix}",
            phase,
            effect,
        )
    if code in (errno.ENAMETOOLONG, errno.EINVAL, errno.ELOOP):
        return _edit_outcome(
            "invalid_path",
            path,
            f"Edit path {quoted} is invalid{suffix}",
            phase,
            effect,
        )
    if code == errno.EISDIR:
        return _edit_outcome(
            "not_regular",
            path,
            f"Edit path {quoted} is not a regular file{suffix}",
            phase,
            effect,
        )
    if code in (errno.EACCES, errno.EPERM):
        if phase == "write":
            return _edit_outcome(
                "not_writable",
                path,
                f"Edit path {quoted} is not writable{suffix}",
                phase,
                effect,
            )
        return _edit_outcome(
            "not_readable",
            path,
            f"Edit path {quoted} is not readable{suffix}",
            phase,
            effect,
        )
    if code == errno.EROFS:
        return _edit_outcome(
            "not_writable",
            path,
            f"Edit path {quoted} is not writable{suffix}",
            phase,
            effect,
        )
    quota = getattr(errno, "EDQUOT", None)
    if code == errno.ENOSPC or (quota is not None and code == quota):
        return _edit_outcome(
            "storage_full",
            path,
            (
                f"Edit path {quoted} could not be completed because "
                f"storage is full{suffix}"
            ),
            phase,
            effect,
        )
    raise error


def _edit_identity_outcome(resolved: str, path: str) -> AgentToolResult | None:
    try:
        status = _stat_path(resolved)
    except OSError as error:
        return _classify_edit_error(error, path, "identity")
    if not stat.S_ISREG(status.st_mode):
        return _edit_outcome(
            "not_regular",
            path,
            f"Edit path {_quote(path)} is not a regular file",
            "identity",
            "none",
        )
    if not os.access(resolved, os.R_OK):
        return _edit_outcome(
            "not_readable",
            path,
            f"Edit path {_quote(path)} is not readable",
            "identity",
            "none",
        )
    if not os.access(resolved, os.W_OK):
        return _edit_outcome(
            "not_writable",
            path,
            f"Edit path {_quote(path)} is not writable",
            "identity",
            "none",
        )
    return None


def _open_read(path: str) -> int:
    return os.open(path, os.O_RDONLY)


def _open_edit_write(path: str) -> int:
    return os.open(path, os.O_WRONLY | os.O_TRUNC)


def _read_file_bytes(handle: int) -> bytes:
    chunk = os.read(handle, 65536)
    return chunk


async def _read_handle(handle: int, signal: AbortSignal) -> bytes:
    chunks: list[bytes] = []
    while True:
        _raise_if_cancelled(signal)
        chunk = _read_file_bytes(handle)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _apply_literal_edits(
    original: str, edits: Sequence[Mapping[str, str]]
) -> str | EditMatchError:
    return apply_literal_edits(original, edits)


def _generate_diff_string(old_content: str, new_content: str) -> tuple[str, int]:
    result = generate_diff_string(old_content, new_content)
    return result.diff, result.first_changed_line


def _generate_unified_patch(path: str, old_content: str, new_content: str) -> str:
    return generate_unified_patch(path, old_content, new_content)


def _build_edit_success(
    path: str, edit_count: int, old_content: str, new_content: str
) -> AgentToolResult:
    diff, first_changed_line = _generate_diff_string(old_content, new_content)
    patch = _generate_unified_patch(path, old_content, new_content)
    return _text_result(
        f"Successfully replaced {edit_count} block(s) in {path}.",
        {
            "diff": diff,
            "patch": patch,
            "firstChangedLine": first_changed_line,
        },
    )


def _match_outcome(path: str, error: EditMatchError) -> AgentToolResult:
    quoted = _quote(path)
    extra: dict[str, JSONValue] = dict(error.extra)
    if error.code == "text_not_found":
        index = int(error.extra["editIndex"])
        text = f"Edit text at edits[{index}] was not found in {quoted}"
    elif error.code == "text_not_unique":
        index = int(error.extra["editIndex"])
        occurrences = int(error.extra["occurrences"])
        text = (
            f"Edit text at edits[{index}] matched {occurrences} locations in "
            f"{quoted}"
        )
    elif error.code == "overlapping_edits":
        first = int(error.extra["firstEditIndex"])
        second = int(error.extra["secondEditIndex"])
        text = (
            f"Edit ranges at edits[{first}] and edits[{second}] overlap in "
            f"{quoted}"
        )
    else:
        text = f"Edit path {quoted} would not change"
    return _edit_outcome(error.code, path, text, "match", "none", extra)


async def execute_edit(
    workspace: str,
    tool_call_id: str,
    params: dict[str, object],
    signal: AbortSignal,
    on_update: Any,
) -> AgentToolResult:
    del tool_call_id, on_update
    path = cast(str, params["path"])
    edits = cast(list[dict[str, str]], params["edits"])
    resolved = resolve_literal_tool_path(workspace, path)
    key = _MUTATION_QUEUE.key_for(resolved)
    async with _MUTATION_QUEUE.hold(key):
        await _after_queue_hold(signal, key)
        identity = _edit_identity_outcome(resolved, path)
        _raise_if_cancelled(signal)
        if identity is not None:
            return identity
        handle = None
        data = b""
        io_error: OSError | None = None
        early: AgentToolResult | None = None
        try:
            handle = _open_read(resolved)
            opened = os.fstat(handle)
            if not stat.S_ISREG(opened.st_mode):
                early = _edit_outcome(
                    "not_regular",
                    path,
                    f"Edit path {_quote(path)} is not a regular file",
                    "read",
                    "none",
                )
            else:
                data = await _read_handle(handle, signal)
        except OSError as error:
            io_error = error
        finally:
            if handle is not None:
                try:
                    _close_handle(handle)
                except OSError as error:
                    raise LifecycleError(
                        "cleanup",
                        "Agent loop cleanup failed",
                        causes=(error,),
                    ) from error
        _raise_if_cancelled(signal)
        if io_error is not None:
            return _classify_edit_error(io_error, path, "read")
        if early is not None:
            return early
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            _raise_if_cancelled(signal)
            return _edit_outcome(
                "not_text",
                path,
                f"Edit path {_quote(path)} is not valid UTF-8 text",
                "read",
                "none",
            )
        if "\x00" in text:
            _raise_if_cancelled(signal)
            return _edit_outcome(
                "not_text",
                path,
                f"Edit path {_quote(path)} is not valid UTF-8 text",
                "read",
                "none",
            )
        bom = _BOM if text.startswith(_BOM) else ""
        original = text[len(bom) :]
        await _yield_for_cancellation(signal)
        replacement = _apply_literal_edits(original, edits)
        if isinstance(replacement, EditMatchError):
            _raise_if_cancelled(signal)
            return _match_outcome(path, replacement)
        await _yield_for_cancellation(signal)
        result = _build_edit_success(path, len(edits), original, replacement)
        await _yield_for_cancellation(signal)
        write_handle = None
        write_error: OSError | None = None
        started = False
        encoded = (bom + replacement).encode("utf-8")
        try:
            write_handle = _open_edit_write(resolved)
            started = True
            await _write_handle(write_handle, encoded, signal)
        except OSError as error:
            write_error = error
        finally:
            if write_handle is not None:
                try:
                    _close_handle(write_handle)
                except OSError as error:
                    raise LifecycleError(
                        "cleanup",
                        "Agent loop cleanup failed",
                        causes=(error,),
                    ) from error
        _raise_if_cancelled(signal)
        if write_error is not None:
            return _classify_edit_error(
                write_error, path, "write", started=started
            )
        return result


async def _unoperational_builtin(
    tool_call_id: str,
    params: dict[str, object],
    signal: AbortSignal,
    on_update: Any,
) -> AgentToolResult:
    del tool_call_id, params, signal, on_update
    raise RuntimeError("built-in execute is not operational")


def _reserved_tool(
    *,
    name: str,
    description: str,
    parameters: Mapping[str, object],
    execute: Any,
) -> AgentTool:
    return AgentTool(
        name=name,
        label=name,
        description=description,
        parameters=cast(Mapping[str, JSONValue], parameters),
        execute=execute,
        prepareArguments=None,
        executionMode=None,
    )


def product_session_tools(workspace: str) -> tuple[AgentTool, ...]:
    async def read_execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: AbortSignal,
        on_update: Any,
    ) -> AgentToolResult:
        return await execute_read(workspace, tool_call_id, params, signal, on_update)

    async def write_execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: AbortSignal,
        on_update: Any,
    ) -> AgentToolResult:
        return await execute_write(workspace, tool_call_id, params, signal, on_update)

    async def edit_execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: AbortSignal,
        on_update: Any,
    ) -> AgentToolResult:
        return await execute_edit(workspace, tool_call_id, params, signal, on_update)

    return (
        _reserved_tool(
            name="read",
            description=READ_DESCRIPTION,
            parameters=READ_PARAMETERS,
            execute=read_execute,
        ),
        _reserved_tool(
            name="bash",
            description=BASH_DESCRIPTION,
            parameters=BASH_PARAMETERS,
            execute=_unoperational_builtin,
        ),
        _reserved_tool(
            name="edit",
            description=EDIT_DESCRIPTION,
            parameters=EDIT_PARAMETERS,
            execute=edit_execute,
        ),
        _reserved_tool(
            name="write",
            description=WRITE_DESCRIPTION,
            parameters=WRITE_PARAMETERS,
            execute=write_execute,
        ),
    )
