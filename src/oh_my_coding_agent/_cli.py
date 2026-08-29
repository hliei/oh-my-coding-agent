from __future__ import annotations

import asyncio
from dataclasses import dataclass
from importlib import metadata
import os
import signal
import sys
from types import FrameType
from typing import Literal

from oh_my_coding_agent import (
    AgentSession,
    AgentSessionEvent,
    CreateAgentSessionOptions,
    NewSessionOptions,
    SessionInfo,
    SessionManager,
    createAgentSession,
)
from oh_my_llm import AssistantMessage, LifecycleError, ModelsError, TextContent

from ._prompt_resources import _es_trim
from ._session_manager import _resolve_path, _session_id
from ._terminal import (
    _INTERRUPT,
    _CommandInput,
    _acquire_end_of_line_editing,
    _restore_tty,
    encode_body,
    encode_field,
    encode_json,
    write_cleanup,
    write_error,
    write_identity,
    write_identity_stdout,
    write_lifecycle,
    write_stderr,
    write_stdout,
    write_usage,
)


HELP_TEXT = """\
omh - coding agent

Usage:
  omh [options]
  omh --print [options] [PROMPT]

Command Modes:
  omh          Interactive REPL. Requires TTY stdin and stdout.
  omh --print  One-shot. Performs one text Run and exits.

Options:
  --print              Select one-shot Command Mode
  --cwd PATH           Working directory
  --continue, -c       Continue the current project's most recent Session
  --session PATH_OR_ID Open a JSONL path, or search exact then prefix id
  --session-id ID      Open an exact current-project Session, or create that id
  --no-session         In-memory Session; may combine with --session-id
  --trust-project      Trust project resources without prompting
  --help               Show this help and exit
  --version            Show version and exit

Prompt:
  One-shot accepts exactly one source: a positional PROMPT, or complete
  strict-UTF-8 non-TTY stdin when PROMPT is omitted.

Trust:
  Interactive asks for a yes/no decision unless --trust-project is set.
  One-shot never prompts: the flag is trusted and omission is untrusted.

Sessions:
  With no selector, a persistent Session is created. An existing selected
  Session is recovered; a fresh persistent or in-memory Session is new.

Status:
  0   Command Mode completed
  1   Model, construction, lifecycle, or I/O failure
  2   Usage failure
  130 SIGINT
  129 SIGHUP
  143 SIGTERM
"""

_VALUE_FLAGS = {
    "--cwd": "cwd",
    "--session": "session",
    "--session-id": "session_id",
}
_SWITCH_FLAGS = {
    "--print": "print_mode",
    "--continue": "continue_session",
    "-c": "continue_session",
    "--no-session": "no_session",
    "--trust-project": "trust_project",
    "--help": "help",
    "--version": "version",
}
_SIGNAL_STATUS: dict[signal.Signals, int] = {
    signal.SIGINT: 130,
    signal.SIGHUP: 129,
    signal.SIGTERM: 143,
}


class _Usage(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _InternalError(Exception):
    pass


class _ResolveError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class _Parsed:
    print_mode: bool = False
    help: bool = False
    version: bool = False
    cwd: str | None = None
    continue_session: bool = False
    session: str | None = None
    session_id: str | None = None
    no_session: bool = False
    trust_project: bool = False
    end_of_options: bool = False
    positionals: tuple[str, ...] = ()


@dataclass
class _Selected:
    manager: SessionManager
    identity: Literal["new", "recovered"]


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


def main() -> None:
    try:
        parsed = _parse(sys.argv[1:])
        _reject_help_version_combinations(parsed)
        if parsed.help:
            write_stdout(HELP_TEXT)
            raise SystemExit(0)
        if parsed.version:
            write_stdout(f"omh {_installed_version()}\n")
            raise SystemExit(0)
        prompt = _admit_source(parsed)
        raise SystemExit(asyncio.run(_drive_command_mode(parsed, prompt)))
    except _Usage as error:
        write_usage(error.reason)
        raise SystemExit(2)
    except _InternalError:
        write_stderr("internal error\n")
        raise SystemExit(1)
    except OSError:
        write_stderr("I/O error\n")
        raise SystemExit(1)


def _parse(argv: list[str]) -> _Parsed:
    parsed = _Parsed()
    positionals: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            parsed.end_of_options = True
            positionals.extend(argv[index + 1 :])
            break
        if token in _VALUE_FLAGS:
            field = _VALUE_FLAGS[token]
            if getattr(parsed, field) is not None:
                raise _Usage("repeated option")
            if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                raise _Usage(f"{token} requires a value")
            index += 1
            setattr(parsed, field, argv[index])
        elif token in _SWITCH_FLAGS:
            field = _SWITCH_FLAGS[token]
            if getattr(parsed, field):
                raise _Usage("repeated option")
            setattr(parsed, field, True)
        elif token.startswith("-"):
            raise _Usage("unknown option")
        else:
            positionals.append(token)
        index += 1
    parsed.positionals = tuple(positionals)
    _reject_selector_conflicts(parsed)
    if parsed.session_id is not None:
        _require_session_id(parsed.session_id)
    if parsed.session is not None and not _path_shaped(parsed.session):
        _require_session_id(parsed.session)
    return parsed


def _reject_help_version_combinations(parsed: _Parsed) -> None:
    if not parsed.help and not parsed.version:
        return
    extras = (
        parsed.print_mode
        or parsed.cwd is not None
        or parsed.continue_session
        or parsed.session is not None
        or parsed.session_id is not None
        or parsed.no_session
        or parsed.trust_project
        or parsed.end_of_options
        or parsed.positionals
        or (parsed.help and parsed.version)
    )
    if extras:
        raise _Usage("help and version are exclusive terminal actions")


def _reject_selector_conflicts(parsed: _Parsed) -> None:
    persistent = [
        parsed.continue_session,
        parsed.session is not None,
        parsed.session_id is not None,
    ]
    if sum(persistent) > 1:
        raise _Usage("conflicting session selectors")
    if parsed.no_session and (parsed.session is not None or parsed.continue_session):
        raise _Usage("conflicting session selectors")


def _admit_source(parsed: _Parsed) -> str | None:
    stdin_tty = sys.stdin.isatty()
    stdout_tty = sys.stdout.isatty()
    if not parsed.print_mode:
        if not stdin_tty or not stdout_tty:
            raise _Usage("interactive mode requires a TTY; use --print")
        if parsed.positionals:
            raise _Usage("extra prompt")
        return None
    if len(parsed.positionals) > 1:
        raise _Usage("extra prompt")
    if parsed.positionals:
        if not stdin_tty:
            raise _Usage("prompt and stdin both provided")
        return _normalized_prompt(parsed.positionals[0])
    if stdin_tty:
        raise _Usage("missing prompt")
    source = _read_stdin()
    if source == "":
        raise _Usage("missing prompt")
    return _normalized_prompt(source)


def _normalized_prompt(source: str) -> str:
    trimmed = _es_trim(source)
    if not trimmed:
        raise _Usage("empty prompt")
    return trimmed


def _read_stdin() -> str:
    raw = sys.stdin.buffer.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _Usage("invalid UTF-8") from error


def _installed_version() -> str:
    try:
        version = metadata.distribution("omh").version
    except metadata.PackageNotFoundError as error:
        raise _InternalError() from error
    if type(version) is not str or not version:
        raise _InternalError()
    return version


def _path_shaped(value: str) -> bool:
    return "/" in value or "\\" in value or value.endswith(".jsonl")


def _require_session_id(value: str) -> None:
    try:
        _session_id(NewSessionOptions(id=value))
    except (TypeError, ValueError) as error:
        raise _Usage("invalid session id") from error


async def _drive_command_mode(parsed: _Parsed, prompt: str | None) -> int:
    process_cwd = os.getcwd()
    try:
        cwd = (
            os.path.abspath(process_cwd)
            if parsed.cwd is None
            else _resolve_path(parsed.cwd, "--cwd")
        )
    except ValueError as error:
        write_error(_public_value_message(error))
        return 1
    shutdown = _Shutdown(print_mode=parsed.print_mode)
    loop = asyncio.get_running_loop()
    for signum in _SIGNAL_STATUS:
        try:
            loop.add_signal_handler(signum, shutdown.request, signum)
        except (NotImplementedError, RuntimeError):
            def _handle(
                _num: int,
                _frame: FrameType | None,
                selected: signal.Signals = signum,
            ) -> None:
                shutdown.request(selected)

            signal.signal(signum, _handle)
    if shutdown.signum is not None:
        return _pre_session_cancel(shutdown.signum)
    try:
        selected = await _select_manager(parsed, cwd)
    except _Usage as error:
        write_usage(error.reason)
        return 2
    except _ResolveError as error:
        write_error(error.message)
        return 1
    except ValueError as error:
        write_error(_public_value_message(error))
        return 1
    except OSError:
        write_stderr("I/O error\n")
        return 1
    if shutdown.signum is not None:
        return _pre_session_cancel(shutdown.signum)
    if parsed.print_mode or parsed.trust_project:
        project_trusted = parsed.trust_project
    else:
        trusted = await _interactive_trust(cwd, shutdown)
        if trusted is None:
            return 0 if shutdown.signum is None else _SIGNAL_STATUS[shutdown.signum]
        project_trusted = trusted
    return await _drive_session(
        parsed, selected, cwd, project_trusted, prompt, shutdown
    )


async def _interactive_trust(cwd: str, shutdown: _Shutdown) -> bool | None:
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[bytes | OSError] = asyncio.Queue()
    stdin_fd = sys.stdin.buffer.fileno()

    def input_ready() -> None:
        try:
            raw = sys.stdin.buffer.readline()
        except OSError as error:
            loop.remove_reader(stdin_fd)
            lines.put_nowait(error)
            return
        if raw == b"":
            loop.remove_reader(stdin_fd)
        lines.put_nowait(raw)

    loop.add_reader(stdin_fd, input_ready)
    try:
        if shutdown.signum is not None:
            _write_trust_cancel(shutdown.signum)
            return None
        while True:
            write_stdout(
                f'Trust project resources in "{encode_field(cwd)}"? [y/n] '
            )
            line = asyncio.create_task(lines.get())
            cancelled = asyncio.create_task(shutdown.wait())
            done, _pending = await asyncio.wait(
                (line, cancelled), return_when=asyncio.FIRST_COMPLETED
            )
            if cancelled in done:
                line.cancel()
                await asyncio.gather(line, return_exceptions=True)
                _write_trust_cancel(shutdown.signum)
                return None
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
            raw = line.result()
            if isinstance(raw, OSError):
                raise raw
            if raw == b"":
                write_stdout("trust cancelled\n")
                return None
            text = _decode_interactive_line(raw, "trust input is not UTF-8")
            lowered = _es_trim(text).lower()
            if lowered == "y":
                return True
            if lowered == "n":
                return False
            write_stdout("Please enter y or n.\n")
    finally:
        loop.remove_reader(stdin_fd)


def _write_trust_cancel(signum: signal.Signals | None) -> None:
    if signum is None or signum == signal.SIGINT:
        write_stdout("trust cancelled\n")
        return
    write_stderr("cancelled\n")


def _pre_session_cancel(signum: signal.Signals) -> int:
    write_stderr("cancelled\n")
    return _SIGNAL_STATUS[signum]


async def _select_manager(parsed: _Parsed, cwd: str) -> _Selected:
    if parsed.no_session:
        options = (
            NewSessionOptions(id=parsed.session_id)
            if parsed.session_id is not None
            else None
        )
        return _Selected(SessionManager.inMemory(cwd, options), "new")
    if parsed.session is not None:
        return await _select_session_arg(parsed.session, cwd)
    if parsed.continue_session:
        manager = SessionManager.continueRecent(cwd)
        path = manager.getSessionFile()
        identity: Literal["new", "recovered"] = (
            "recovered" if path is not None and os.path.exists(path) else "new"
        )
        return _Selected(manager, identity)
    if parsed.session_id is not None:
        existing = await _find_exact_local(parsed.session_id, cwd)
        if existing is not None:
            return _Selected(SessionManager.open(existing), "recovered")
        return _Selected(
            SessionManager.create(cwd, options=NewSessionOptions(id=parsed.session_id)),
            "new",
        )
    return _Selected(SessionManager.create(cwd), "new")


async def _select_session_arg(argument: str, cwd: str) -> _Selected:
    if _path_shaped(argument):
        path = _resolve_path(argument, "--session")
        existed = os.path.exists(path) and os.path.getsize(path) > 0
        return _Selected(SessionManager.open(path), "recovered" if existed else "new")
    local = await SessionManager.list(cwd)
    match = _match_session_id(local, argument)
    if match is not None:
        return _Selected(SessionManager.open(match), "recovered")
    global_sessions = await SessionManager.listAll()
    match = _match_session_id(global_sessions, argument)
    if match is not None:
        return _Selected(SessionManager.open(match), "recovered")
    raise _ResolveError("session not found")


def _match_session_id(sessions: tuple[SessionInfo, ...], argument: str) -> str | None:
    exact = next((item.path for item in sessions if item.id == argument), None)
    if exact is not None:
        return exact
    return next(
        (item.path for item in sessions if item.id.startswith(argument)),
        None,
    )


async def _find_exact_local(session_id: str, cwd: str) -> str | None:
    local = await SessionManager.list(cwd)
    return next((item.path for item in local if item.id == session_id), None)


async def _drive_session(
    parsed: _Parsed,
    selected: _Selected,
    cwd: str,
    project_trusted: bool,
    prompt: str | None,
    shutdown: _Shutdown,
) -> int:
    try:
        result = await createAgentSession(
            CreateAgentSessionOptions(
                cwd=cwd,
                sessionManager=selected.manager,
                projectTrusted=project_trusted,
            )
        )
    except asyncio.CancelledError:
        if shutdown.signum is not None:
            return _pre_session_cancel(shutdown.signum)
        raise
    except ModelsError as error:
        write_error(str(error))
        return 1
    except ValueError as error:
        write_error(_public_value_message(error))
        return 1
    except LifecycleError as error:
        write_lifecycle(error.code, str(error))
        return 1
    except OSError:
        write_stderr("I/O error\n")
        return 1
    except Exception:
        write_stderr("internal error\n")
        return 1
    session = result.session
    shutdown.session = session
    status = 1
    try:
        status = await _drive_published_session(
            parsed, selected, session, prompt, shutdown
        )
    finally:
        status = await _dispose(session, status)
    return status


async def _drive_published_session(
    parsed: _Parsed,
    selected: _Selected,
    session: AgentSession,
    prompt: str | None,
    shutdown: _Shutdown,
) -> int:
    try:
        if parsed.print_mode:
            write_identity(session.sessionId, selected.identity)
        else:
            write_identity_stdout(session.sessionId, selected.identity)
    except OSError:
        write_stderr("I/O error\n")
        return 1
    if shutdown.signum is not None:
        if parsed.print_mode:
            write_stderr("cancelled\n")
        return _SIGNAL_STATUS[shutdown.signum]
    if not parsed.print_mode:
        return await _drive_interactive(session, shutdown)
    assert prompt is not None
    classification: Literal["completed", "model_error", "cancelled", "unconfirmed"]
    stdout_payload: bytes | None
    status = 0
    primary: str | None = None
    try:
        await session.prompt(prompt)
        classification, stdout_payload = _one_shot_terminal(session)
    except asyncio.CancelledError:
        classification = "unconfirmed"
        stdout_payload = None
        if shutdown.signum is not None:
            status = _SIGNAL_STATUS[shutdown.signum]
            primary = "cancelled"
        else:
            status = 1
            primary = "internal"
    except ValueError as error:
        write_error(_public_value_message(error))
        return 1
    except LifecycleError as error:
        write_lifecycle(error.code, str(error))
        return 1
    except OSError:
        write_stderr("I/O error\n")
        return 1
    except Exception:
        write_stderr("internal error\n")
        return 1
    else:
        if shutdown.signum is not None:
            status = _SIGNAL_STATUS[shutdown.signum]
            primary = "cancelled"
            if classification == "unconfirmed":
                stdout_payload = None
        elif classification == "completed":
            status = 0
            primary = None
        elif classification == "model_error":
            status = 1
            primary = "model error"
        elif classification == "cancelled":
            status = 130
            primary = "cancelled"
        else:
            stdout_payload = None
            status = 1
            primary = "internal"
    if classification == "unconfirmed":
        stdout_payload = None
    if stdout_payload:
        try:
            write_stdout(stdout_payload)
        except OSError:
            write_stderr("I/O error\n")
            return 1
    if primary == "model error":
        write_stderr("model error\n")
    elif primary == "cancelled":
        write_stderr("cancelled\n")
    elif primary == "internal":
        write_stderr("internal error\n")
    return status


async def _drive_interactive(session: AgentSession, shutdown: _Shutdown) -> int:
    rendered_text = ""
    busy = False
    queued_nonempty = False
    input_failure: Exception | None = None

    async def render(event: AgentSessionEvent) -> None:
        nonlocal rendered_text
        if input_failure is not None:
            return
        if isinstance(event, AgentSessionEvent.MessageStart) and isinstance(
            event.message, AssistantMessage
        ):
            rendered_text = ""
            write_stdout("assistant start\n")
        elif isinstance(event, AgentSessionEvent.MessageUpdate):
            text = "".join(
                block.text
                for block in event.message.content
                if isinstance(block, TextContent)
            )
            if not text.startswith(rendered_text):
                raise RuntimeError("Assistant Text update is not cumulative")
            suffix = text[len(rendered_text) :]
            if suffix:
                write_stdout(encode_body(suffix))
            rendered_text = text
        elif isinstance(event, AgentSessionEvent.MessageEnd) and isinstance(
            event.message, AssistantMessage
        ):
            if not rendered_text.endswith("\n"):
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

    session.subscribe(render)
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[bytes | Exception] = asyncio.Queue()
    stdin_fd = sys.stdin.buffer.fileno()
    saved_tty, echo = _acquire_end_of_line_editing(stdin_fd)
    output_fd = sys.stdout.buffer.fileno()
    editor = _CommandInput(echo=echo, output_fd=output_fd)

    def fail_input(error: Exception) -> None:
        nonlocal input_failure
        loop.remove_reader(stdin_fd)
        input_failure = error
        if busy:
            asyncio.create_task(session.abort())
            return
        lines.put_nowait(error)

    def on_resize() -> None:
        if input_failure is not None:
            return
        try:
            editor.resize()
        except Exception as error:
            fail_input(error)

    def deliver(raw: bytes) -> None:
        nonlocal queued_nonempty
        if raw == b"":
            loop.remove_reader(stdin_fd)
            if busy:
                asyncio.create_task(session.abort())
            lines.put_nowait(raw)
            return
        if raw == _INTERRUPT:
            if busy:
                asyncio.create_task(session.abort())
                return
            lines.put_nowait(raw)
            return
        try:
            text = _decode_interactive_line(
                raw, "interactive input is not UTF-8"
            )
        except OSError as error:
            fail_input(error)
            return
        prompt = _es_trim(text)
        if busy or queued_nonempty:
            if prompt and not write_stderr(
                "busy: Product Session has an active Run\n"
            ):
                fail_input(OSError("interactive diagnostic write failed"))
            return
        queued_nonempty = bool(prompt)
        lines.put_nowait(raw)

    def input_ready() -> None:
        try:
            raw = os.read(stdin_fd, 4096)
        except OSError as error:
            fail_input(error)
            return
        if raw == b"":
            deliver(b"")
            return
        try:
            completed = editor.feed(raw)
        except Exception as error:
            fail_input(error)
            return
        for item in completed:
            deliver(item)
            if item == b"":
                return

    reader_registered = False
    winch_registered = False
    try:
        editor.begin()
        loop.add_reader(stdin_fd, input_ready)
        reader_registered = True
        try:
            loop.add_signal_handler(signal.SIGWINCH, on_resize)
            winch_registered = True
        except (NotImplementedError, RuntimeError):
            signal.signal(signal.SIGWINCH, lambda _num, _frame: on_resize())
            winch_registered = True
        while True:
            if shutdown.signum is not None:
                return _shutdown_status(shutdown)
            item = await _wait_line_or_control(lines, shutdown)
            if item == "terminate":
                return _shutdown_status(shutdown)
            if item == "interrupt" or item == _INTERRUPT:
                editor.handle_idle_interrupt()
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
            queued_nonempty = False
            prior_messages = session.messages
            prior_entries = session.sessionManager.getEntries()
            busy = True
            prompt_task = asyncio.create_task(session.prompt(prompt))
            try:
                try:
                    await _join_prompt(prompt_task, session, shutdown)
                except ValueError as error:
                    if (
                        session.messages != prior_messages
                        or session.sessionManager.getEntries() != prior_entries
                        or not session.isIdle
                    ):
                        raise RuntimeError(
                            "AgentSession ValueError changed admitted state"
                        ) from error
                    if not write_error(_public_value_message(error)):
                        raise OSError("interactive diagnostic write failed")
                    continue
                if input_failure is not None:
                    raise input_failure
                classification, _payload = _one_shot_terminal(session)
                if shutdown.signum is not None:
                    if classification == "cancelled":
                        write_stdout("run cancelled\n")
                    return _shutdown_status(shutdown)
                if classification == "unconfirmed":
                    raise RuntimeError("AgentSession prompt settlement is unconfirmed")
                write_stdout(f"run {classification}\n")
            finally:
                busy = False
                editor.reopen_idle()
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
            loop.remove_reader(stdin_fd)
        if winch_registered:
            try:
                loop.remove_signal_handler(signal.SIGWINCH)
            except (NotImplementedError, RuntimeError, OSError):
                signal.signal(signal.SIGWINCH, signal.SIG_DFL)
        try:
            _restore_tty(stdin_fd, saved_tty)
        except OSError:
            if shutdown.signum is not None:
                write_cleanup("terminal restore failed")
            else:
                write_stderr("I/O error\n")
                return 1


def _consume_task_exception(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        task.exception()


def _shutdown_status(shutdown: _Shutdown) -> int:
    assert shutdown.signum is not None
    return _SIGNAL_STATUS[shutdown.signum]


async def _wait_line_or_control(
    lines: asyncio.Queue[bytes | Exception],
    shutdown: _Shutdown,
) -> bytes | Exception | Literal["interrupt", "terminate"]:
    if shutdown.signum is not None:
        return "terminate"
    line = asyncio.create_task(lines.get())
    interrupt = asyncio.create_task(shutdown.wait_interrupt())
    terminate = asyncio.create_task(shutdown.wait())
    done, pending = await asyncio.wait(
        {line, interrupt, terminate},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    if terminate in done or shutdown.signum is not None:
        return "terminate"
    if line in done:
        return line.result()
    shutdown.clear_interrupt()
    return "interrupt"


async def _join_prompt(
    prompt_task: asyncio.Task[None],
    session: AgentSession,
    shutdown: _Shutdown,
) -> None:
    while not prompt_task.done():
        if shutdown.signum is not None:
            await prompt_task
            return
        interrupt = asyncio.create_task(shutdown.wait_interrupt())
        terminate = asyncio.create_task(shutdown.wait())
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
            shutdown.clear_interrupt()
            await session.abort()
    await prompt_task


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


def _one_shot_terminal(
    session: AgentSession,
) -> tuple[Literal["completed", "model_error", "cancelled", "unconfirmed"], bytes | None]:
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


async def _dispose(session: AgentSession, status: int) -> int:
    try:
        await session.dispose()
    except LifecycleError as error:
        write_cleanup(str(error))
        return 1 if status == 0 else status
    except Exception:
        write_cleanup("AgentSession disposal failed")
        return 1 if status == 0 else status
    return status


def _public_value_message(error: ValueError) -> str:
    message = str(error)
    if "Session file is not a valid omh session" in message:
        return "session file is invalid"
    if message.startswith("NewSessionOptions.id"):
        return "invalid session id"
    if message.startswith("--cwd") or message.startswith("CreateAgentSessionOptions.cwd"):
        return "invalid cwd"
    return "invalid value"
