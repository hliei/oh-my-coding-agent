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

from .mutation_queue import MUTATION_QUEUE as _MUTATION_QUEUE
from .paths import resolve_literal_tool_path


_PATH_PATTERN = "^[^\x00]+$"


def _quote(path: str) -> str:
    return json.dumps(path, ensure_ascii=False)


WRITE_DESCRIPTION = (
    "Write content to a file. Creates the file if it doesn't exist, overwrites "
    "if it does. Automatically creates parent directories."
)


def _path_schema(description: str) -> dict[str, object]:
    return {
        "type": "string",
        "minLength": 1,
        "pattern": _PATH_PATTERN,
        "description": description,
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


def _makedirs(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _open_write(path: str) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)


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
    async with _MUTATION_QUEUE.hold(key, signal):
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
                    raise _AgentToolOwnerCleanupError(error) from error
        _raise_if_cancelled(signal)
        if io_error is not None:
            return _classify_write_error(io_error, path, "write")
        return _text_result(
            f"Successfully wrote {len(encoded)} bytes to {_quote(path)}"
        )


def create_write_tool(workspace: str) -> AgentTool:
    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: AbortSignal,
        on_update: Any,
    ) -> AgentToolResult:
        return await execute_write(workspace, tool_call_id, params, signal, on_update)

    return AgentTool(
        name="write",
        label="write",
        description=WRITE_DESCRIPTION,
        parameters=cast(Mapping[str, JSONValue], WRITE_PARAMETERS),
        execute=cast(Any, execute),
        prepareArguments=None,
        executionMode=None,
    )
