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
    CreateAgentSessionOptions,
    NewSessionOptions,
    ResourceAdmissionError,
    SessionInfo,
    SessionManager,
    TrustPolicyError,
    createAgentSession,
)
from oh_my_llm import LifecycleError, ModelsError

from ._project_trust import interactive_trust_is_pending, update_project_trust
from ._prompt_resources import _es_trim
from ._session_manager import _resolve_path, _session_id
from ._terminal import (
    _SIGNAL_STATUS,
    _Shutdown,
    _decode_interactive_line,
    _drive_interactive,
    _public_value_message,
    _session_terminal,
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
  --approve, -a        Trust project resources for this construction
  --no-approve, -na    Do not trust project resources for this construction
  --help               Show this help and exit
  --version            Show version and exit

Prompt:
  One-shot accepts exactly one source: a positional PROMPT, or complete
  strict-UTF-8 non-TTY stdin when PROMPT is omitted.

Trust:
  Omission resolves saved policy, then defaultProjectTrust. Interactive
  unresolved ask offers persistent, parent, this-Session, and untrusted
  choices. Explicit --approve/--no-approve write no policy. One-shot never
  prompts: always is trusted; ask and never are untrusted.

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
    "--approve": "approve",
    "-a": "approve",
    "--no-approve": "no_approve",
    "-na": "no_approve",
    "--help": "help",
    "--version": "version",
}
_TRUST_CANCELLED = object()
_TRUST_PROMPT_WITH_PARENT = (
    "Project resources require trust:\n"
    "1. Trust for future Sessions\n"
    "2. Trust parent for future Sessions\n"
    "3. Trust this Session only\n"
    "4. Do not trust for future Sessions\n"
    "5. Do not trust this Session only\n"
    "Select 1-5, or c to cancel:"
)
_TRUST_PROMPT_ROOT = (
    "Project resources require trust:\n"
    "1. Trust for future Sessions\n"
    "3. Trust this Session only\n"
    "4. Do not trust for future Sessions\n"
    "5. Do not trust this Session only\n"
    "Select 1/3/4/5, or c to cancel:"
)
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
    approve: bool = False
    no_approve: bool = False
    end_of_options: bool = False
    positionals: tuple[str, ...] = ()


@dataclass
class _TrustChoice:
    trusted: bool
    persist: Literal["trust", "distrust", "trust_parent"] | None


@dataclass
class _Selected:
    manager: SessionManager
    identity: Literal["new", "recovered"]


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
    _reject_trust_conflicts(parsed)
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
        or parsed.approve
        or parsed.no_approve
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


def _reject_trust_conflicts(parsed: _Parsed) -> None:
    if parsed.approve and parsed.no_approve:
        raise _Usage("conflicting options")


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
    try:
        project_trusted = await _resolve_construction_trust(parsed, cwd, shutdown)
    except TrustPolicyError as error:
        _write_trust_policy_failure(error)
        return 1
    except ResourceAdmissionError:
        write_stderr("internal error\n")
        return 1
    except OSError:
        write_stderr("I/O error\n")
        return 1
    if project_trusted is _TRUST_CANCELLED:
        return 0 if shutdown.signum is None else _SIGNAL_STATUS[shutdown.signum]
    assert project_trusted is None or type(project_trusted) is bool
    return await _drive_session(
        parsed, selected, cwd, project_trusted, prompt, shutdown
    )


async def _resolve_construction_trust(
    parsed: _Parsed, cwd: str, shutdown: _Shutdown
) -> bool | None | object:
    if parsed.approve:
        return True
    if parsed.no_approve:
        return False
    if parsed.print_mode:
        return None
    if shutdown.signum is not None:
        _write_trust_cancel(shutdown.signum)
        return _TRUST_CANCELLED
    if not interactive_trust_is_pending(cwd):
        return None
    choice = await _interactive_trust(cwd, shutdown)
    if choice is None:
        return _TRUST_CANCELLED
    if choice.persist is not None:
        update_project_trust(cwd, choice.persist)
    return choice.trusted


async def _interactive_trust(
    cwd: str, shutdown: _Shutdown
) -> _TrustChoice | None:
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[bytes | OSError] = asyncio.Queue()
    redraw = asyncio.Event()
    stdin_fd = sys.stdin.buffer.fileno()
    prompt = _trust_prompt(cwd)
    winch_registered = False

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

    def on_resize() -> None:
        redraw.set()

    loop.add_reader(stdin_fd, input_ready)
    try:
        try:
            loop.add_signal_handler(signal.SIGWINCH, on_resize)
            winch_registered = True
        except (NotImplementedError, RuntimeError):
            signal.signal(signal.SIGWINCH, lambda _num, _frame: on_resize())
            winch_registered = True
        if shutdown.signum is not None:
            _write_trust_cancel(shutdown.signum)
            return None
        line: asyncio.Task[bytes | OSError] | None = None
        while True:
            write_stdout(prompt)
            if line is None or line.done():
                line = asyncio.create_task(lines.get())
            cancelled = asyncio.create_task(shutdown.wait())
            resized = asyncio.create_task(redraw.wait())
            done, _pending = await asyncio.wait(
                (line, cancelled, resized),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if resized in done:
                redraw.clear()
                cancelled.cancel()
                await asyncio.gather(cancelled, return_exceptions=True)
                resized.cancel()
                await asyncio.gather(resized, return_exceptions=True)
                continue
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
            resized.cancel()
            await asyncio.gather(resized, return_exceptions=True)
            assert line is not None
            if cancelled in done:
                line.cancel()
                await asyncio.gather(line, return_exceptions=True)
                _write_trust_cancel(shutdown.signum)
                return None
            raw = line.result()
            line = None
            if isinstance(raw, OSError):
                raise raw
            if raw == b"":
                write_stdout("trust cancelled\n")
                return None
            text = _decode_interactive_line(raw, "trust input is not UTF-8")
            trimmed = _es_trim(text)
            lowered = trimmed.lower()
            if lowered == "c" or trimmed == "\x1b":
                write_stdout("trust cancelled\n")
                return None
            selected = _trust_choice(trimmed, _trust_has_parent(cwd))
            if selected is not None:
                return selected
    finally:
        loop.remove_reader(stdin_fd)
        if winch_registered:
            try:
                loop.remove_signal_handler(signal.SIGWINCH)
            except (NotImplementedError, RuntimeError, OSError):
                signal.signal(signal.SIGWINCH, signal.SIG_DFL)


def _trust_has_parent(cwd: str) -> bool:
    path = os.path.realpath(cwd)
    return os.path.dirname(path) != path


def _trust_prompt(cwd: str) -> str:
    if _trust_has_parent(cwd):
        return _TRUST_PROMPT_WITH_PARENT
    return _TRUST_PROMPT_ROOT


def _trust_choice(selected: str, has_parent: bool) -> _TrustChoice | None:
    if selected == "1":
        return _TrustChoice(True, "trust")
    if selected == "2" and has_parent:
        return _TrustChoice(True, "trust_parent")
    if selected == "3":
        return _TrustChoice(True, None)
    if selected == "4":
        return _TrustChoice(False, "distrust")
    if selected == "5":
        return _TrustChoice(False, None)
    return None


def _write_trust_policy_failure(error: TrustPolicyError) -> None:
    write_stderr(
        f'trust policy failed operation="{error.operation}" '
        f'stage="{error.stage}"\n'
    )


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
    project_trusted: bool | None,
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
    except TrustPolicyError as error:
        _write_trust_policy_failure(error)
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
        classification, stdout_payload = _session_terminal(session)
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
