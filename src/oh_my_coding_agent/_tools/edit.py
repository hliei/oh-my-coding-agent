from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import errno
import json
import os
import stat
from typing import Any, cast

from oh_my_core import AgentTool, AgentToolResult
from oh_my_core._tools import _AgentToolOwnerCleanupError
from oh_my_llm import AbortSignal, JSONValue, TextContent

from .edit_diff import (
    EditMatchError,
    apply_literal_edits,
    generate_diff_string,
    generate_unified_patch,
)
from .mutation_queue import MUTATION_QUEUE as _MUTATION_QUEUE
from .paths import resolve_literal_tool_path


_PATH_PATTERN = "^[^\x00]+$"


def _quote(path: str) -> str:
    return json.dumps(path, ensure_ascii=False)


EDIT_DESCRIPTION = (
    "Edit a single file using exact text replacement. Every edits[].oldText "
    "must match a unique, non-overlapping region of the original file. If two "
    "changes affect the same block or nearby lines, merge them into one edit "
    "instead of emitting overlapping edits. Do not include large unchanged "
    "regions just to connect distant changes."
)


def _path_schema(description: str) -> dict[str, object]:
    return {
        "type": "string",
        "minLength": 1,
        "pattern": _PATH_PATTERN,
        "description": description,
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


def _text_result(text: str, details: JSONValue = None) -> AgentToolResult:
    return AgentToolResult(
        content=(TextContent(text=text),),
        details=details,
        terminate=None,
    )


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


def _write_file_bytes(handle: int, data: bytes) -> int:
    return os.write(handle, data)


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
    async with _MUTATION_QUEUE.hold(key, signal):
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
                    raise _AgentToolOwnerCleanupError(error) from error
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
                    raise _AgentToolOwnerCleanupError(error) from error
        _raise_if_cancelled(signal)
        if write_error is not None:
            return _classify_edit_error(
                write_error, path, "write", started=started
            )
        return result


def create_edit_tool(workspace: str) -> AgentTool:
    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: AbortSignal,
        on_update: Any,
    ) -> AgentToolResult:
        return await execute_edit(workspace, tool_call_id, params, signal, on_update)

    return AgentTool(
        name="edit",
        label="edit",
        description=EDIT_DESCRIPTION,
        parameters=cast(Mapping[str, JSONValue], EDIT_PARAMETERS),
        execute=cast(Any, execute),
        prepareArguments=None,
        executionMode=None,
    )
