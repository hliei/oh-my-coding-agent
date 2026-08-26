from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import termios
import tempfile
import time
from typing import Iterable

sys.path.insert(0, str(Path(__file__).parent))

from ticket_23_scenario import omh_bin


def read_until(master: int, marker: bytes, *, timeout: float = 10.0) -> bytes:
    output = bytearray()
    deadline = time.monotonic() + timeout
    while marker not in output:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"PTY output did not contain {marker!r}: {bytes(output)!r}"
            )
        readable, _, _ = select.select([master], [], [], remaining)
        if not readable:
            continue
        output.extend(os.read(master, 4096))
    return bytes(output)


def spawn_interactive(
    args: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    pass_fds: Iterable[int] = (),
) -> tuple[subprocess.Popen[bytes], int]:
    master, slave = pty.openpty()
    attributes = termios.tcgetattr(slave)
    attributes[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, attributes)
    process = subprocess.Popen(
        [os.fspath(omh_bin()), *args],
        stdin=slave,
        stdout=slave,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        pass_fds=tuple(pass_fds),
    )
    os.close(slave)
    return process, master


def close_process(process: subprocess.Popen[bytes], *descriptors: int) -> None:
    for descriptor in descriptors:
        os.close(descriptor)
    if process.poll() is None:
        process.kill()
        process.wait()


def blocking_pythonpath(
    home: Path, body: bytes, *, hold_cancel: bool = False
) -> tuple[Path, int, int, int, int, int, int]:
    inject = home / "inject-blocking"
    inject.mkdir(parents=True, exist_ok=True)
    started_read, started_write = os.pipe()
    release_read, release_write = os.pipe()
    cancel_read, cancel_write = os.pipe()
    hold = (
        "        except asyncio.CancelledError:\n"
        "            os.write(STARTED, b'c')\n"
        "            await asyncio.shield(asyncio.to_thread(os.read, CANCEL, 1))\n"
        "            raise\n"
        if hold_cancel
        else "        except asyncio.CancelledError:\n"
        "            os.write(STARTED, b'c')\n"
        "            raise\n"
    )
    (inject / "sitecustomize.py").write_text(
        "from __future__ import annotations\n"
        "import asyncio\n"
        "import os\n"
        "import httpx\n"
        f"BODY = {body!r}\n"
        "STARTED = int(os.environ['OMH_TEST_STARTED_FD'])\n"
        "RELEASE = int(os.environ['OMH_TEST_RELEASE_FD'])\n"
        + ("CANCEL = int(os.environ['OMH_TEST_CANCEL_FD'])\n" if hold_cancel else "")
        + "class BarrierTransport(httpx.AsyncBaseTransport):\n"
        "    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:\n"
        "        os.write(STARTED, b'1')\n"
        "        try:\n"
        "            await asyncio.to_thread(os.read, RELEASE, 1)\n"
        + hold
        + "        return httpx.Response(200, content=BODY, headers={'Content-Type': 'text/event-stream'}, request=request)\n"
        "class FakeClient(httpx.AsyncClient):\n"
        "    def __init__(self, **kwargs: object) -> None:\n"
        "        values = dict(kwargs)\n"
        "        values['transport'] = BarrierTransport()\n"
        "        super().__init__(**values)\n"
        "httpx.AsyncClient = FakeClient\n",
        encoding="utf-8",
    )
    return (
        inject,
        started_read,
        started_write,
        release_read,
        release_write,
        cancel_read,
        cancel_write,
    )


def failing_dispose_pythonpath(home: Path) -> Path:
    inject = home / "inject-dispose"
    inject.mkdir(parents=True, exist_ok=True)
    (inject / "sitecustomize.py").write_text(
        "from __future__ import annotations\n"
        "from oh_my_coding_agent import AgentSession\n"
        "from oh_my_llm import LifecycleError\n"
        "ORIGINAL = AgentSession.dispose\n"
        "async def failing_dispose(self):\n"
        "    await ORIGINAL(self)\n"
        "    raise LifecycleError('disposal', 'AgentSession disposal failed', causes=(RuntimeError('canary'),))\n"
        "AgentSession.dispose = failing_dispose\n",
        encoding="utf-8",
    )
    return inject


def spawn_one_shot_hang(
    args: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    pass_fds: Iterable[int] = (),
) -> tuple[subprocess.Popen[bytes], int, int]:
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [os.fspath(omh_bin()), *args],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        pass_fds=tuple(pass_fds),
    )
    return process, master, slave


def wait_started(process: subprocess.Popen[bytes], started: Path) -> None:
    deadline = time.monotonic() + 10
    while not started.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError((process.returncode, stdout, stderr))
        if time.monotonic() > deadline:
            process.kill()
            raise AssertionError("process did not start the Provider request")


def wait_fd(descriptor: int, *, timeout: float = 10.0) -> bytes:
    readable, _, _ = select.select([descriptor], [], [], timeout)
    if not readable:
        raise AssertionError("timed out waiting for test barrier")
    return os.read(descriptor, 1)


def seed_incomplete_session(
    home: Path, workspace: Path, session_id: str
) -> tuple[Path, bytes]:
    import asyncio
    from typing import Any

    import httpx

    from oh_my_coding_agent import (
        CreateAgentSessionOptions,
        NewSessionOptions,
        SessionManager,
        createAgentSession,
    )
    from oh_my_llm import UserMessage

    from ticket_23_scenario import text_sse

    previous_home = os.environ.get("HOME")
    previous_key = os.environ.get("DEEPSEEK_API_KEY")
    os.environ["HOME"] = os.fspath(home)
    os.environ["DEEPSEEK_API_KEY"] = "omh-conformance-canary"

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            return httpx.Response(
                200,
                content=text_sse("seeded"),
                headers={"Content-Type": "text/event-stream"},
                request=request,
            )

    original = httpx.AsyncClient

    class FakeClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            values = dict(kwargs)
            values["transport"] = Transport()
            super().__init__(**values)

    async def seed() -> tuple[Path, bytes]:
        manager = SessionManager.create(
            os.fspath(workspace),
            options=NewSessionOptions(id=session_id),
        )
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    sessionManager=manager,
                )
            )
        ).session
        await session.prompt("complete first")
        manager.appendMessage(
            UserMessage(content="interrupted request", timestamp=3)
        )
        path = manager.getSessionFile()
        assert path is not None
        snapshot = Path(path).read_bytes()
        await session.dispose()
        return Path(path), snapshot

    httpx.AsyncClient = FakeClient  # type: ignore[misc]
    try:
        return asyncio.run(seed())
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]
        if previous_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = previous_home
        if previous_key is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = previous_key


def control_observations(home: Path, workspace: Path) -> dict[str, object]:
    from ticket_23_scenario import isolated_env, text_sse

    workspace.mkdir(parents=True, exist_ok=True)
    env = isolated_env(home)
    process, master = spawn_interactive(
        ["--trust-project", "--no-session", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
    )
    try:
        read_until(master, b"> ")
        process.send_signal(signal.SIGINT)
        redrawn = read_until(master, b"> ")
        os.write(master, b"\x04")
        idle_status = process.wait(timeout=10)
        stderr = process.stderr.read() if process.stderr is not None else b""
    finally:
        close_process(process, master)

    inject, started_read, started_write, release_read, release_write, cancel_read, cancel_write = (
        blocking_pythonpath(home / "repeat", text_sse("late"), hold_cancel=True)
    )
    env = isolated_env(home / "repeat-home", pythonpath=inject)
    env["OMH_TEST_STARTED_FD"] = str(started_write)
    env["OMH_TEST_RELEASE_FD"] = str(release_read)
    env["OMH_TEST_CANCEL_FD"] = str(cancel_read)
    process, master = spawn_interactive(
        ["--trust-project", "--no-session", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
        pass_fds=(started_write, release_read, cancel_read),
    )
    os.close(started_write)
    os.close(release_read)
    os.close(cancel_read)
    try:
        read_until(master, b"> ")
        os.write(master, b"hang\n")
        wait_fd(started_read)
        process.send_signal(signal.SIGINT)
        wait_fd(started_read)
        process.send_signal(signal.SIGINT)
        still_running = process.poll() is None
        os.write(cancel_write, b"1")
        os.write(release_write, b"1")
        cancelled = read_until(master, b"run cancelled\r\n> ")
        os.write(master, b"\x04")
        repeat_status = process.wait(timeout=10)
    finally:
        close_process(process, master, started_read, release_write, cancel_write)

    return {
        "A": {"idleThenEof": idle_status, "repeatedThenEof": repeat_status},
        "L": {
            "idleRedrew": b"> " in redrawn,
            "repeatedJoined": still_running,
        },
        "T": {
            "runCancelledOnce": cancelled.count(b"run cancelled") == 1,
            "stderrEmpty": stderr == b"",
        },
        "E": {"noRecoveryHint": stderr == b"" and b"resume" not in cancelled.lower()},
        "C": "abort_and_reuse_without_force_path",
    }


def signal_observations(home: Path, workspace: Path) -> dict[str, object]:
    from ticket_23_scenario import isolated_env

    workspace.mkdir(parents=True, exist_ok=True)
    env = isolated_env(home)
    hup, master = spawn_interactive(
        ["--trust-project", "--no-session", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
    )
    try:
        read_until(master, b"> ")
        hup.send_signal(signal.SIGHUP)
        hup_status = hup.wait(timeout=10)
        hup_stderr = hup.stderr.read() if hup.stderr is not None else b""
    finally:
        close_process(hup, master)

    term, master = spawn_interactive(
        ["--trust-project", "--no-session", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
    )
    try:
        read_until(master, b"> ")
        term.send_signal(signal.SIGTERM)
        term_status = term.wait(timeout=10)
    finally:
        close_process(term, master)

    trust, master = spawn_interactive(
        ["--cwd", os.fspath(workspace)], env=env, cwd=workspace
    )
    try:
        read_until(master, b"? [y/n] ")
        trust.send_signal(signal.SIGHUP)
        trust_status = trust.wait(timeout=10)
        trust_stderr = trust.stderr.read() if trust.stderr is not None else b""
    finally:
        close_process(trust, master)

    cleanup_env = isolated_env(
        home / "cleanup", pythonpath=failing_dispose_pythonpath(home / "cleanup")
    )
    cleanup, master = spawn_interactive(
        ["--trust-project", "--no-session", "--cwd", os.fspath(workspace)],
        env=cleanup_env,
        cwd=workspace,
    )
    try:
        read_until(master, b"> ")
        cleanup.send_signal(signal.SIGHUP)
        cleanup_status = cleanup.wait(timeout=10)
        cleanup_stderr = (
            cleanup.stderr.read() if cleanup.stderr is not None else b""
        )
    finally:
        close_process(cleanup, master)

    return {
        "A": {
            "idleHup": hup_status,
            "idleTerm": term_status,
            "trustHup": trust_status,
            "cleanupHup": cleanup_status,
        },
        "L": "first_signal_fixes_status",
        "T": {
            "idleHupStderr": hup_stderr.decode("utf-8"),
            "trustHupStderr": trust_stderr.decode("utf-8"),
            "cleanupStderr": cleanup_stderr.decode("utf-8"),
        },
        "E": {
            "traceback": b"Traceback" in hup_stderr + trust_stderr + cleanup_stderr,
            "hint": b"resume" in (hup_stderr + trust_stderr).lower(),
            "cause": b"canary" in cleanup_stderr,
        },
        "C": "conventional_signal_status_without_force_or_hint",
    }


def platform_signal_observations(home: Path, workspace: Path) -> dict[str, object]:
    observed = signal_observations(home, workspace)
    statuses = observed["A"]
    assert isinstance(statuses, dict)
    return {
        "A": statuses,
        "L": "real_posix_signal_and_pty",
        "T": observed["T"],
        "E": {"usedRealPty": True, "usedRealSignals": True},
        "C": "release_row_posix_terminal_signals",
    }


def main() -> None:
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        actual = {
            "reference.repl-control-inputs": control_observations(
                root / "control-home", root / "control-workspace"
            ),
            "reference.repl-signal-status": signal_observations(
                root / "signal-home", root / "signal-workspace"
            ),
            "reference.command-posix-terminal-signals": (
                platform_signal_observations(
                    root / "platform-home", root / "platform-workspace"
                )
            ),
        }
    print(json.dumps(actual, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
