from __future__ import annotations

import asyncio
import errno
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
from oh_my_llm import AssistantMessage, LifecycleError, TextContent, UserMessage
from oh_my_llm._canonical import encodeCanonical

from ._project_rules import _project_root
from ._project_trust import (
    trust_path_has_parent,
    update_default_project_trust,
    update_project_trust,
)
from ._prompt_resources import _es_trim
from ._resource_state import (
    ExtensionDiagnostic,
    ExtensionDiagnosticLifecycle,
    ExtensionDiagnosticLoad,
    ProjectResourceReloadError,
    ProjectResourceReloadResult,
    ProjectResourceState,
    PromptResourceResolution,
    ResourceAdmissionError,
    ResourceResolutionReport,
    TrustPolicyError,
)
from ._session import AgentSession, AgentSessionEvent, PendingMessages


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
_TRUST_MODAL_WITH_PARENT = (
    "Save project trust for future Sessions:\n"
    "[t] Trust\n"
    "[p] Trust parent\n"
    "[n] Do not trust\n"
    "[c] Cancel\n"
    "Select t/p/n/c:"
)
_TRUST_MODAL_ROOT = (
    "Save project trust for future Sessions:\n"
    "[t] Trust\n"
    "[n] Do not trust\n"
    "[c] Cancel\n"
    "Select t/n/c:"
)
_SETTINGS_MODAL = (
    "Default project trust for future Sessions:\n"
    "[a] Ask\n"
    "[y] Always\n"
    "[n] Never\n"
    "[c] Cancel\n"
    "Select a/y/n/c:"
)

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


def _user_text(message: UserMessage) -> str:
    content = message.content
    if type(content) is str:
        return content
    return "".join(
        block.text
        for block in content
        if isinstance(block, TextContent)
    )


def _run_classification(
    messages: tuple[Any, ...],
) -> _TerminalClassification:
    assistants = [
        message
        for message in messages
        if isinstance(message, AssistantMessage)
    ]
    if not assistants:
        return "unconfirmed"
    terminal = assistants[-1]
    if terminal.stopReason in {"stop", "length"}:
        return "completed"
    if terminal.stopReason == "error":
        return "model_error"
    if terminal.stopReason == "aborted":
        return "cancelled"
    return "unconfirmed"


def _resources_line(state: ProjectResourceState) -> str:
    if state.status == "indeterminate":
        return "Resources: indeterminate"
    discovery = state.report.discovery
    return f"Resources: current, discovery {discovery}"


def _terminal_relative_path(path: str, root: str) -> str:
    lexical = path.replace(os.sep, "/")
    base = root.replace(os.sep, "/").rstrip("/")
    if lexical == base:
        return "."
    prefix = base + "/"
    if lexical.startswith(prefix):
        return lexical[len(prefix) :]
    return lexical.rsplit("/", 1)[-1]


def _is_escape_token(text: str, index: int) -> bool:
    return (
        index + 6 <= len(text)
        and text.startswith("\\u", index)
        and all(
            character in "0123456789ABCDEF"
            for character in text[index + 2 : index + 6]
        )
    )


def _field_tokens(
    text: str, iterator: icu.BreakIterator
) -> list[tuple[str, int]]:
    tokens: list[tuple[str, int]] = []
    index = 0
    length = len(text)
    span_start: int | None = None

    def flush(end: int) -> None:
        nonlocal span_start
        if span_start is None:
            return
        fragment = text[span_start:end]
        for begin, stop in _grapheme_bounds(fragment, iterator):
            grapheme = fragment[begin:stop]
            tokens.append((grapheme, _grapheme_width(grapheme)))
        span_start = None

    while index < length:
        if _is_escape_token(text, index):
            flush(index)
            tokens.append((text[index : index + 6], 6))
            index += 6
            continue
        if span_start is None:
            span_start = index
        index += 1
    flush(length)
    return tokens


def _truncate_row(row: str, width: int, iterator: icu.BreakIterator) -> str:
    if width <= 0:
        return ""
    tokens = _field_tokens(row, iterator)
    total = sum(cells for _text, cells in tokens)
    if total <= width:
        return row
    ellipsis = "…"
    ellipsis_width = _grapheme_width(ellipsis)
    if width < ellipsis_width:
        kept: list[str] = []
        used = 0
        for text, cells in tokens:
            if used + cells > width:
                break
            kept.append(text)
            used += cells
        return "".join(kept)
    kept_text: list[str] = []
    used = 0
    for text, cells in tokens:
        if used + cells + ellipsis_width > width:
            break
        kept_text.append(text)
        used += cells
    return "".join(kept_text) + ellipsis


def _truncate_block(text: str, width: int) -> str:
    iterator = icu.BreakIterator.createCharacterInstance(_ICU_ROOT)
    lines = text.split("\n")
    trailing = lines[-1] == ""
    body = lines[:-1] if trailing else lines
    truncated = [_truncate_row(line, width, iterator) for line in body]
    result = "\n".join(truncated)
    if trailing:
        result += "\n"
    return result


def _comma_row(items: tuple[str, ...] | list[str]) -> str:
    if not items:
        return "  (none)"
    return "  " + ", ".join(encode_field(item) for item in items)


def _startup_summary(
    state: ProjectResourceState,
    root: str,
    width: int,
    *,
    extension_diagnostics: tuple[ExtensionDiagnostic, ...] | None = None,
) -> str:
    report = state.report
    if report.discovery == "disabled":
        return _truncate_block("[Project resources] disabled\n", width)
    rules = tuple(
        _terminal_relative_path(rule.path, root)
        for rule in report.projectRules
        if rule.disposition == "effective"
    )
    skills = tuple(group.name for group in report.skills)
    prompts = tuple(f"/{group.name}" for group in report.promptTemplates)
    sections = (
        f"[Context]\n{_comma_row(rules)}\n",
        f"[Skills]\n{_comma_row(skills)}\n",
        f"[Prompts]\n{_comma_row(prompts)}\n",
    )
    body = "\n".join(sections)
    diagnostics = _resource_diagnostics(report, root)
    if diagnostics:
        body = f"{body}\n{diagnostics}"
    extensions = _extension_diagnostics(
        state.extensionDiagnostics
        if extension_diagnostics is None
        else extension_diagnostics,
        root,
    )
    if extensions:
        body = f"{body}\n{extensions}"
    return _truncate_block(body, width)


def _resources_command(state: ProjectResourceState, root: str, width: int) -> str:
    report = state.report
    if state.status == "current":
        header = (
            "[Resources]\n"
            f"state=current report=current discovery={report.discovery}\n"
        )
    else:
        header = (
            "[Resources]\n"
            "state=indeterminate report=last_successful "
            f"discovery={report.discovery}\n"
        )
    if report.discovery == "disabled":
        return _truncate_block(header, width)
    sections = (
        _project_rules_section(report, root),
        _named_resource_section("Skills", report.skills, root),
        _named_resource_section("Prompts", report.promptTemplates, root),
    )
    body = header + "\n" + "\n".join(sections)
    extensions = _extension_diagnostics(state.extensionDiagnostics, root)
    if extensions:
        body = f"{body}\n{extensions}"
    return _truncate_block(body, width)


def _project_rules_section(report: ResourceResolutionReport, root: str) -> str:
    if not report.projectRules:
        return "[Project Rules]\n  (none)\n"
    rows: list[str] = []
    for rule in report.projectRules:
        path = encode_field(_terminal_relative_path(rule.path, root))
        if rule.disposition == "effective":
            rows.append(f'effective path="{path}"')
            continue
        stage = encode_field(rule.validationStage or "")
        rows.append(f'soft_skipped path="{path}" stage="{stage}"')
    return "[Project Rules]\n" + "\n".join(rows) + "\n"


def _named_resource_section(
    title: str,
    groups: tuple[PromptResourceResolution, ...],
    root: str,
) -> str:
    if not groups:
        return f"[{title}]\n  (none)\n"
    blocks: list[str] = []
    for group in groups:
        name = encode_field(group.name)
        lines = [f'name="{name}"']
        for candidate in group.candidates:
            source = encode_field(candidate.source)
            path = encode_field(_terminal_relative_path(candidate.path, root))
            if candidate.disposition == "effective":
                lines.append(f'  effective source="{source}" path="{path}"')
            else:
                lines.append(
                    f'  shadowed_unchecked source="{source}" path="{path}"'
                )
        blocks.append("\n".join(lines))
    return f"[{title}]\n" + "\n".join(blocks) + "\n"


def _resource_diagnostics(report: ResourceResolutionReport, root: str) -> str:
    rows: list[str] = []
    for rule in report.projectRules:
        if rule.disposition != "soft_skipped" or rule.validationStage is None:
            continue
        path = encode_field(_terminal_relative_path(rule.path, root))
        stage = encode_field(rule.validationStage)
        rows.append(f'  project_rule soft_skipped path="{path}" stage="{stage}"')
    for kind, groups in (
        ("skill", report.skills),
        ("prompt_template", report.promptTemplates),
    ):
        for group in groups:
            if len(group.candidates) < 2:
                continue
            name = encode_field(group.name)
            winner = group.candidates[0]
            effective = encode_field(_terminal_relative_path(winner.path, root))
            rows.append(
                f'  {kind} collision name="{name}" effective="{effective}"'
            )
            for candidate in group.candidates[1:]:
                shadowed = encode_field(
                    _terminal_relative_path(candidate.path, root)
                )
                rows.append(
                    f'  {kind} collision name="{name}" '
                    f'shadowed_unchecked="{shadowed}"'
                )
    if not rows:
        return ""
    return "[Resource diagnostics]\n" + "\n".join(rows) + "\n"


def _extension_diagnostics(
    diagnostics: tuple[ExtensionDiagnostic, ...], root: str
) -> str:
    if not diagnostics:
        return ""
    rows: list[str] = []
    for item in diagnostics:
        if isinstance(item, ExtensionDiagnosticLoad):
            path = encode_field(_terminal_relative_path(item.path, root))
            phase = encode_field(item.phase)
            message = encode_field(item.message)
            rows.append(
                f'  load path="{path}" phase="{phase}" message="{message}"'
            )
            continue
        if isinstance(item, ExtensionDiagnosticLifecycle):
            path = encode_field(_terminal_relative_path(item.path, root))
            event = encode_field(item.eventType)
            message = encode_field(item.message)
            line = (
                f'  lifecycle path="{path}" event="{event}" message="{message}"'
            )
            if item.stack is not None:
                line += f' stack="{encode_field(item.stack)}"'
            rows.append(line)
    if not rows:
        return ""
    return "[Extension diagnostics]\n" + "\n".join(rows) + "\n"


def _fifo_removed(
    before: tuple[str, ...], after: tuple[str, ...]
) -> tuple[str, ...]:
    remaining = list(after)
    removed: list[str] = []
    for item in before:
        if remaining and remaining[0] == item:
            del remaining[0]
        else:
            removed.append(item)
    if remaining:
        return ()
    return tuple(removed)


def _single_removal(
    previous: PendingMessages, current: PendingMessages
) -> tuple[str, str] | None:
    steering = _fifo_removed(previous.steering, current.steering)
    follow_up = _fifo_removed(previous.followUp, current.followUp)
    if len(steering) == 1 and not follow_up:
        return ("steering", steering[0])
    if len(follow_up) == 1 and not steering:
        return ("follow_up", follow_up[0])
    return None


def _pending_group_rows(
    title: str, items: tuple[str, ...], max_rows: int
) -> list[str]:
    if not items or max_rows <= 0:
        return []
    rows = [f"{title} pending ({len(items)}):"]
    if max_rows == 1:
        return rows
    capacity = max_rows - 1
    if len(items) <= capacity:
        rows.extend(f"  {encode_field(item)}" for item in items)
        return rows
    shown = capacity - 1
    if shown <= 0:
        rows.append(f"  … +{len(items)} more")
        return rows
    rows.extend(f"  {encode_field(item)}" for item in items[:shown])
    rows.append(f"  … +{len(items) - shown} more")
    return rows


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
    try:
        while offset < len(payload):
            written = buffer.write(payload[offset:])
            if written is None or written <= 0:
                raise OSError("terminal write made no progress")
            offset += written
        buffer.flush()
    except BrokenPipeError as error:
        raise OSError(errno.EPIPE, "broken pipe") from error


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


@dataclass(frozen=True, slots=True)
class _CompletedLine:
    text: str
    alt: bool
    submission_id: int = 0


@dataclass(frozen=True, slots=True)
class _TerminalSubmission:
    text: str
    intent: Literal["prompt", "steering", "follow_up"]


@dataclass(frozen=True, slots=True)
class _ModalChoice:
    text: str


_QueuedLine = (
    bytes
    | Exception
    | _TerminalSubmission
    | _ModalChoice
    | Literal["reload", "clear_queue", "resources", "modal_cancel"]
)
_LineWait = _QueuedLine | Literal["interrupt", "terminate", "prompt_done"]


class _InteractiveTerminalAdapter:
    __slots__ = (
        "_admission_closed",
        "_awaiting_processed",
        "_command_active",
        "_editor",
        "_enqueue_waiting",
        "_input_failure",
        "_lines",
        "_live_encoded",
        "_loop",
        "_modal_kind",
        "_modal_pending",
        "_modal_text",
        "_modal_consumed_cr",
        "_output_held",
        "_project_root_path",
        "_pending_prompt",
        "_prompt_task",
        "_queue_snapshot",
        "_removed_for_admission",
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
        cwd = session.sessionManager.getCwd()
        self._project_root_path = _project_root(cwd) or cwd
        self._rendered_text = ""
        self._pending_prompt = False
        self._enqueue_waiting = False
        self._command_active = False
        self._modal_kind: Literal["trust", "settings"] | None = None
        self._modal_pending = bytearray()
        self._modal_text = ""
        self._modal_consumed_cr = False
        self._output_held = False
        self._prompt_task: asyncio.Task[None] | None = None
        self._input_failure: Exception | None = None
        self._live_encoded = ""
        self._admission_closed = False
        self._queue_snapshot = session.pendingMessages
        self._removed_for_admission: tuple[str, str] | None = None
        self._awaiting_processed: list[tuple[str, str]] = []
        self._loop: asyncio.AbstractEventLoop
        self._lines: asyncio.Queue[_QueuedLine]
        self._stdin_fd: int
        self._editor: _CommandInput

    async def run(self) -> int:
        self._loop = asyncio.get_running_loop()
        self._session.subscribe(self._render)
        self._lines = asyncio.Queue()
        self._stdin_fd = sys.stdin.buffer.fileno()
        saved_tty, echo = _acquire_end_of_line_editing(self._stdin_fd)
        output_fd = sys.stdout.buffer.fileno()
        self._editor = _CommandInput(echo=echo, output_fd=output_fd)
        self._editor.ensure_size()
        write_stdout(
            _startup_summary(
                self._session.projectResourceState,
                self._project_root_path,
                self._editor.width,
            )
        )
        self._editor.set_live_lines(self._allocate_live())

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
            try:
                stdout_fd = sys.stdout.fileno()
                sys.stdout.flush()
                os.write(stdout_fd, b"")
            except OSError as error:
                if error.errno in {errno.EPIPE, errno.EIO}:
                    devnull = os.open(os.devnull, os.O_WRONLY)
                    try:
                        os.dup2(devnull, sys.stdout.fileno())
                    finally:
                        os.close(devnull)
                else:
                    raise
            if reader_registered:
                self._loop.remove_reader(self._stdin_fd)
            if winch_registered:
                try:
                    self._loop.remove_signal_handler(signal.SIGWINCH)
                except (NotImplementedError, RuntimeError, OSError):
                    signal.signal(signal.SIGWINCH, signal.SIG_DFL)
            try:
                _restore_tty(self._stdin_fd, saved_tty)
            except OSError as error:
                if self._shutdown.signum is not None:
                    write_cleanup("terminal restore failed")
                elif error.errno in {errno.EPIPE, errno.EIO}:
                    pass
                else:
                    write_stderr("I/O error\n")
                    return 1

    async def _run_editor(self) -> int:
        while True:
            if self._shutdown.signum is not None:
                if self._prompt_task is not None:
                    await self._finish_prompt()
                return self._shutdown_status()
            item = await self._wait_line_or_control()
            if item == "prompt_done":
                await self._finish_prompt()
                if self._shutdown.signum is not None:
                    return self._shutdown_status()
                continue
            if item == "terminate":
                if self._modal_kind is not None:
                    self._leave_modal()
                if self._prompt_task is not None:
                    await self._finish_prompt()
                return self._shutdown_status()
            if item == "interrupt" or item == _INTERRUPT or item == "modal_cancel":
                if self._modal_kind is not None:
                    self._leave_modal()
                    self._refresh_live(force=True)
                    continue
                if self._is_idle_editor():
                    self._editor.handle_idle_interrupt()
                else:
                    asyncio.create_task(self._session.abort())
                continue
            if isinstance(item, Exception):
                raise item
            if isinstance(item, _ModalChoice):
                self._settle_modal(item.text)
                continue
            if item == b"":
                if self._modal_kind is not None:
                    self._leave_modal()
                self._admission_closed = True
                try:
                    await self._session.clearQueue()
                except LifecycleError:
                    pass
                if self._prompt_task is not None:
                    await self._finish_prompt()
                return 0
            if item == "resources":
                self._show_resources()
                continue
            if item == "reload":
                await self._reload_resources()
                continue
            if item == "clear_queue":
                await self._clear_queue()
                continue
            if not isinstance(item, _TerminalSubmission):
                raise RuntimeError("interactive editor submitted an invalid event")
            await self._submit(item)

    async def _render(self, event: AgentSessionEvent) -> None:
        if self._input_failure is not None:
            return
        try:
            await self._render_body(event)
        except OSError as error:
            if error.errno in {errno.EPIPE, errno.EIO}:
                return
            raise

    async def _render_body(self, event: AgentSessionEvent) -> None:
        if isinstance(event, AgentSessionEvent.QueueUpdate):
            if self._admission_closed:
                self._removed_for_admission = None
            else:
                self._removed_for_admission = _single_removal(
                    self._queue_snapshot, event.pendingMessages
                )
            self._queue_snapshot = event.pendingMessages
        elif isinstance(event, AgentSessionEvent.MessageStart) and isinstance(
            event.message, UserMessage
        ):
            pending = self._removed_for_admission
            self._removed_for_admission = None
            text = _user_text(event.message)
            if (
                not self._admission_closed
                and pending is not None
                and pending[1] == text
            ):
                kind, source = pending
                label = "Steering" if kind == "steering" else "Follow-up"
                self._write_record(
                    f"{label} admitted: {encode_field(source)}\n"
                )
                self._awaiting_processed.append((kind, source))
        else:
            self._removed_for_admission = None
            if isinstance(event, AgentSessionEvent.MessageStart) and isinstance(
                event.message, AssistantMessage
            ):
                self._rendered_text = ""
                self._write_record("assistant start\n", hold=True)
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
                    self._write_record(encode_body(suffix), hold=True)
                self._rendered_text = text
            elif isinstance(event, AgentSessionEvent.MessageEnd) and isinstance(
                event.message, AssistantMessage
            ):
                if not self._rendered_text.endswith("\n"):
                    self._write_record("\n", hold=True)
                self._write_record("assistant end\n")
            elif isinstance(event, AgentSessionEvent.ToolExecutionStart):
                self._write_record(
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
                self._write_record(
                    f"tool {kind} "
                    f'name="{encode_field(event.toolName)}" '
                    f'id="{encode_field(event.toolCallId)}" '
                    f"result={encode_json(result)}\n"
                )
            elif isinstance(event, AgentSessionEvent.TurnEnd):
                if self._awaiting_processed:
                    kind, source = self._awaiting_processed.pop(0)
                    label = "Steering" if kind == "steering" else "Follow-up"
                    self._write_record(
                        f"{label} processed: {encode_field(source)}\n"
                    )
            elif isinstance(event, AgentSessionEvent.AgentEnd):
                classification = _run_classification(event.messages)
                if classification == "unconfirmed":
                    raise RuntimeError(
                        "AgentSession run settlement is unconfirmed"
                    )
                self._write_record(f"run {classification}\n")
            elif isinstance(event, AgentSessionEvent.AgentSettled):
                self._write_record("session settled\n")
        if isinstance(event, AgentSessionEvent.QueueUpdate):
            self._refresh_live(force=True)
        self._loop.call_soon(self._schedule_refresh)

    def _write_record(self, data: bytes | str, *, hold: bool = False) -> None:
        if not self._output_held:
            self._editor.yield_for_output()
            self._output_held = True
        write_stdout(data)
        if hold:
            return
        self._output_held = False
        if self._modal_kind is not None:
            self._editor.restore_after_output()
            return
        self._refresh_live(force=True)

    def _is_idle_editor(self) -> bool:
        return not self._is_active_input() and self._prompt_task is None

    def _is_active_input(self) -> bool:
        return (
            self._command_active
            or self._pending_prompt
            or self._enqueue_waiting
            or not self._session.isIdle
        )

    def _fail_input(self, error: Exception) -> None:
        if (
            isinstance(error, OSError)
            and error.errno in {errno.EPIPE, errno.EIO}
            and self._is_idle_editor()
            and self._input_failure is None
        ):
            self._loop.remove_reader(self._stdin_fd)
            self._input_failure = error
            self._lines.put_nowait(b"")
            return
        self._loop.remove_reader(self._stdin_fd)
        self._input_failure = error
        if not self._is_idle_editor() and self._modal_kind is None:
            asyncio.create_task(self._session.abort())
            return
        self._lines.put_nowait(error)

    def _on_resize(self) -> None:
        if self._input_failure is not None:
            return
        try:
            self._editor.resize()
            if self._modal_kind is None:
                self._refresh_live(force=True)
        except Exception as error:
            self._fail_input(error)

    def _deliver(self, raw: bytes | _CompletedLine) -> None:
        if isinstance(raw, _CompletedLine):
            prompt = _es_trim(raw.text)
            if not prompt:
                return
            if self._command_active:
                self._editor.restore_draft()
                return
            if prompt == "/resources":
                self._lines.put_nowait("resources")
                return
            if prompt == "/reload":
                self._begin_command(raw)
                self._lines.put_nowait("reload")
                return
            if prompt == "/clear-queue":
                self._begin_command(raw)
                self._lines.put_nowait("clear_queue")
                return
            if prompt == "/trust":
                self._admit_policy_command(raw, "trust")
                return
            if prompt == "/settings":
                self._admit_policy_command(raw, "settings")
                return
            active = self._is_active_input()
            if raw.alt:
                intent: Literal["prompt", "steering", "follow_up"] = (
                    "follow_up" if active else "prompt"
                )
            else:
                intent = "steering" if active else "prompt"
            if intent != "prompt" and self._enqueue_waiting:
                return
            if intent == "prompt":
                self._pending_prompt = True
            else:
                self._enqueue_waiting = True
                self._editor.freeze()
            self._lines.put_nowait(_TerminalSubmission(prompt, intent))
            return
        if raw == b"":
            self._loop.remove_reader(self._stdin_fd)
            self._lines.put_nowait(raw)
            return
        if raw == _INTERRUPT:
            if not self._is_idle_editor():
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
        if _es_trim(text):
            self._deliver(_CompletedLine(text=text, alt=False))

    def _input_ready(self) -> None:
        try:
            raw = os.read(self._stdin_fd, 4096)
        except OSError as error:
            if error.errno == errno.EINTR:
                return
            self._fail_input(error)
            return
        if raw == b"":
            self._deliver(b"")
            return
        if self._modal_kind is not None:
            try:
                self._feed_modal(raw)
            except Exception as error:
                self._fail_input(error)
            return
        try:
            completed = self._editor.feed(
                raw, freeze_on_submit=self._is_active_input()
            )
        except Exception as error:
            self._fail_input(error)
            return
        for item in completed:
            self._deliver(item)
            if item == b"":
                return

    async def _wait_line_or_control(self) -> _LineWait:
        if self._shutdown.signum is not None:
            return "terminate"
        line = asyncio.create_task(self._lines.get())
        interrupt = asyncio.create_task(self._shutdown.wait_interrupt())
        terminate = asyncio.create_task(self._shutdown.wait())
        waiters: set[asyncio.Task[Any]] = {line, interrupt, terminate}
        prompt_task = self._prompt_task
        if prompt_task is not None:
            waiters.add(prompt_task)
        done, pending = await asyncio.wait(
            waiters,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            if task is not prompt_task:
                task.cancel()
        await asyncio.gather(
            *[task for task in pending if task is not prompt_task],
            return_exceptions=True,
        )
        if terminate in done or self._shutdown.signum is not None:
            return "terminate"
        if line in done:
            return line.result()
        if prompt_task is not None and prompt_task in done:
            return "prompt_done"
        self._shutdown.clear_interrupt()
        return "interrupt"

    async def _submit(self, item: _TerminalSubmission) -> None:
        if item.intent == "prompt":
            await self._admit_prompt(item.text)
            return
        await self._enqueue_queued(item.intent, item.text)

    def _show_resources(self) -> None:
        self._editor.clear_after_success()
        state = self._session.projectResourceState
        self._write_record(
            _resources_command(
                state,
                self._project_root_path,
                self._editor.width,
            )
        )

    def _begin_command(self, submitted: _CompletedLine) -> None:
        self._editor.accept_command(submitted.submission_id)
        self._command_active = True
        self._refresh_live(force=True)

    def _admit_policy_command(
        self, submitted: _CompletedLine, kind: Literal["trust", "settings"]
    ) -> None:
        active = self._is_active_input()
        self._editor.accept_command(submitted.submission_id)
        if active:
            self._write_stderr_record(
                f'command rejected name="/{kind}" reason="busy"\n'
            )
            return
        self._command_active = True
        self._enter_modal(kind)

    def _modal_prompt(self, kind: Literal["trust", "settings"]) -> str:
        if kind == "settings":
            return _SETTINGS_MODAL
        cwd = self._session.sessionManager.getCwd()
        if trust_path_has_parent(cwd):
            return _TRUST_MODAL_WITH_PARENT
        return _TRUST_MODAL_ROOT

    def _enter_modal(self, kind: Literal["trust", "settings"]) -> None:
        self._modal_kind = kind
        self._modal_text = ""
        self._modal_pending.clear()
        self._modal_consumed_cr = False
        self._editor.show_overlay(self._modal_prompt(kind))

    def _leave_modal(self) -> None:
        self._modal_kind = None
        self._modal_text = ""
        self._modal_pending.clear()
        self._modal_consumed_cr = False
        self._command_active = False
        self._editor.clear_overlay()

    def _redraw_modal(self) -> None:
        kind = self._modal_kind
        if kind is None:
            return
        self._modal_text = ""
        self._editor.show_overlay(self._modal_prompt(kind))

    def _settle_modal(self, text: str) -> None:
        kind = self._modal_kind
        if kind is None:
            return
        choice = _es_trim(text).lower()
        if choice == "c":
            self._leave_modal()
            self._refresh_live(force=True)
            return
        cwd = self._session.sessionManager.getCwd()
        if kind == "trust":
            action = _trust_modal_action(choice, trust_path_has_parent(cwd))
            if action is None:
                self._redraw_modal()
                return
            try:
                update_project_trust(cwd, action)
            except TrustPolicyError as error:
                self._leave_modal()
                self._write_policy_failure(error)
                return
            decision = {
                "trust": "trusted",
                "trust_parent": "trusted_parent",
                "distrust": "untrusted",
            }[action]
            self._leave_modal()
            self._write_record(
                "project trust saved "
                f'decision="{decision}"; applies to future Sessions\n'
            )
            return
        setting = _settings_modal_value(choice)
        if setting is None:
            self._redraw_modal()
            return
        try:
            update_default_project_trust(setting)
        except TrustPolicyError as error:
            self._leave_modal()
            self._write_policy_failure(error)
            return
        self._leave_modal()
        self._write_record(
            "default project trust saved "
            f'value="{setting}"; applies to future Sessions\n'
        )

    def _write_policy_failure(self, error: TrustPolicyError) -> None:
        self._write_stderr_record(
            f'trust policy failed operation="{error.operation}" '
            f'stage="{error.stage}"\n'
        )

    def _feed_modal(self, data: bytes) -> None:
        self._modal_pending.extend(data)
        while self._modal_pending:
            first = self._modal_pending[0]
            if first == 0x1B:
                if len(self._modal_pending) == 1:
                    del self._modal_pending[0]
                    self._lines.put_nowait("modal_cancel")
                    return
                action = _take_escape(self._modal_pending)
                if action is None:
                    return
                if action == "reprocess":
                    continue
                continue
            if first < 0x20 or first == 0x7F:
                del self._modal_pending[0]
                if first == 0x0A and self._modal_consumed_cr:
                    self._modal_consumed_cr = False
                    continue
                if first == 0x0D:
                    self._modal_consumed_cr = True
                    submitted = self._modal_text
                    self._modal_text = ""
                    self._lines.put_nowait(_ModalChoice(submitted))
                    continue
                self._modal_consumed_cr = False
                if first == 0x0A:
                    submitted = self._modal_text
                    self._modal_text = ""
                    self._lines.put_nowait(_ModalChoice(submitted))
                    continue
                if first == 0x03:
                    self._lines.put_nowait("modal_cancel")
                    return
                if first == 0x04:
                    self._lines.put_nowait(b"")
                    return
                if first in {0x08, 0x7F}:
                    self._modal_text = self._modal_text[:-1]
                continue
            character = _take_complete_character(self._modal_pending)
            if character is None:
                return
            self._modal_text += character

    async def _reload_resources(self) -> None:
        try:
            result = await self._session.reloadProjectResources()
        except LifecycleError as error:
            if error.code != "busy":
                raise
            self._write_stderr_record(
                'command rejected name="/reload" reason="busy"\n'
            )
        except ResourceAdmissionError as error:
            name = (
                ""
                if error.name is None
                else f' name="{encode_field(error.name)}"'
            )
            path = encode_field(
                _terminal_relative_path(error.path, self._project_root_path)
            )
            self._write_stderr_record(
                f'resource admission failed kind="{encode_field(error.kind)}"'
                f'{name} path="{path}" stage="{encode_field(error.stage)}"\n'
            )
        except ProjectResourceReloadError as error:
            self._refresh_live(force=True)
            self._write_stderr_record(
                'resource reload failed state="indeterminate"\n'
            )
            diagnostics = _extension_diagnostics(
                error.diagnostics,
                self._project_root_path,
            )
            if diagnostics:
                self._write_record(
                    _truncate_block(diagnostics, self._editor.width)
                )
        else:
            self._write_reload_result(result)
        finally:
            self._command_active = False
            self._refresh_live(force=True)

    def _write_reload_result(self, result: ProjectResourceReloadResult) -> None:
        self._write_record(f"resource reload {result.status}\n")
        self._write_record(
            _startup_summary(
                result.state,
                self._project_root_path,
                self._editor.width,
                extension_diagnostics=result.diagnostics,
            )
        )

    async def _clear_queue(self) -> None:
        try:
            cleared = await self._session.clearQueue()
            self._removed_for_admission = None
            self._queue_snapshot = self._session.pendingMessages
            self._write_record(
                "pending queues cleared "
                f"steering={len(cleared.steering)} "
                f"followUp={len(cleared.followUp)}; "
                "drained messages may still proceed\n"
            )
        finally:
            self._command_active = False
            self._refresh_live(force=True)

    def _write_stderr_record(self, data: str) -> None:
        self._editor.yield_for_output()
        if not write_stderr(data):
            raise OSError("interactive diagnostic write failed")
        self._refresh_live(force=True)

    async def _admit_prompt(self, text: str) -> None:
        self._editor.freeze()
        prior_messages = self._session.messages
        prior_entries = self._session.sessionManager.getEntries()
        task = asyncio.create_task(self._session.prompt(text))
        self._prompt_task = task
        nudge = asyncio.get_running_loop().create_future()
        self._loop.call_soon(nudge.set_result, None)
        await asyncio.wait(
            {task, nudge}, return_when=asyncio.FIRST_COMPLETED
        )
        self._pending_prompt = False
        if not nudge.done():
            nudge.cancel()
        if not task.done():
            self._editor.clear_after_success()
            self._refresh_live(force=True)
            return
        self._prompt_task = None
        try:
            task.result()
        except ValueError as error:
            if (
                self._session.messages != prior_messages
                or self._session.sessionManager.getEntries() != prior_entries
                or not self._session.isIdle
            ):
                raise RuntimeError(
                    "AgentSession ValueError changed admitted state"
                ) from error
            self._editor.restore_draft()
            if not write_error(_public_value_message(error)):
                raise OSError("interactive diagnostic write failed")
            return
        self._editor.clear_after_success()
        await self._finish_prompt_task(task)

    async def _enqueue_queued(
        self, intent: Literal["steering", "follow_up"], text: str
    ) -> None:
        self._editor.freeze()
        prior_messages = self._session.messages
        prior_entries = self._session.sessionManager.getEntries()
        prior_pending = self._session.pendingMessages
        call = (
            self._session.steer if intent == "steering" else self._session.followUp
        )
        try:
            await call(text)
        except ValueError as error:
            self._enqueue_waiting = False
            effects = (
                self._session.messages != prior_messages
                or self._session.sessionManager.getEntries() != prior_entries
                or self._session.pendingMessages != prior_pending
            )
            if effects:
                raise RuntimeError(
                    "AgentSession ValueError changed admitted state"
                ) from error
            self._editor.restore_draft()
            if not write_error(_public_value_message(error)):
                raise OSError("interactive diagnostic write failed")
            return
        except BaseException:
            self._enqueue_waiting = False
            raise
        self._enqueue_waiting = False
        self._editor.clear_after_success()
        self._refresh_live(force=True)

    async def _finish_prompt(self) -> None:
        task = self._prompt_task
        self._prompt_task = None
        if task is None:
            return
        await self._finish_prompt_task(task)

    async def _finish_prompt_task(self, task: asyncio.Task[None]) -> None:
        if self._input_failure is not None:
            try:
                await task
            except BaseException:
                pass
            raise self._input_failure
        await task
        if self._shutdown.signum is not None:
            self._release_output()
            return
        self._release_output()

    def _release_output(self) -> None:
        if self._output_held:
            self._output_held = False
        self._refresh_live(force=True)

    def _schedule_refresh(self) -> None:
        if self._input_failure is not None:
            return
        try:
            self._refresh_live()
        except Exception as error:
            self._fail_input(error)

    def _refresh_live(self, *, force: bool = False) -> None:
        if self._input_failure is not None:
            return
        try:
            lines = self._allocate_live()
            encoded = "\n".join(lines)
            changed = encoded != self._live_encoded
            self._live_encoded = encoded
            self._editor.set_live_lines(lines)
            if self._modal_kind is not None:
                return
            if self._output_held:
                return
            if changed or force:
                self._editor.restore_after_output()
        except OSError as error:
            if error.errno in {errno.EPIPE, errno.EIO}:
                return
            raise

    def _allocate_live(self) -> tuple[str, ...]:
        session = self._session
        idle = "idle" if session.isIdle else "active"
        run = "streaming" if session.isStreaming else "none"
        core = (
            f"Session {encode_field(session.sessionId)}: {idle}",
            f"Run: {run}",
            _resources_line(session.projectResourceState),
        )
        height = self._editor.height
        budget = max(0, height - 1)
        lines: list[str] = []
        for row in core:
            if len(lines) >= budget:
                break
            lines.append(row)
        remaining = budget - len(lines)
        pending = session.pendingMessages
        steering_rows = _pending_group_rows(
            "Steering", pending.steering, remaining
        )
        lines.extend(steering_rows)
        follow_rows = _pending_group_rows(
            "Follow-up",
            pending.followUp,
            remaining - len(steering_rows),
        )
        lines.extend(follow_rows)
        return tuple(lines)

    def _shutdown_status(self) -> int:
        assert self._shutdown.signum is not None
        return _SIGNAL_STATUS[self._shutdown.signum]


def _trust_modal_action(
    choice: str, has_parent: bool
) -> Literal["trust", "distrust", "trust_parent"] | None:
    if choice == "t":
        return "trust"
    if choice == "p" and has_parent:
        return "trust_parent"
    if choice == "n":
        return "distrust"
    return None


def _settings_modal_value(
    choice: str,
) -> Literal["ask", "always", "never"] | None:
    if choice == "a":
        return "ask"
    if choice == "y":
        return "always"
    if choice == "n":
        return "never"
    return None


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
        "_draft",
        "_draft_submission_id",
        "_frozen",
        "_freeze_on_submit",
        "_pending",
        "_submission_id",
        "_consumed_cr",
        "_paused",
        "_width",
        "_height",
        "_origin",
        "_owned_rows",
        "_yielded",
        "_grapheme_iter",
        "_word_iter",
        "_live_lines",
        "_overlay",
    )

    def __init__(self, *, echo: bool, output_fd: int) -> None:
        self._echo = echo
        self._output_fd = output_fd
        self._text = ""
        self._cursor = 0
        self._draft = ""
        self._draft_submission_id = 0
        self._frozen = False
        self._freeze_on_submit = False
        self._pending = bytearray()
        self._submission_id = 0
        self._consumed_cr = False
        self._paused = False
        self._width = 0
        self._height = 0
        self._origin = 0
        self._owned_rows = 0
        self._yielded = False
        self._live_lines: tuple[str, ...] = ()
        self._overlay: str | None = None
        self._grapheme_iter = icu.BreakIterator.createCharacterInstance(_ICU_ROOT)
        self._word_iter = icu.BreakIterator.createWordInstance(_ICU_ROOT)

    @property
    def height(self) -> int:
        return self._height

    @property
    def width(self) -> int:
        return self._width

    def set_live_lines(self, lines: tuple[str, ...]) -> None:
        self._live_lines = lines

    def show_overlay(self, text: str) -> None:
        self._overlay = text
        self._yielded = False
        if not self._paused:
            self._paint()

    def clear_overlay(self) -> None:
        if self._overlay is None:
            return
        self.yield_for_output()
        self._overlay = None
        self._yielded = False

    def ensure_size(self) -> None:
        if self._width == 0:
            self._read_size()

    def begin(self) -> None:
        if self._width == 0:
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
        if recovering or self._echo or self._owned_rows or self._overlay is not None:
            self._paint()

    def freeze(self) -> None:
        self._frozen = True

    def yield_for_output(self) -> None:
        if self._paused:
            self._yielded = True
            return
        if self._owned_rows == 0:
            self._yielded = True
            return
        self._home()
        count = self._owned_rows
        for index in range(count):
            write_stdout("\x1b[K")
            if index < count - 1:
                write_stdout("\n")
        if count > 1:
            write_stdout(f"\x1b[{count - 1}A")
        write_stdout("\r")
        self._owned_rows = 0
        self._yielded = True

    def restore_after_output(self) -> None:
        self._yielded = False
        if not self._paused:
            self._paint()

    def has_visible_input(self) -> bool:
        return bool(self._text) and not self._paused

    def restore_draft(self) -> None:
        self._text = self._draft
        self._cursor = len(self._text)
        self._frozen = False
        if not self._paused:
            self._paint()

    def clear_after_success(self) -> None:
        self._draft = ""
        self._draft_submission_id = 0
        self._text = ""
        self._cursor = 0
        self._origin = 0
        self._pending.clear()
        self._frozen = False

    def accept_command(self, submission_id: int) -> None:
        if self._draft_submission_id == submission_id:
            self._draft = ""
            self._draft_submission_id = 0
            self._frozen = False
            return
        self.restore_draft()

    def handle_idle_interrupt(self) -> None:
        self._text = ""
        self._cursor = 0
        self._pending.clear()
        self._consumed_cr = False
        self._origin = 0
        if not self._paused:
            self._paint()

    def feed(
        self, data: bytes, *, freeze_on_submit: bool = False
    ) -> list[bytes | _CompletedLine]:
        self._pending.extend(data)
        self._freeze_on_submit = freeze_on_submit
        try:
            return self._consume_pending()
        finally:
            self._freeze_on_submit = False

    def _consume_pending(self) -> list[bytes | _CompletedLine]:
        completed: list[bytes | _CompletedLine] = []
        while self._pending:
            first = self._pending[0]
            if first == 0x1B:
                action = _take_escape(self._pending)
                if action is None:
                    break
                if action == "reprocess":
                    continue
                if action == "alt_enter":
                    line = self._enter(alt=True)
                    if line is not None:
                        completed.append(line)
                    continue
                navigation = self._navigation(action)
                if navigation is not None:
                    completed.append(navigation)
                continue
            if first < 0x20 or first == 0x7F:
                del self._pending[0]
                control = self._control(first)
                if control is not None:
                    completed.append(control)
                    if control == b"":
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

    def _control(self, first: int) -> bytes | _CompletedLine | None:
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
        if self._paused or self._frozen or not action:
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
        if self._frozen:
            return
        self._text = self._text[: self._cursor] + character + self._text[self._cursor :]
        self._cursor += len(character)
        if self._echo:
            self._paint()

    def _backward_delete(self) -> None:
        if self._paused or self._frozen:
            return
        if self._cursor == 0:
            return
        start = _move_grapheme(self._text, self._cursor, -1, self._grapheme_iter)
        self._text = self._text[:start] + self._text[self._cursor :]
        self._cursor = start
        if self._echo:
            self._paint()

    def _forward_delete(self) -> None:
        if self._paused or self._frozen:
            return
        if self._cursor >= len(self._text):
            return
        end = _move_grapheme(self._text, self._cursor, 1, self._grapheme_iter)
        self._text = self._text[: self._cursor] + self._text[end:]
        if self._echo:
            self._paint()

    def _enter(self, *, alt: bool = False) -> _CompletedLine | None:
        if self._paused or self._frozen:
            return None
        line = self._text
        if not _es_trim(line):
            self._text = ""
            self._cursor = 0
            self._origin = 0
            self._paint()
            return None
        self._finalize()
        self._submission_id += 1
        self._draft = line
        self._draft_submission_id = self._submission_id
        self._text = ""
        self._cursor = 0
        self._origin = 0
        if self._freeze_on_submit:
            self._frozen = True
        return _CompletedLine(
            text=line,
            alt=alt,
            submission_id=self._submission_id,
        )

    def _ctrl_d(self) -> bytes | None:
        if self._paused:
            return b"" if not self._text else None
        if self._frozen and self._text:
            return None
        if not self._text:
            return b""
        self._forward_delete()
        return None

    def _interrupt(self) -> bytes | None:
        return _INTERRUPT

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
        usable = max(1, self._height - len(self._live_lines))
        return min(usable, max(1, max(5, self._height * 30 // 100)))

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
        if self._paused:
            return
        if self._overlay is not None:
            self._paint_overlay()
            return
        if self._yielded and not self._text:
            return
        self._home()
        live = self._live_lines
        for line in live:
            write_stdout(line)
            write_stdout("\x1b[K\n")
        if not self._echo:
            write_stdout("> \x1b[K")
            extra = self._owned_rows - len(live) - 1
            if extra > 0:
                for _ in range(extra):
                    write_stdout("\n\x1b[K")
                write_stdout(f"\x1b[{extra}A")
            self._owned_rows = len(live) + 1
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
        editor_rows = max(len(visible), 1)
        extra = self._owned_rows - len(live) - editor_rows
        if extra > 0:
            for _ in range(extra):
                write_stdout("\n\x1b[K")
            write_stdout(f"\x1b[{extra}A")
        self._owned_rows = len(live) + editor_rows
        last = len(visible) - 1
        target = cursor_row - origin
        up = last - target
        if up > 0:
            write_stdout(f"\x1b[{up}A")
        write_stdout(f"\x1b[{cursor_col + 1}G")

    def _paint_overlay(self) -> None:
        self._home()
        overlay = self._overlay
        assert overlay is not None
        lines = overlay.split("\n")
        for index, line in enumerate(lines):
            write_stdout(line)
            write_stdout("\x1b[K")
            if index < len(lines) - 1:
                write_stdout("\n")
        extra = self._owned_rows - len(lines)
        if extra > 0:
            for _ in range(extra):
                write_stdout("\n\x1b[K")
            write_stdout(f"\x1b[{extra}A")
        self._owned_rows = len(lines)
        self._yielded = False

    def _finalize(self) -> None:
        submitted = self._text
        self.yield_for_output()
        self._yielded = False
        if not self._echo:
            write_stdout("\n")
            return
        write_stdout("> " + submitted + "\n")


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
    if second in {0x0D, 0x0A}:
        del buffer[:2]
        return "alt_enter"
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
    if parameter == b"27;3;13" and final == 0x7E:
        return "alt_enter"
    if parameter == b"13;3" and final == 0x75:
        return "alt_enter"
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
