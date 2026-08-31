from __future__ import annotations

import asyncio
import os
import signal
import string
import sys
import termios
import tty
from dataclasses import dataclass
from typing import Any, BinaryIO, Literal

import icu  # type: ignore[import-untyped]
import wcwidth
from oh_my_llm import AssistantMessage, LifecycleError, TextContent
from oh_my_llm._canonical import encodeCanonical

from ._prompt_resources import _es_trim
from ._session import AgentSession, AgentSessionEvent


_BIDI = frozenset(
    {
        "\u061c",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    }
)
_FIELD_EXTRA = frozenset({'\t', '\n', '"', '\\'})
_UTF8_ERROR = "interactive input is not UTF-8"
_WIDTH_ERROR = "interactive input width is invalid"
_WIDTH_UNICODE = "17.0.0"
_ICU_ROOT = icu.Locale.getRoot()
_INTERRUPT = b"\x03"
_SIGNAL_STATUS: dict[signal.Signals, int] = {
    signal.SIGINT: 130,
    signal.SIGHUP: 129,
    signal.SIGTERM: 143,
}

_TerminalClassification = Literal[
    "completed", "model_error", "cancelled", "unconfirmed"
]


class _Shutdown:
    def __init__(self, *, print_mode: bool) -> None:
        self.print_mode = print_mode
        self.signum: signal.Signals | None = None
        self.session: AgentSession | None = None
        self._terminating = asyncio.Event()
        self._interrupt = asyncio.Event()

    def request(self, signum: signal.Signals) -> None:
        if self.signum is not None:
            self._abort()
            return
        session = self.session
        if signum == signal.SIGINT and not self.print_mode and session is not None:
            self._interrupt.set()
            if not session.isIdle:
                self._abort()
            return
        self.signum = signum
        self._terminating.set()
        self._abort()

    def _abort(self) -> None:
        session = self.session
        if session is None:
            return
        task = asyncio.create_task(session.abort())
        task.add_done_callback(_consume_task_exception)

    async def wait(self) -> None:
        await self._terminating.wait()

    async def wait_interrupt(self) -> None:
        await self._interrupt.wait()

    def clear_interrupt(self) -> None:
        self._interrupt.clear()


def _consume_task_exception(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        task.exception()


def encode_body(value: str) -> str:
    return "".join(_escape(character, field=False) for character in value)


def encode_field(value: str) -> str:
    return "".join(_escape(character, field=True) for character in value)


def encode_json(value: object) -> str:
    return encode_body(encodeCanonical(value).decode("utf-8"))


def write_stdout(data: bytes | str) -> None:
    _write(sys.stdout.buffer, _payload(data))


def write_stderr(data: bytes | str) -> bool:
    try:
        _write(sys.stderr.buffer, _payload(data))
        return True
    except OSError:
        return False


def write_usage(reason: str) -> None:
    write_stderr(f"usage: {encode_field(reason)}\n")


def write_error(message: str) -> bool:
    return write_stderr(f"error: {encode_field(message)}\n")


def write_lifecycle(code: str, message: str) -> None:
    write_stderr(f"lifecycle {encode_field(code)}: {encode_field(message)}\n")


def write_cleanup(message: str) -> None:
    write_stderr(f"lifecycle cleanup: {encode_field(message)}\n")


def write_identity(session_id: str, kind: str) -> None:
    _write(sys.stderr.buffer, _payload(f"session {encode_field(session_id)} {kind}\n"))


def write_identity_stdout(session_id: str, kind: str) -> None:
    write_stdout(f"session {encode_field(session_id)} {kind}\n")


def _escape(character: str, *, field: bool) -> str:
    code = ord(character)
    if not field and character in {'\t', '\n'}:
        return character
    if (
        code < 32
        or code == 127
        or 128 <= code <= 159
        or character in _BIDI
        or (field and character in _FIELD_EXTRA)
    ):
        return f"\\u{code:04X}"
    return character


def _payload(data: bytes | str) -> bytes:
    if isinstance(data, str):
        return data.encode("utf-8")
    return data


def _write(buffer: BinaryIO, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = buffer.write(payload[offset:])
        if written is None or written <= 0:
            raise OSError("terminal write made no progress")
        offset += written
    buffer.flush()


def _acquire_end_of_line_editing(fd: int) -> tuple[list[Any], bool]:
    saved = termios.tcgetattr(fd)
    echo = bool(saved[3] & termios.ECHO)
    tty.setcbreak(fd, termios.TCSANOW)
    return saved, echo


def _restore_tty(fd: int, saved: list[Any]) -> None:
    termios.tcsetattr(fd, termios.TCSANOW, saved)


async def _drive_interactive(
    session: AgentSession, shutdown: _Shutdown
) -> int:
    return await _InteractiveTerminalAdapter(session, shutdown).run()


class _InteractiveTerminalAdapter:
    __slots__ = (
        "_busy",
        "_editor",
        "_input_failure",
        "_lines",
        "_loop",
        "_queued_nonempty",
        "_rendered_text",
        "_session",
        "_shutdown",
        "_stdin_fd",
    )

    def __init__(
        self, session: AgentSession, shutdown: _Shutdown
    ) -> None:
        self._session = session
        self._shutdown = shutdown
        self._rendered_text = ""
        self._busy = False
        self._queued_nonempty = False
        self._input_failure: Exception | None = None
        self._loop: asyncio.AbstractEventLoop
        self._lines: asyncio.Queue[bytes | Exception]
        self._stdin_fd: int
        self._editor: _CommandInput

    async def run(self) -> int:
        self._session.subscribe(self._render)
        self._loop = asyncio.get_running_loop()
        self._lines = asyncio.Queue()
        self._stdin_fd = sys.stdin.buffer.fileno()
        saved_tty, echo = _acquire_end_of_line_editing(self._stdin_fd)
        output_fd = sys.stdout.buffer.fileno()
        self._editor = _CommandInput(echo=echo, output_fd=output_fd)

        reader_registered = False
        winch_registered = False
        try:
            self._editor.begin()
            self._loop.add_reader(self._stdin_fd, self._input_ready)
            reader_registered = True
            try:
                self._loop.add_signal_handler(signal.SIGWINCH, self._on_resize)
                winch_registered = True
            except (NotImplementedError, RuntimeError):
                signal.signal(
                    signal.SIGWINCH,
                    lambda _num, _frame: self._on_resize(),
                )
                winch_registered = True
            return await self._run_editor()
        except LifecycleError as error:
            write_lifecycle(error.code, str(error))
            return 1
        except OSError:
            write_stderr("I/O error\n")
            return 1
        except ValueError as error:
            write_error(_public_value_message(error))
            return 1
        except Exception:
            write_stderr("internal error\n")
            return 1
        finally:
            if reader_registered:
                self._loop.remove_reader(self._stdin_fd)
            if winch_registered:
                try:
                    self._loop.remove_signal_handler(signal.SIGWINCH)
                except (NotImplementedError, RuntimeError, OSError):
                    signal.signal(signal.SIGWINCH, signal.SIG_DFL)
            try:
                _restore_tty(self._stdin_fd, saved_tty)
            except OSError:
                if self._shutdown.signum is not None:
                    write_cleanup("terminal restore failed")
                else:
                    write_stderr("I/O error\n")
                    return 1

    async def _run_editor(self) -> int:
        while True:
            if self._shutdown.signum is not None:
                return self._shutdown_status()
            item = await self._wait_line_or_control()
            if item == "terminate":
                return self._shutdown_status()
            if item == "interrupt" or item == _INTERRUPT:
                self._editor.handle_idle_interrupt()
                continue
            if isinstance(item, Exception):
                raise item
            if item == b"":
                return 0
            text = _decode_interactive_line(
                item, "interactive input is not UTF-8"
            )
            prompt = _es_trim(text)
            if not prompt:
                continue
            self._queued_nonempty = False
            prior_messages = self._session.messages
            prior_entries = self._session.sessionManager.getEntries()
            self._busy = True
            prompt_task = asyncio.create_task(self._session.prompt(prompt))
            try:
                try:
                    await self._join_prompt(prompt_task)
                except ValueError as error:
                    if (
                        self._session.messages != prior_messages
                        or self._session.sessionManager.getEntries() != prior_entries
                        or not self._session.isIdle
                    ):
                        raise RuntimeError(
                            "AgentSession ValueError changed admitted state"
                        ) from error
                    if not write_error(_public_value_message(error)):
                        raise OSError("interactive diagnostic write failed")
                    continue
                if self._input_failure is not None:
                    raise self._input_failure
                classification, _payload = _session_terminal(self._session)
                if self._shutdown.signum is not None:
                    if classification == "cancelled":
                        write_stdout("run cancelled\n")
                    return self._shutdown_status()
                if classification == "unconfirmed":
                    raise RuntimeError("AgentSession prompt settlement is unconfirmed")
                write_stdout(f"run {classification}\n")
            finally:
                self._busy = False
                self._editor.reopen_idle()

    async def _render(self, event: AgentSessionEvent) -> None:
        if self._input_failure is not None:
            return
        if isinstance(event, AgentSessionEvent.MessageStart) and isinstance(
            event.message, AssistantMessage
        ):
            self._rendered_text = ""
            write_stdout("assistant start\n")
        elif isinstance(event, AgentSessionEvent.MessageUpdate):
            text = "".join(
                block.text
                for block in event.message.content
                if isinstance(block, TextContent)
            )
            if not text.startswith(self._rendered_text):
                raise RuntimeError("Assistant Text update is not cumulative")
            suffix = text[len(self._rendered_text) :]
            if suffix:
                write_stdout(encode_body(suffix))
            self._rendered_text = text
        elif isinstance(event, AgentSessionEvent.MessageEnd) and isinstance(
            event.message, AssistantMessage
        ):
            if not self._rendered_text.endswith("\n"):
                write_stdout("\n")
            write_stdout("assistant end\n")
        elif isinstance(event, AgentSessionEvent.ToolExecutionStart):
            write_stdout(
                "tool start "
                f'name="{encode_field(event.toolName)}" '
                f'id="{encode_field(event.toolCallId)}" '
                f"arguments={encode_json(event.args)}\n"
            )
        elif isinstance(event, AgentSessionEvent.ToolExecutionEnd):
            kind = "failure" if event.isError else "outcome"
            result = {
                "content": tuple(item.text for item in event.result.content),
                "details": event.result.details,
                "terminate": event.result.terminate,
            }
            write_stdout(
                f"tool {kind} "
                f'name="{encode_field(event.toolName)}" '
                f'id="{encode_field(event.toolCallId)}" '
                f"result={encode_json(result)}\n"
            )

    def _fail_input(self, error: Exception) -> None:
        self._loop.remove_reader(self._stdin_fd)
        self._input_failure = error
        if self._busy:
            asyncio.create_task(self._session.abort())
            return
        self._lines.put_nowait(error)

    def _on_resize(self) -> None:
        if self._input_failure is not None:
            return
        try:
            self._editor.resize()
        except Exception as error:
            self._fail_input(error)

    def _deliver(self, raw: bytes) -> None:
        if raw == b"":
            self._loop.remove_reader(self._stdin_fd)
            if self._busy:
                asyncio.create_task(self._session.abort())
            self._lines.put_nowait(raw)
            return
        if raw == _INTERRUPT:
            if self._busy:
                asyncio.create_task(self._session.abort())
                return
            self._lines.put_nowait(raw)
            return
        try:
            text = _decode_interactive_line(
                raw, "interactive input is not UTF-8"
            )
        except OSError as error:
            self._fail_input(error)
            return
        prompt = _es_trim(text)
        if self._busy or self._queued_nonempty:
            if prompt and not write_stderr(
                "busy: Product Session has an active Run\n"
            ):
                self._fail_input(OSError("interactive diagnostic write failed"))
            return
        self._queued_nonempty = bool(prompt)
        self._lines.put_nowait(raw)

    def _input_ready(self) -> None:
        try:
            raw = os.read(self._stdin_fd, 4096)
        except OSError as error:
            self._fail_input(error)
            return
        if raw == b"":
            self._deliver(b"")
            return
        try:
            completed = self._editor.feed(raw)
        except Exception as error:
            self._fail_input(error)
            return
        for item in completed:
            self._deliver(item)
            if item == b"":
                return

    async def _wait_line_or_control(
        self,
    ) -> bytes | Exception | Literal["interrupt", "terminate"]:
        if self._shutdown.signum is not None:
            return "terminate"
        line = asyncio.create_task(self._lines.get())
        interrupt = asyncio.create_task(self._shutdown.wait_interrupt())
        terminate = asyncio.create_task(self._shutdown.wait())
        done, pending = await asyncio.wait(
            {line, interrupt, terminate},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if terminate in done or self._shutdown.signum is not None:
            return "terminate"
        if line in done:
            return line.result()
        self._shutdown.clear_interrupt()
        return "interrupt"

    async def _join_prompt(self, prompt_task: asyncio.Task[None]) -> None:
        while not prompt_task.done():
            if self._shutdown.signum is not None:
                await prompt_task
                return
            interrupt = asyncio.create_task(self._shutdown.wait_interrupt())
            terminate = asyncio.create_task(self._shutdown.wait())
            done, pending = await asyncio.wait(
                {prompt_task, interrupt, terminate},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                if task is not prompt_task:
                    task.cancel()
            await asyncio.gather(
                *[task for task in pending if task is not prompt_task],
                return_exceptions=True,
            )
            if interrupt in done:
                self._shutdown.clear_interrupt()
                await self._session.abort()
        await prompt_task

    def _shutdown_status(self) -> int:
        assert self._shutdown.signum is not None
        return _SIGNAL_STATUS[self._shutdown.signum]


def _decode_interactive_line(raw: bytes, message: str) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise OSError(message) from error
    if text.endswith("\n"):
        text = text[:-1]
    if text.endswith("\r"):
        text = text[:-1]
    return text


def _session_terminal(
    session: AgentSession,
) -> tuple[_TerminalClassification, bytes | None]:
    messages = session.messages
    assistants = [
        message for message in messages if isinstance(message, AssistantMessage)
    ]
    if not assistants:
        return "unconfirmed", None
    terminal = assistants[-1]
    body = "".join(
        block.text for block in terminal.content if isinstance(block, TextContent)
    )
    encoded = encode_body(body).encode("utf-8")
    if terminal.stopReason in {"stop", "length"}:
        return "completed", encoded
    if terminal.stopReason == "error":
        return "model_error", encoded
    if terminal.stopReason == "aborted":
        return "cancelled", encoded
    return "unconfirmed", None


def _public_value_message(error: ValueError) -> str:
    message = str(error)
    if "Session file is not a valid omh session" in message:
        return "session file is invalid"
    if message.startswith("NewSessionOptions.id"):
        return "invalid session id"
    if message.startswith("--cwd") or message.startswith(
        "CreateAgentSessionOptions.cwd"
    ):
        return "invalid cwd"
    return "invalid value"


@dataclass(frozen=True, slots=True)
class _VisualRow:
    text: str
    start: int
    cells: int


class _CommandInput:
    __slots__ = (
        "_echo",
        "_output_fd",
        "_text",
        "_cursor",
        "_busy_text",
        "_pending",
        "_consumed_cr",
        "_mode",
        "_paused",
        "_width",
        "_height",
        "_origin",
        "_owned_rows",
        "_grapheme_iter",
        "_word_iter",
    )

    def __init__(self, *, echo: bool, output_fd: int) -> None:
        self._echo = echo
        self._output_fd = output_fd
        self._text = ""
        self._cursor = 0
        self._busy_text = ""
        self._pending = bytearray()
        self._consumed_cr = False
        self._mode: Literal["idle", "busy"] = "idle"
        self._paused = False
        self._width = 0
        self._height = 0
        self._origin = 0
        self._owned_rows = 0
        self._grapheme_iter = icu.BreakIterator.createCharacterInstance(_ICU_ROOT)
        self._word_iter = icu.BreakIterator.createWordInstance(_ICU_ROOT)

    def begin(self) -> None:
        self._read_size()
        self._paint()

    def resize(self) -> None:
        width, height = self._query_size()
        if width < 3 or height < 1:
            self._enter_pause()
            return
        self._width = width
        self._height = height
        recovering = self._paused
        self._paused = False
        if self._mode == "idle" and (recovering or self._echo or self._owned_rows):
            self._paint()

    def handle_idle_interrupt(self) -> None:
        self._text = ""
        self._cursor = 0
        self._pending.clear()
        self._consumed_cr = False
        self._origin = 0
        if not self._paused:
            self._paint()

    def reopen_idle(self) -> None:
        self._busy_text = ""
        self._pending.clear()
        self._text = ""
        self._cursor = 0
        self._origin = 0
        self._owned_rows = 0
        self._mode = "idle"
        if not self._paused:
            self._paint()

    def feed(self, data: bytes) -> list[bytes]:
        self._pending.extend(data)
        completed: list[bytes] = []
        while self._pending:
            first = self._pending[0]
            if first == 0x1B:
                action = _take_escape(self._pending)
                if action is None:
                    break
                if action == "reprocess":
                    continue
                event = self._navigation(action)
                if event is not None:
                    completed.append(event)
                continue
            if first < 0x20 or first == 0x7F:
                del self._pending[0]
                event = self._control(first)
                if event is not None:
                    completed.append(event)
                    if event == b"":
                        return completed
                continue
            if self._paused:
                self._consume_paused_utf8()
                continue
            character = _take_complete_character(self._pending)
            if character is None:
                break
            self._note_raw(None)
            self._insert(character)
        return completed

    def _control(self, first: int) -> bytes | None:
        if first == 0x0A and self._consumed_cr:
            self._consumed_cr = False
            return None
        if first == 0x0D:
            self._consumed_cr = True
            return self._enter()
        self._consumed_cr = False
        if first == 0x0A:
            return self._enter()
        if first == 0x03:
            return self._interrupt()
        if first in {0x08, 0x7F}:
            self._backward_delete()
            return None
        if first == 0x04:
            return self._ctrl_d()
        return None

    def _navigation(self, action: str) -> bytes | None:
        self._note_raw(None)
        if self._paused or self._mode == "busy" or not action:
            return None
        before = self._cursor
        if action == "left":
            self._cursor = _move_grapheme(
                self._text, self._cursor, -1, self._grapheme_iter
            )
        elif action == "right":
            self._cursor = _move_grapheme(
                self._text, self._cursor, 1, self._grapheme_iter
            )
        elif action == "word_left":
            self._cursor = _word_move(
                self._text, self._cursor, -1, self._word_iter
            )
        elif action == "word_right":
            self._cursor = _word_move(
                self._text, self._cursor, 1, self._word_iter
            )
        elif action == "forward_delete":
            self._forward_delete()
            return None
        else:
            return None
        if self._cursor != before and self._echo:
            self._paint()
        return None

    def _insert(self, character: str) -> None:
        if self._mode == "busy":
            self._busy_text += character
            return
        self._text = self._text[: self._cursor] + character + self._text[self._cursor :]
        self._cursor += len(character)
        if self._echo:
            self._paint()

    def _backward_delete(self) -> None:
        if self._paused:
            return
        if self._mode == "busy":
            if not self._busy_text:
                return
            start = _move_grapheme(
                self._busy_text, len(self._busy_text), -1, self._grapheme_iter
            )
            self._busy_text = self._busy_text[:start]
            return
        if self._cursor == 0:
            return
        start = _move_grapheme(self._text, self._cursor, -1, self._grapheme_iter)
        self._text = self._text[:start] + self._text[self._cursor :]
        self._cursor = start
        if self._echo:
            self._paint()

    def _forward_delete(self) -> None:
        if self._paused or self._mode == "busy":
            return
        if self._cursor >= len(self._text):
            return
        end = _move_grapheme(self._text, self._cursor, 1, self._grapheme_iter)
        self._text = self._text[: self._cursor] + self._text[end:]
        if self._echo:
            self._paint()

    def _enter(self) -> bytes | None:
        if self._paused:
            return None
        if self._mode == "busy":
            line = self._busy_text
            self._busy_text = ""
            if not _es_trim(line):
                return None
            return (line + "\n").encode("utf-8")
        line = self._text
        if not _es_trim(line):
            self._text = ""
            self._cursor = 0
            self._origin = 0
            self._paint()
            return None
        self._finalize()
        self._text = ""
        self._cursor = 0
        self._origin = 0
        self._busy_text = ""
        self._mode = "busy"
        return (line + "\n").encode("utf-8")

    def _ctrl_d(self) -> bytes | None:
        if self._mode == "busy":
            return b"" if not self._busy_text else None
        if self._paused:
            return b"" if not self._text else None
        if not self._text:
            return b""
        self._forward_delete()
        return None

    def _interrupt(self) -> bytes | None:
        if self._mode == "busy":
            return _INTERRUPT
        self.handle_idle_interrupt()
        return None

    def _consume_paused_utf8(self) -> None:
        first = self._pending[0]
        length = _utf8_length(first)
        if length is None or any(
            (byte & 0xC0) != 0x80 for byte in self._pending[1 : min(len(self._pending), length)]
        ):
            del self._pending[0]
            self._note_raw(None)
            return
        if len(self._pending) < length:
            self._pending.clear()
            self._note_raw(None)
            return
        try:
            bytes(self._pending[:length]).decode("utf-8")
        except UnicodeDecodeError:
            del self._pending[0]
            self._note_raw(None)
            return
        del self._pending[:length]
        self._note_raw(None)

    def _note_raw(self, first: int | None) -> None:
        if first == 0x0D:
            return
        if first == 0x0A and self._consumed_cr:
            return
        self._consumed_cr = False

    def _enter_pause(self) -> None:
        self._paused = True
        self._pending.clear()

    def _read_size(self) -> None:
        width, height = self._query_size()
        if width < 3 or height < 1:
            raise OSError("terminal geometry is unusable")
        self._width = width
        self._height = height
        self._paused = False

    def _query_size(self) -> tuple[int, int]:
        size = os.get_terminal_size(self._output_fd)
        return size.columns, size.lines

    def _viewport_cap(self) -> int:
        return min(self._height, max(5, self._height * 30 // 100))

    def _layout(self) -> list[_VisualRow]:
        if not self._text:
            return [_VisualRow("", 0, 0)]
        rows: list[_VisualRow] = []
        capacity = self._width - 3
        start = 0
        text = ""
        cells = 0
        for begin, end in _grapheme_bounds(self._text, self._grapheme_iter):
            grapheme = self._text[begin:end]
            width = _grapheme_width(grapheme)
            overflow = cells + width > capacity
            if text and overflow:
                rows.append(_VisualRow(text, start, cells))
                start = begin
                text = grapheme
                cells = width
                capacity = self._width - 1
            elif not text and overflow:
                rows.append(_VisualRow("", start, 0))
                start = begin
                text = grapheme
                cells = width
                capacity = self._width - 1
                if width > capacity:
                    raise RuntimeError(_WIDTH_ERROR)
            else:
                text += grapheme
                cells += width
        rows.append(_VisualRow(text, start, cells))
        return rows

    def _cursor_row_col(self, rows: list[_VisualRow]) -> tuple[int, int]:
        for index, row in enumerate(rows):
            end = rows[index + 1].start if index + 1 < len(rows) else len(self._text)
            if self._cursor > end:
                continue
            if self._cursor < end or index == len(rows) - 1:
                prefix = self._width_of(self._text[row.start : self._cursor])
                prompt = 2 if index == 0 else 0
                return index, prompt + prefix
        return 0, 2

    def _width_of(self, text: str) -> int:
        total = 0
        for begin, end in _grapheme_bounds(text, self._grapheme_iter):
            total += _grapheme_width(text[begin:end])
        return total

    def _follow(self, cursor_row: int, row_count: int) -> int:
        cap = self._viewport_cap()
        if row_count <= cap:
            self._origin = 0
            return 0
        origin = self._origin
        if cursor_row < origin:
            origin = cursor_row
        elif cursor_row >= origin + cap:
            origin = cursor_row - cap + 1
        origin = max(0, min(origin, row_count - cap))
        self._origin = origin
        return origin

    def _home(self) -> None:
        if self._owned_rows > 1:
            write_stdout(f"\x1b[{self._owned_rows - 1}A")
        if self._owned_rows >= 1:
            write_stdout("\r")

    def _paint(self) -> None:
        if self._paused or self._mode == "busy":
            return
        if not self._text and self._owned_rows == 0:
            write_stdout("> ")
            self._owned_rows = 1
            return
        self._home()
        if not self._echo:
            write_stdout("> \x1b[K")
            extra = self._owned_rows - 1
            if extra > 0:
                for _ in range(extra):
                    write_stdout("\n\x1b[K")
                write_stdout(f"\x1b[{extra}A")
            self._owned_rows = 1
            return
        rows = self._layout()
        cursor_row, cursor_col = self._cursor_row_col(rows)
        origin = self._follow(cursor_row, len(rows))
        visible = rows[origin : origin + self._viewport_cap()]
        for index, row in enumerate(visible):
            prefix = "> " if origin == 0 and index == 0 else ""
            write_stdout(prefix + row.text)
            write_stdout("\x1b[K")
            if index < len(visible) - 1:
                write_stdout("\n")
        extra = self._owned_rows - len(visible)
        if extra > 0:
            for _ in range(extra):
                write_stdout("\n\x1b[K")
            write_stdout(f"\x1b[{extra}A")
        self._owned_rows = max(len(visible), 1)
        last = len(visible) - 1
        target = cursor_row - origin
        up = last - target
        if up > 0:
            write_stdout(f"\x1b[{up}A")
        write_stdout(f"\x1b[{cursor_col + 1}G")

    def _finalize(self) -> None:
        if not self._echo:
            write_stdout("\n")
            self._owned_rows = 0
            return
        rows = self._layout()
        cursor_row, _cursor_col = self._cursor_row_col(rows)
        origin = self._origin if len(rows) > self._viewport_cap() else 0
        visible_count = min(len(rows) - origin, self._viewport_cap())
        last_row = origin + visible_count - 1
        down = last_row - cursor_row
        if down > 0:
            write_stdout(f"\x1b[{down}B")
        write_stdout("\n")
        self._owned_rows = 0


def _grapheme_bounds(text: str, iterator: icu.BreakIterator) -> list[tuple[int, int]]:
    return [(start, end) for start, end, _status in _segments(text, iterator)]


def _segments(
    text: str, iterator: icu.BreakIterator
) -> list[tuple[int, int, int]]:
    utf16_to_cp = {0: 0}
    units = 0
    for index, character in enumerate(text, 1):
        units += len(character.encode("utf-16-le")) // 2
        utf16_to_cp[units] = index
    iterator.setText(text)
    start = 0
    result: list[tuple[int, int, int]] = []
    for end_units in iterator:
        end = utf16_to_cp[end_units]
        result.append((start, end, iterator.getRuleStatus()))
        start = end
    return result


def _move_grapheme(
    text: str, cursor: int, direction: int, iterator: icu.BreakIterator
) -> int:
    bounds = [0, *[end for _start, end in _grapheme_bounds(text, iterator)]]
    if direction < 0:
        previous = [bound for bound in bounds if bound < cursor]
        return previous[-1] if previous else 0
    following = [bound for bound in bounds if bound > cursor]
    return following[0] if following else len(text)


def _word_move(
    text: str, cursor: int, direction: int, iterator: icu.BreakIterator
) -> int:
    side = text[cursor:] if direction == 1 else text[:cursor]
    spans = _segments(side, iterator)
    if direction == -1:
        spans.reverse()
    consumed = 0
    while spans and side[spans[0][0] : spans[0][1]].isspace():
        start, end, _status = spans.pop(0)
        consumed += end - start
    if not spans:
        return cursor + direction * consumed
    start, end, status = spans[0]
    fragment = side[start:end]
    if status >= 100:
        punctuation = [
            index
            for index, character in enumerate(fragment)
            if character in string.punctuation
        ]
        if punctuation:
            consumed += (
                punctuation[0]
                if direction == 1
                else len(fragment) - punctuation[-1] - 1
            )
        else:
            consumed += len(fragment)
    else:
        for start, end, status in spans:
            if status >= 100 or side[start:end].isspace():
                break
            consumed += end - start
    return cursor + direction * consumed


def _grapheme_width(grapheme: str) -> int:
    cells = wcwidth.wcswidth(
        grapheme, ambiguous_width=1, unicode_version=_WIDTH_UNICODE
    )
    if cells < 0:
        raise OSError(_WIDTH_ERROR)
    return cells


def _take_escape(buffer: bytearray) -> str | None:
    if len(buffer) == 1:
        return None
    second = buffer[1]
    if second == 0x5B:
        return _take_csi(buffer)
    if second == 0x4F:
        if len(buffer) == 2:
            return None
        final = buffer[2]
        if not (0x20 <= final <= 0x7E):
            del buffer[:2]
            return "reprocess"
        del buffer[:3]
        if final == 0x44:
            return "left"
        if final == 0x43:
            return "right"
        return ""
    if second in {0x62, 0x66}:
        del buffer[:2]
        return "word_left" if second == 0x62 else "word_right"
    del buffer[0]
    return "reprocess"


def _take_csi(buffer: bytearray) -> str | None:
    index = 2
    while index < len(buffer) and 0x30 <= buffer[index] <= 0x3F:
        index += 1
    while index < len(buffer) and 0x20 <= buffer[index] <= 0x2F:
        index += 1
    if index >= len(buffer):
        return None
    if 0x40 <= buffer[index] <= 0x7E:
        parameter = bytes(buffer[2:index])
        final = buffer[index]
        del buffer[: index + 1]
        return _csi_action(parameter, final)
    del buffer[:index]
    return ""


def _csi_action(parameter: bytes, final: int) -> str:
    if parameter == b"" and final == 0x44:
        return "left"
    if parameter == b"" and final == 0x43:
        return "right"
    if parameter == b"1;3" and final == 0x44:
        return "word_left"
    if parameter == b"1;3" and final == 0x43:
        return "word_right"
    if parameter == b"3" and final == 0x7E:
        return "forward_delete"
    return ""


def _take_complete_character(buffer: bytearray) -> str | None:
    length = _utf8_length(buffer[0])
    if length is None:
        raise OSError(_UTF8_ERROR)
    if len(buffer) < length:
        if any((byte & 0xC0) != 0x80 for byte in buffer[1:]):
            raise OSError(_UTF8_ERROR)
        return None
    raw = bytes(buffer[:length])
    try:
        character = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise OSError(_UTF8_ERROR) from error
    del buffer[:length]
    return character


def _utf8_length(lead: int) -> int | None:
    if lead < 0x80:
        return 1
    if 0xC2 <= lead <= 0xDF:
        return 2
    if 0xE0 <= lead <= 0xEF:
        return 3
    if 0xF0 <= lead <= 0xF4:
        return 4
    return None
