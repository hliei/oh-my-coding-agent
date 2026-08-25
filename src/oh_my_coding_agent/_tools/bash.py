from __future__ import annotations

import asyncio
from collections.abc import Mapping
import errno
import json
import os
import stat
import sys
from typing import Any, cast

from oh_my_core import AgentTool, AgentToolResult
from oh_my_core._tools import _AgentToolOwnerCleanupError
from oh_my_llm import AbortSignal, JSONValue, TextContent

from .output import (
    OutputAccumulator,
    _OutputFault as _OutputFault,
    _OutputSnapshot,
    _UpdateThrottle,
    _recognized_output_error,
    _storage_full,
)
from .shell import (
    _ShellConfig,
    _host_environment,
    _kill_process_tree,
    _usable_shell,
    resolve_shell as resolve_shell,
)
from .truncation import _format_size, _truncation_json

_EMPTY_UPDATE = AgentToolResult(content=(), details=None, terminate=None)

BASH_DESCRIPTION = (
    "Execute a bash command in the current working directory. Returns stdout "
    "and stderr. Output is truncated to last 2000 lines or 50KB (whichever is "
    "hit first). If truncated, full output is saved to a temp file. Optionally "
    "provide a timeout in seconds."
)

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


def _quote(path: str) -> str:
    return json.dumps(path, ensure_ascii=False)


def _ecmascript_number_text(value: int | float) -> str:
    if type(value) is int:
        return str(value)
    if value == 0.0:
        return "0"
    token = repr(value).lower()
    if "e" in token:
        mantissa, exponent = token.split("e", 1)
        exponent_value = int(exponent)
        if -6 <= exponent_value < 21:
            sign = "-" if mantissa.startswith("-") else ""
            mantissa = mantissa.removeprefix("-")
            digits = mantissa.replace(".", "")
            position = 1 + exponent_value
            if position <= 0:
                return f"{sign}0.{('0' * -position)}{digits}"
            if position >= len(digits):
                return f"{sign}{digits}{'0' * (position - len(digits))}"
            return f"{sign}{digits[:position]}.{digits[position:]}"
        token = f"{mantissa.removesuffix('.0')}e{exponent_value:+d}"
    elif token.endswith(".0"):
        token = token[:-2]
    return token


def _raise_if_cancelled(signal: AbortSignal) -> None:
    if signal.aborted:
        raise asyncio.CancelledError


async def _yield_for_cancellation(signal: AbortSignal) -> None:
    _raise_if_cancelled(signal)
    await asyncio.sleep(0)
    _raise_if_cancelled(signal)


def _text_result(text: str, details: JSONValue = None) -> AgentToolResult:
    return AgentToolResult(
        content=(TextContent(text=text),),
        details=details,
        terminate=None,
    )


def _admission_outcome(
    code: str,
    phase: str,
    text: str,
    extra: Mapping[str, JSONValue] | None = None,
) -> AgentToolResult:
    details: dict[str, JSONValue] = {"code": code, "phase": phase, "effect": "none"}
    if extra is not None:
        details.update(extra)
    return _text_result(text, details)


def _workspace_unavailable(workspace: str) -> AgentToolResult | None:
    try:
        status = os.stat(workspace)
    except OSError as error:
        if error.errno not in (errno.ENOENT, errno.ENOTDIR, errno.EACCES, errno.EPERM):
            raise
        return _admission_outcome(
            "workspace_unavailable",
            "workspace",
            f"Bash Workspace {_quote(workspace)} is unavailable",
            {"workspace": workspace},
        )
    if not stat.S_ISDIR(status.st_mode) or not os.access(workspace, os.X_OK):
        return _admission_outcome(
            "workspace_unavailable",
            "workspace",
            f"Bash Workspace {_quote(workspace)} is unavailable",
            {"workspace": workspace},
        )
    return None


def _read_pipe(stream: asyncio.StreamReader) -> Any:
    return stream.read(65536)


async def _after_spawn(signal: AbortSignal, pid: int) -> None:
    del pid
    await _yield_for_cancellation(signal)


async def _spawn_process(
    config: _ShellConfig,
    command: str,
    workspace: str,
    env: Mapping[str, str],
) -> asyncio.subprocess.Process:
    stdin = (
        asyncio.subprocess.PIPE
        if config.command_from_stdin
        else asyncio.subprocess.DEVNULL
    )
    argv = (
        list(config.args)
        if config.command_from_stdin
        else [*config.args, command]
    )
    kwargs: dict[str, Any] = {
        "cwd": workspace,
        "env": dict(env),
        "stdin": stdin,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    try:
        process = await asyncio.create_subprocess_exec(config.shell, *argv, **kwargs)
    except FileNotFoundError as error:
        phase = (
            "workspace"
            if _workspace_unavailable(workspace) is not None
            else "shell"
        )
        raise _OutputFault(
            phase, OSError(errno.ENOENT, os.strerror(errno.ENOENT))
        ) from error
    except PermissionError as error:
        denied = OSError(errno.EACCES, os.strerror(errno.EACCES))
        denied.errno = errno.EACCES
        phase = (
            "workspace"
            if _workspace_unavailable(workspace) is not None
            else "shell" if not _usable_shell(config.shell) else "spawn"
        )
        raise _OutputFault(phase, denied) from error
    except ValueError as error:
        raise _OutputFault("command", OSError(errno.EINVAL, str(error))) from error
    except OSError as error:
        if error.errno == errno.ENOENT:
            raise _OutputFault("shell", error) from error
        if error.errno in (errno.EACCES, errno.EPERM):
            raise _OutputFault("spawn", error) from error
        raise
    if config.command_from_stdin:
        assert process.stdin is not None
        process.stdin.write(command.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
    return process


def _truncation_notice(snapshot: _OutputSnapshot, last_line_bytes: int) -> str:
    truncation = snapshot.truncation
    start_line = truncation["totalLines"] - truncation["outputLines"] + 1
    end_line = truncation["totalLines"]
    path = snapshot.full_output_path
    if truncation["lastLinePartial"]:
        return (
            f"[Showing last {_format_size(truncation['outputBytes'])} of line "
            f"{end_line} (line is {_format_size(last_line_bytes)}). "
            f"Full output: {path}]"
        )
    if truncation["truncatedBy"] == "lines":
        return (
            f"[Showing lines {start_line}-{end_line} of {truncation['totalLines']}. "
            f"Full output: {path}]"
        )
    return (
        f"[Showing lines {start_line}-{end_line} of {truncation['totalLines']} "
        f"(50.0KB limit). Full output: {path}]"
    )


def _display_text(snapshot: _OutputSnapshot, last_line_bytes: int, empty: str) -> str:
    text = snapshot.content if snapshot.content != "" else empty
    if snapshot.truncation["truncated"]:
        text = f"{text}\n\n{_truncation_notice(snapshot, last_line_bytes)}"
    return text


def _status_details(
    snapshot: _OutputSnapshot,
    extra: Mapping[str, JSONValue],
) -> dict[str, JSONValue]:
    details = dict(extra)
    if snapshot.truncation["truncated"]:
        details["truncation"] = _truncation_json(snapshot.truncation)
        details["fullOutputPath"] = snapshot.full_output_path
    return details


def _infra_outcome(
    snapshot: _OutputSnapshot,
    code: str,
    phase: str,
    suffix: str,
) -> AgentToolResult:
    body = snapshot.content if snapshot.content != "" else "(no output captured)"
    return _text_result(
        f"{body}\n\n{suffix}",
        {
            "code": code,
            "phase": phase,
            "effect": "command_may_have_effects",
            "output": "partial",
        },
    )


def _infra_from_fault(snapshot: _OutputSnapshot, fault: _OutputFault) -> AgentToolResult:
    code = (
        "output_storage_full"
        if _storage_full(fault.error)
        else "output_unavailable"
    )
    suffix = (
        "Command output could not be saved completely because storage is full"
        if code == "output_storage_full"
        else "Command output could not be collected completely"
    )
    return _infra_outcome(snapshot, code, fault.phase, suffix)


async def _put_event(
    events: asyncio.Queue[tuple[str, object]], kind: str, payload: object
) -> None:
    await events.put((kind, payload))


async def _pump_stream(
    stream: asyncio.StreamReader,
    events: asyncio.Queue[tuple[str, object]],
) -> None:
    try:
        while True:
            chunk = await _read_pipe(stream)
            if not chunk:
                break
            await _put_event(events, "data", chunk)
    except OSError as error:
        await _put_event(events, "pipe_error", error)
    finally:
        await _put_event(events, "eof", None)


async def _watch_exit(
    process: asyncio.subprocess.Process,
    events: asyncio.Queue[tuple[str, object]],
) -> None:
    await _put_event(events, "exit", await process.wait())


async def _watch_abort(
    signal: AbortSignal, events: asyncio.Queue[tuple[str, object]]
) -> None:
    if not signal.aborted:
        await signal.wait()
    await _put_event(events, "abort", None)


async def _watch_timeout(
    seconds: float, events: asyncio.Queue[tuple[str, object]]
) -> None:
    await asyncio.sleep(seconds)
    await _put_event(events, "timeout", None)


async def execute_bash(
    workspace: str,
    tool_call_id: str,
    params: dict[str, object],
    signal: AbortSignal,
    on_update: Any,
) -> AgentToolResult:
    del tool_call_id
    command = cast(str, params["command"])
    timeout_value = params.get("timeout")
    _raise_if_cancelled(signal)
    on_update(_EMPTY_UPDATE)
    await _yield_for_cancellation(signal)
    if "\x00" in command:
        return _admission_outcome(
            "invalid_command", "command", "Bash command is invalid"
        )
    unavailable = _workspace_unavailable(workspace)
    if unavailable is not None:
        _raise_if_cancelled(signal)
        return unavailable
    await _yield_for_cancellation(signal)
    config = resolve_shell()
    if config is None or not _usable_shell(config.shell):
        _raise_if_cancelled(signal)
        return _admission_outcome(
            "shell_unavailable", "shell", "Bash shell is unavailable"
        )
    await _yield_for_cancellation(signal)
    env = _host_environment()
    try:
        process = await _spawn_process(config, command, workspace, env)
    except _OutputFault as fault:
        _raise_if_cancelled(signal)
        if fault.phase == "command":
            return _admission_outcome(
                "invalid_command", "command", "Bash command is invalid"
            )
        if fault.phase == "workspace":
            return _admission_outcome(
                "workspace_unavailable",
                "workspace",
                f"Bash Workspace {_quote(workspace)} is unavailable",
                {"workspace": workspace},
            )
        if fault.phase == "shell":
            return _admission_outcome(
                "shell_unavailable", "shell", "Bash shell is unavailable"
            )
        return _admission_outcome(
            "spawn_denied", "spawn", "Bash process creation is not permitted"
        )
    pid = process.pid
    assert pid is not None
    try:
        await _after_spawn(signal, pid)
        return await _collect_bash_result(
            process,
            pid,
            signal,
            on_update,
            timeout_value=timeout_value,
        )
    except BaseException:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            current.uncancel()
        await _kill_process_tree(pid)
        try:
            if process.stdout is not None:
                await process.stdout.read()
            if process.stderr is not None:
                await process.stderr.read()
            await process.wait()
        except OSError as error:
            raise _AgentToolOwnerCleanupError(error) from error
        raise


async def _collect_bash_result(
    process: asyncio.subprocess.Process,
    pid: int,
    signal: AbortSignal,
    on_update: Any,
    *,
    timeout_value: object,
) -> AgentToolResult:
    accumulator = OutputAccumulator()
    throttle = _UpdateThrottle(on_update, accumulator, signal)
    events: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_task = asyncio.create_task(_pump_stream(process.stdout, events))
    stderr_task = asyncio.create_task(_pump_stream(process.stderr, events))
    exit_task = asyncio.create_task(_watch_exit(process, events))
    abort_task = asyncio.create_task(_watch_abort(signal, events))
    timeout_task: asyncio.Task[None] | None = None
    if timeout_value is not None:
        timeout_task = asyncio.create_task(
            _watch_timeout(float(cast(int | float, timeout_value)), events)
        )
    watchers = [abort_task]
    if timeout_task is not None:
        watchers.append(timeout_task)
    pumps = [stdout_task, stderr_task, exit_task]
    accepting = True
    pending_eofs = 2
    exit_code: int | None = None
    timed_out = False
    output_fault: _OutputFault | None = None
    unclassified: BaseException | None = None
    killed = False
    interrupted = False

    async def kill_once() -> None:
        nonlocal killed
        if killed:
            return
        killed = True
        await _kill_process_tree(pid)

    def note_fault(fault: _OutputFault) -> None:
        nonlocal accepting, output_fault, unclassified
        accepting = False
        throttle.close_admission()
        if _recognized_output_error(fault.error):
            if output_fault is None:
                output_fault = fault
            return
        if unclassified is None:
            unclassified = fault.error

    async def consume(kind: str, payload: object) -> None:
        nonlocal pending_eofs, exit_code, timed_out
        if kind == "data":
            if not accepting:
                return
            try:
                accumulator.append(cast(bytes, payload))
                throttle.mark()
            except _OutputFault as fault:
                note_fault(fault)
                await kill_once()
                return
            if throttle.fault is not None:
                note_fault(throttle.fault)
                await kill_once()
            return
        if kind == "pipe_error":
            note_fault(_OutputFault("pipe", cast(OSError, payload)))
            await kill_once()
            return
        if kind == "eof":
            pending_eofs -= 1
            return
        if kind == "exit":
            exit_code = cast(int, payload)
            return
        if kind == "timeout":
            if exit_code is None:
                timed_out = True
                await kill_once()
            return
        if kind == "abort":
            await kill_once()

    try:
        while pending_eofs > 0 or exit_code is None:
            kind, payload = await events.get()
            await consume(kind, payload)
    except asyncio.CancelledError:
        interrupted = True
        await kill_once()
    current = asyncio.current_task()
    if current is not None and current.cancelling():
        current.uncancel()
        interrupted = True
        await kill_once()
    cleanup_error: OSError | None = None
    try:
        for watcher in watchers:
            watcher.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)
        while pending_eofs > 0 or exit_code is None:
            kind, payload = await events.get()
            if kind in {"timeout", "abort"}:
                continue
            await consume(kind, payload)
        await asyncio.gather(*pumps)
    except OSError as error:
        cleanup_error = error
    if throttle.fault is not None:
        note_fault(throttle.fault)
    failed = (
        interrupted
        or signal.aborted
        or output_fault is not None
        or unclassified is not None
    )
    if failed:
        throttle.close_admission()
        accumulator.finish(persist=False)
        snapshot = accumulator.snapshot(persist_if_truncated=False)
        retain_spill = (interrupted or signal.aborted) and throttle.exposed_spill
        try:
            if retain_spill:
                accumulator.close_temp_file()
            else:
                accumulator.abandon_temp_file()
        except _OutputFault as fault:
            cleanup_error = fault.error if cleanup_error is None else cleanup_error
        if cleanup_error is not None:
            raise _AgentToolOwnerCleanupError(cleanup_error) from cleanup_error
        if interrupted or signal.aborted:
            raise asyncio.CancelledError
        if unclassified is not None:
            raise unclassified
        assert output_fault is not None
        return _infra_from_fault(snapshot, output_fault)
    try:
        previous = accumulator.snapshot(persist_if_truncated=False).content
        accumulator.finish(persist=True)
        if accumulator.snapshot(persist_if_truncated=False).content != previous:
            throttle.mark()
        throttle.flush()
        if throttle.fault is not None:
            raise throttle.fault
        snapshot = accumulator.snapshot(persist_if_truncated=True)
        accumulator.close_temp_file()
    except _OutputFault as fault:
        accumulator.abandon_temp_file()
        if cleanup_error is not None:
            raise _AgentToolOwnerCleanupError(cleanup_error) from cleanup_error
        if _recognized_output_error(fault.error):
            return _infra_from_fault(
                accumulator.snapshot(persist_if_truncated=False), fault
            )
        raise _AgentToolOwnerCleanupError(fault.error) from fault.error
    if cleanup_error is not None:
        raise _AgentToolOwnerCleanupError(cleanup_error) from cleanup_error
    if signal.aborted:
        if not throttle.exposed_spill:
            accumulator.abandon_temp_file()
        raise asyncio.CancelledError
    return _terminal_result(
        snapshot,
        accumulator.last_line_bytes(),
        exit_code=exit_code if exit_code is not None else 0,
        timed_out=timed_out,
        timeout_value=timeout_value,
    )


def _terminal_result(
    snapshot: _OutputSnapshot,
    last_line_bytes: int,
    *,
    exit_code: int,
    timed_out: bool,
    timeout_value: object,
) -> AgentToolResult:
    output = _display_text(snapshot, last_line_bytes, "(no output)")
    if timed_out:
        timeout_number = cast(int | float, timeout_value)
        text = (
            f"{output}\n\nCommand timed out after "
            f"{_ecmascript_number_text(timeout_number)} seconds"
        )
        details = _status_details(
            snapshot, {"code": "timed_out", "timeout": timeout_number}
        )
        return _text_result(text, details)
    if exit_code < 0:
        signal_number = -exit_code
        text = f"{output}\n\nCommand terminated by signal {signal_number}"
        details = _status_details(
            snapshot, {"code": "signal_exit", "signal": signal_number}
        )
        return _text_result(text, details)
    if exit_code != 0:
        text = f"{output}\n\nCommand exited with code {exit_code}"
        details = _status_details(
            snapshot, {"code": "nonzero_exit", "exitCode": exit_code}
        )
        return _text_result(text, details)
    if snapshot.truncation["truncated"]:
        return _text_result(
            output,
            {
                "truncation": _truncation_json(snapshot.truncation),
                "fullOutputPath": snapshot.full_output_path,
            },
        )
    return _text_result(output)


def create_bash_tool(workspace: str) -> AgentTool:
    async def execute(
        tool_call_id: str,
        params: dict[str, object],
        signal: AbortSignal,
        on_update: Any,
    ) -> AgentToolResult:
        return await execute_bash(workspace, tool_call_id, params, signal, on_update)

    return AgentTool(
        name="bash",
        label="bash",
        description=BASH_DESCRIPTION,
        parameters=cast(Mapping[str, JSONValue], BASH_PARAMETERS),
        execute=cast(Any, execute),
        prepareArguments=None,
        executionMode=None,
    )
