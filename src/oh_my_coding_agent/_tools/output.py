from __future__ import annotations

import asyncio
import codecs
import errno
import os
import tempfile
from typing import Any

from oh_my_core import AgentToolResult
from oh_my_core._tools import _AgentToolOwnerCleanupError
from oh_my_llm import AbortSignal, JSONValue, TextContent

from .truncation import (
    TruncationFacts,
    _MAX_OUTPUT_BYTES,
    _MAX_OUTPUT_LINES,
    _truncation_json,
    _utf8_byte_length,
    truncate_tail,
)


_BASH_UPDATE_THROTTLE_S = 0.1


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
    try:
        os.fchmod(handle, 0o600)
    except BaseException as primary:
        cleanup_errors: list[OSError] = []
        try:
            os.close(handle)
        except OSError as error:
            cleanup_errors.append(error)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            cause: BaseException = (
                cleanup_errors[0]
                if len(cleanup_errors) == 1
                else ExceptionGroup("Bash spill cleanup failed", cleanup_errors)
            )
            raise _AgentToolOwnerCleanupError(cause) from primary
        raise
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
        delay = _BASH_UPDATE_THROTTLE_S - (_throttle_time(self._loop) - self._last)
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
        self._last = _throttle_time(self._loop)
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


def _throttle_time(loop: asyncio.AbstractEventLoop) -> float:
    return loop.time()
