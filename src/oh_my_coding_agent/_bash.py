from __future__ import annotations

import asyncio
from collections.abc import Mapping
import codecs
import errno
import json
import os
import shutil
import signal
import stat
import sys
import tempfile
from typing import Any, TypedDict, cast

from oh_my_core import AgentToolResult
from oh_my_llm import AbortSignal, JSONValue, LifecycleError, TextContent


_MAX_OUTPUT_LINES = 2000
_MAX_OUTPUT_BYTES = 51_200
_KIB = 1024
_MIB = 1024 * 1024
_BASH_UPDATE_THROTTLE_S = 0.1
_EMPTY_UPDATE = AgentToolResult(content=(), details=None, terminate=None)


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


class _OutputSnapshot:
    __slots__ = ("content", "truncation", "full_output_path")

    def __init__(
        self, content: str, truncation: TruncationFacts, full_output_path: str | None
    ) -> None:
        self.content = content
        self.truncation = truncation
        self.full_output_path = full_output_path


class _OutputFault(Exception):
    def __init__(self, phase: str, error: OSError) -> None:
        super().__init__(phase)
        self.phase = phase
        self.error = error


class _ShellConfig:
    __slots__ = ("shell", "args", "command_from_stdin")

    def __init__(
        self, shell: str, args: tuple[str, ...], *, command_from_stdin: bool = False
    ) -> None:
        self.shell = shell
        self.args = args
        self.command_from_stdin = command_from_stdin


def _quote(path: str) -> str:
    return json.dumps(path, ensure_ascii=False)


def _format_size(size: int) -> str:
    if size < _KIB:
        return f"{size}B"
    if size < _MIB:
        return f"{size / _KIB:.1f}KB"
    return f"{size / _MIB:.1f}MB"


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


def _is_legacy_wsl_bash(path: str) -> bool:
    normalized = path.replace("/", "\\").lower()
    return (
        len(normalized) >= 22
        and normalized[0].isalpha()
        and normalized.startswith(
            (
                normalized[0] + ":\\windows\\system32\\bash.exe",
                normalized[0] + ":\\windows\\sysnative\\bash.exe",
            )
        )
    )


def _bash_config(shell: str) -> _ShellConfig:
    if _is_legacy_wsl_bash(shell):
        return _ShellConfig(shell, ("-s",), command_from_stdin=True)
    return _ShellConfig(shell, ("-c",))


def _usable_shell(path: str) -> bool:
    try:
        status = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(status.st_mode) and os.access(path, os.X_OK)


def _first_on_path(name: str) -> str | None:
    found = shutil.which(name)
    if found is None or not _usable_shell(found):
        return None
    return found


def resolve_shell() -> _ShellConfig | None:
    if sys.platform == "win32":
        roots: list[str] = []
        program_files = os.environ.get("ProgramFiles")
        if program_files:
            roots.append(os.path.join(program_files, "Git", "bin", "bash.exe"))
        program_files_x86 = os.environ.get("ProgramFiles(x86)")
        if program_files_x86:
            roots.append(os.path.join(program_files_x86, "Git", "bin", "bash.exe"))
        for path in roots:
            if _usable_shell(path):
                return _bash_config(path)
        found = _first_on_path("bash.exe")
        if found is not None:
            return _bash_config(found)
        return None
    if _usable_shell("/bin/bash"):
        return _bash_config("/bin/bash")
    found = _first_on_path("bash")
    if found is not None:
        return _bash_config(found)
    found_sh = _first_on_path("sh")
    if found_sh is not None:
        return _ShellConfig(found_sh, ("-c",))
    return None


def _host_environment() -> dict[str, str]:
    return dict(os.environ)


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
    except OSError:
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


def _storage_full(error: OSError) -> bool:
    quota = getattr(errno, "EDQUOT", None)
    return error.errno == errno.ENOSPC or (quota is not None and error.errno == quota)


def _output_refusal(error: OSError) -> bool:
    return error.errno in (errno.EACCES, errno.EPERM, errno.EROFS)


def _recognized_output_error(error: OSError) -> bool:
    return _storage_full(error) or _output_refusal(error)


def _spill_path() -> str:
    return os.path.join(tempfile.gettempdir(), f"omh-bash-{os.urandom(8).hex()}.log")


def _open_spill(path: str) -> int:
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(handle, 0o600)
    return handle


def _write_spill(handle: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(handle, view)
        view = view[written:]


def _close_spill(handle: int) -> None:
    os.close(handle)


def _remove_spill(path: str | None) -> None:
    if path is None:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _read_pipe(stream: asyncio.StreamReader) -> Any:
    return stream.read(65536)


async def _after_spawn(signal: AbortSignal, pid: int) -> None:
    del pid
    await _yield_for_cancellation(signal)


async def _kill_process_tree(pid: int) -> None:
    if sys.platform == "win32":
        helper = await asyncio.create_subprocess_exec(
            "taskkill",
            "/F",
            "/T",
            "/PID",
            str(pid),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await helper.wait()
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


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
        raise _OutputFault("shell", OSError(errno.ENOENT, os.strerror(errno.ENOENT))) from error
    except PermissionError as error:
        denied = OSError(errno.EACCES, os.strerror(errno.EACCES))
        denied.errno = errno.EACCES
        raise _OutputFault("spawn", denied) from error
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


class OutputAccumulator:
    def __init__(self) -> None:
        self._max_lines = _MAX_OUTPUT_LINES
        self._max_bytes = _MAX_OUTPUT_BYTES
        self._max_rolling_bytes = max(self._max_bytes * 2, 1)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._raw_chunks: list[bytes] = []
        self._tail_text = ""
        self._tail_bytes = 0
        self._tail_starts_at_line_boundary = True
        self._total_raw_bytes = 0
        self._total_decoded_bytes = 0
        self._completed_lines = 0
        self._total_lines = 0
        self._current_line_bytes = 0
        self._has_open_line = False
        self._finished = False
        self._temp_path: str | None = None
        self._temp_handle: int | None = None

    @property
    def full_output_path(self) -> str | None:
        return self._temp_path

    def last_line_bytes(self) -> int:
        return self._current_line_bytes

    def append(self, data: bytes) -> None:
        if self._finished:
            raise RuntimeError("Cannot append to a finished output accumulator")
        self._total_raw_bytes += len(data)
        self._append_decoded(self._decoder.decode(data, False))
        if self._temp_handle is not None or self._should_use_temp_file():
            self._ensure_temp_file()
            self._write_current(data)
        elif data:
            self._raw_chunks.append(data)

    def finish(self, *, persist: bool = True) -> None:
        if self._finished:
            return
        self._finished = True
        self._append_decoded(self._decoder.decode(b"", True))
        if persist and self._should_use_temp_file():
            self._ensure_temp_file()

    def snapshot(self, *, persist_if_truncated: bool = False) -> _OutputSnapshot:
        tail = truncate_tail(self._snapshot_text())
        truncated = (
            self._total_lines > self._max_lines
            or self._total_decoded_bytes > self._max_bytes
        )
        if truncated:
            truncated_by = tail["truncatedBy"]
            if truncated_by is None:
                truncated_by = (
                    "bytes" if self._total_decoded_bytes > self._max_bytes else "lines"
                )
        else:
            truncated_by = None
        truncation: TruncationFacts = {
            **tail,
            "truncated": truncated,
            "truncatedBy": truncated_by,
            "totalLines": self._total_lines,
            "totalBytes": self._total_decoded_bytes,
            "maxLines": self._max_lines,
            "maxBytes": self._max_bytes,
        }
        if persist_if_truncated and truncation["truncated"]:
            self._ensure_temp_file()
        return _OutputSnapshot(truncation["content"], truncation, self._temp_path)

    def close_temp_file(self) -> None:
        handle = self._temp_handle
        if handle is None:
            return
        self._temp_handle = None
        try:
            _close_spill(handle)
        except OSError as error:
            raise _OutputFault("spill_close", error) from error

    def abandon_temp_file(self) -> None:
        handle = self._temp_handle
        path = self._temp_path
        self._temp_handle = None
        self._temp_path = None
        if handle is not None:
            try:
                _close_spill(handle)
            except OSError:
                pass
        _remove_spill(path)

    def _append_decoded(self, text: str) -> None:
        if text == "":
            return
        encoded_bytes = _utf8_byte_length(text)
        self._total_decoded_bytes += encoded_bytes
        self._tail_text += text
        self._tail_bytes += encoded_bytes
        if self._tail_bytes > self._max_rolling_bytes * 2:
            self._trim_tail()
        newlines = 0
        last_newline = -1
        cursor = text.find("\n")
        while cursor != -1:
            newlines += 1
            last_newline = cursor
            cursor = text.find("\n", cursor + 1)
        if newlines == 0:
            self._current_line_bytes += encoded_bytes
            self._has_open_line = True
        else:
            self._completed_lines += newlines
            tail = text[last_newline + 1 :]
            self._current_line_bytes = _utf8_byte_length(tail)
            self._has_open_line = tail != ""
        self._total_lines = self._completed_lines + (1 if self._has_open_line else 0)

    def _trim_tail(self) -> None:
        buffer = self._tail_text.encode("utf-8")
        if len(buffer) <= self._max_rolling_bytes:
            self._tail_bytes = len(buffer)
            return
        start = len(buffer) - self._max_rolling_bytes
        while start < len(buffer) and buffer[start] & 0xC0 == 0x80:
            start += 1
        if start != 0:
            self._tail_starts_at_line_boundary = buffer[start - 1] == 0x0A
        self._tail_text = buffer[start:].decode("utf-8")
        self._tail_bytes = _utf8_byte_length(self._tail_text)

    def _snapshot_text(self) -> str:
        if self._tail_starts_at_line_boundary:
            return self._tail_text
        first_newline = self._tail_text.find("\n")
        return self._tail_text if first_newline == -1 else self._tail_text[first_newline + 1 :]

    def _should_use_temp_file(self) -> bool:
        return (
            self._total_raw_bytes > self._max_bytes
            or self._total_decoded_bytes > self._max_bytes
            or self._total_lines > self._max_lines
        )

    def _ensure_temp_file(self) -> None:
        if self._temp_path is not None:
            return
        while True:
            path = _spill_path()
            try:
                handle = _open_spill(path)
            except OSError as error:
                if error.errno == errno.EEXIST:
                    continue
                raise _OutputFault("spill_create", error) from error
            self._temp_path = path
            self._temp_handle = handle
            break
        for chunk in self._raw_chunks:
            self._write_current(chunk)
        self._raw_chunks = []

    def _write_current(self, data: bytes) -> None:
        if not data:
            return
        assert self._temp_handle is not None
        try:
            _write_spill(self._temp_handle, data)
        except OSError as error:
            raise _OutputFault("spill_write", error) from error


class _UpdateThrottle:
    def __init__(
        self,
        on_update: Any,
        accumulator: OutputAccumulator,
        signal: AbortSignal,
    ) -> None:
        self._on_update = on_update
        self._accumulator = accumulator
        self._signal = signal
        self._dirty = False
        self._last = 0.0
        self._handle: asyncio.TimerHandle | None = None
        self._loop = asyncio.get_running_loop()
        self._open = True
        self.exposed_spill = False
        self.fault: _OutputFault | None = None

    def close_admission(self) -> None:
        self._open = False
        self._cancel()

    def mark(self) -> None:
        if not self._open or self._signal.aborted:
            return
        self._dirty = True
        delay = _BASH_UPDATE_THROTTLE_S - (self._loop.time() - self._last)
        if delay <= 0:
            self._cancel()
            self._emit()
            return
        if self._handle is None:
            self._handle = self._loop.call_later(delay, self._emit)

    def flush(self) -> None:
        self._cancel()
        if self._dirty and self._open and not self._signal.aborted:
            self._emit()

    def _cancel(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def _emit(self) -> None:
        self._handle = None
        if not self._open or self._signal.aborted or not self._dirty:
            return
        self._dirty = False
        self._last = self._loop.time()
        try:
            snapshot = self._accumulator.snapshot(persist_if_truncated=True)
        except _OutputFault as fault:
            self.close_admission()
            self.fault = fault
            return
        details: JSONValue = None
        if snapshot.truncation["truncated"]:
            details = {
                "truncation": _truncation_json(snapshot.truncation),
                "fullOutputPath": snapshot.full_output_path,
            }
            self.exposed_spill = True
        self._on_update(
            AgentToolResult(
                content=(TextContent(text=snapshot.content),),
                details=details,
                terminate=None,
            )
        )


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
            raise LifecycleError(
                "cleanup",
                "Agent loop cleanup failed",
                causes=(error,),
            ) from error
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
            raise LifecycleError(
                "cleanup",
                "Agent loop cleanup failed",
                causes=(cleanup_error,),
            ) from cleanup_error
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
            raise LifecycleError(
                "cleanup",
                "Agent loop cleanup failed",
                causes=(cleanup_error,),
            ) from cleanup_error
        if _recognized_output_error(fault.error):
            return _infra_from_fault(
                accumulator.snapshot(persist_if_truncated=False), fault
            )
        raise LifecycleError(
            "cleanup",
            "Agent loop cleanup failed",
            causes=(fault.error,),
        ) from fault.error
    if cleanup_error is not None:
        raise LifecycleError(
            "cleanup",
            "Agent loop cleanup failed",
            causes=(cleanup_error,),
        ) from cleanup_error
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
