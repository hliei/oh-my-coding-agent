from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal

from ticket_23_scenario import (
    hang_pythonpath,
    identity_record,
    isolated_env,
    run_omh,
    scripted_pythonpath,
    session_files,
    text_sse,
)
from ticket_25_scenario import (
    blocking_pythonpath,
    close_process,
    control_observations,
    failing_dispose_pythonpath,
    platform_signal_observations,
    read_until,
    seed_incomplete_session,
    signal_observations,
    spawn_interactive,
    spawn_one_shot_hang,
    wait_fd,
    wait_started,
)


ROOT = Path(__file__).parents[2]


def _expected(case_id: str) -> object:
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    case = next(item for item in corpus["cases"] if item["id"] == case_id)
    return case.get("omhExpectation", case["observations"])


def test_idle_ctrl_c_redraws_prompt_and_does_not_exit(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = isolated_env(home)
    process, master = spawn_interactive(
        ["--trust-project", "--no-session", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
    )
    try:
        first = read_until(master, b"> ")
        process.send_signal(signal.SIGINT)
        redrawn = read_until(master, b"> ")
        os.write(master, b"\x04")
        assert process.wait(timeout=10) == 0
        assert process.stderr is not None
        assert process.stderr.read() == b""
    finally:
        close_process(process, master)

    assert first == b"session memory-id new\r\n> " or first.endswith(b"> ")
    assert b"> " in redrawn
    assert session_files(home) == []


def test_idle_sighup_exits_129_without_a_run_record(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = isolated_env(home)
    process, master = spawn_interactive(
        ["--trust-project", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
    )
    try:
        transcript = read_until(master, b"> ")
        process.send_signal(signal.SIGHUP)
        assert process.wait(timeout=10) == 129
        assert process.stderr is not None
        stderr = process.stderr.read()
    finally:
        close_process(process, master)

    assert b"run " not in transcript
    assert b"cancelled" not in stderr
    assert b"resume" not in stderr.lower()
    assert b"Traceback" not in stderr


def test_idle_sigterm_exits_143_without_a_run_record(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = isolated_env(home)
    process, master = spawn_interactive(
        ["--trust-project", "--no-session", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
    )
    try:
        read_until(master, b"> ")
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 143
        assert process.stderr is not None
        assert process.stderr.read() == b""
    finally:
        close_process(process, master)


def test_later_sigterm_joins_first_sighup_status(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inject, started_read, started_write, release_read, release_write, cancel_read, cancel_write = (
        blocking_pythonpath(home, text_sse("late"), hold_cancel=True)
    )
    env = isolated_env(home, pythonpath=inject)
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
        assert wait_fd(started_read) == b"1"
        process.send_signal(signal.SIGHUP)
        assert wait_fd(started_read) == b"c"
        process.send_signal(signal.SIGTERM)
        os.write(cancel_write, b"1")
        transcript = read_until(master, b"run cancelled\r\n")
        os.write(release_write, b"1")
        assert process.wait(timeout=10) == 129
        assert process.stderr is not None
        stderr = process.stderr.read()
    finally:
        close_process(process, master, started_read, release_write, cancel_write)

    assert transcript.count(b"run cancelled") == 1
    assert b"run completed" not in transcript
    assert b"resume" not in stderr.lower()


def test_repeated_ctrl_c_during_settlement_joins_and_returns_to_idle(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inject, started_read, started_write, release_read, release_write, cancel_read, cancel_write = (
        blocking_pythonpath(home, text_sse("late"), hold_cancel=True)
    )
    env = isolated_env(home, pythonpath=inject)
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
        assert wait_fd(started_read) == b"1"
        process.send_signal(signal.SIGINT)
        assert wait_fd(started_read) == b"c"
        process.send_signal(signal.SIGINT)
        assert process.poll() is None
        os.write(cancel_write, b"1")
        os.write(release_write, b"1")
        cancelled = read_until(master, b"run cancelled\r\n> ")
        os.write(master, b"\x04")
        assert process.wait(timeout=10) == 0
        assert process.stderr is not None
        assert process.stderr.read() == b""
    finally:
        close_process(process, master, started_read, release_write, cancel_write)

    assert cancelled.count(b"run cancelled") == 1


def test_escape_does_not_cancel_an_active_run(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inject, started_read, started_write, release_read, release_write, _cancel_read, cancel_write = (
        blocking_pythonpath(home, text_sse("escaped"))
    )
    env = isolated_env(home, pythonpath=inject)
    env["OMH_TEST_STARTED_FD"] = str(started_write)
    env["OMH_TEST_RELEASE_FD"] = str(release_read)
    process, master = spawn_interactive(
        ["--trust-project", "--no-session", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
        pass_fds=(started_write, release_read),
    )
    os.close(started_write)
    os.close(release_read)
    try:
        read_until(master, b"> ")
        os.write(master, b"hang\n")
        assert wait_fd(started_read) == b"1"
        os.write(master, b"\x1b\n")
        assert process.stderr is not None
        stderr_fd = process.stderr.fileno()
        assert select.select([stderr_fd], [], [], 10.0)[0] == [stderr_fd]
        assert os.read(stderr_fd, 4096) == (
            b"busy: Product Session has an active Run\n"
        )
        os.write(release_write, b"1")
        completed = read_until(master, b"run completed\r\n> ")
        os.write(master, b"\x04")
        assert process.wait(timeout=10) == 0
    finally:
        close_process(process, master, started_read, release_write, cancel_write)

    assert b"run cancelled" not in completed
    assert b"escaped" in completed


def test_trust_sighup_cancels_before_session_construction(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = isolated_env(home)
    process, master = spawn_interactive(
        ["--cwd", os.fspath(workspace)], env=env, cwd=workspace
    )
    try:
        read_until(master, b"? [y/n] ")
        process.send_signal(signal.SIGHUP)
        assert process.wait(timeout=10) == 129
        assert process.stderr is not None
        stderr = process.stderr.read()
    finally:
        close_process(process, master)

    assert stderr == b"cancelled\n"
    assert session_files(home) == []


def test_one_shot_sighup_and_sigterm_use_conventional_statuses(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    statuses: list[int] = []
    for signum, expected in ((signal.SIGHUP, 129), (signal.SIGTERM, 143)):
        home = tmp_path / f"home-{expected}"
        started = home / "started"
        env = isolated_env(home, pythonpath=hang_pythonpath(home, started))
        process, master, slave = spawn_one_shot_hang(
            [
                "--print",
                "--no-session",
                "--cwd",
                os.fspath(workspace),
                "hang",
            ],
            env=env,
            cwd=workspace,
        )
        try:
            wait_started(process, started)
            process.send_signal(signum)
            stdout, stderr = process.communicate(timeout=10)
        finally:
            os.close(master)
            os.close(slave)
        assert process.returncode == expected, (expected, stdout, stderr)
        assert b"cancelled\n" in stderr
        assert b"session " in stderr
        assert b"Traceback" not in stderr
        assert stdout == b"" or b"run " not in stdout
        statuses.append(process.returncode)
    assert statuses == [129, 143]


def test_one_shot_later_sigterm_joins_first_sighup(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inject, started_read, started_write, release_read, release_write, cancel_read, cancel_write = (
        blocking_pythonpath(home, text_sse("late"), hold_cancel=True)
    )
    env = isolated_env(home, pythonpath=inject)
    env["OMH_TEST_STARTED_FD"] = str(started_write)
    env["OMH_TEST_RELEASE_FD"] = str(release_read)
    env["OMH_TEST_CANCEL_FD"] = str(cancel_read)
    process, master, slave = spawn_one_shot_hang(
        ["--print", "--no-session", "--cwd", os.fspath(workspace), "hang"],
        env=env,
        cwd=workspace,
        pass_fds=(started_write, release_read, cancel_read),
    )
    os.close(started_write)
    os.close(release_read)
    os.close(cancel_read)
    try:
        assert wait_fd(started_read) == b"1"
        process.send_signal(signal.SIGHUP)
        assert wait_fd(started_read) == b"c"
        process.send_signal(signal.SIGTERM)
        os.write(cancel_write, b"1")
        os.write(release_write, b"1")
        stdout, stderr = process.communicate(timeout=10)
    finally:
        os.close(master)
        os.close(slave)
        for descriptor in (started_read, release_write, cancel_write):
            os.close(descriptor)
    assert process.returncode == 129, (stdout, stderr)
    assert b"cancelled\n" in stderr


def test_cleanup_failure_does_not_replace_signal_status(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = isolated_env(home, pythonpath=failing_dispose_pythonpath(home))
    process, master = spawn_interactive(
        ["--trust-project", "--no-session", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
    )
    try:
        read_until(master, b"> ")
        process.send_signal(signal.SIGHUP)
        assert process.wait(timeout=10) == 129
        assert process.stderr is not None
        stderr = process.stderr.read()
    finally:
        close_process(process, master)

    assert stderr == b"lifecycle cleanup: AgentSession disposal failed\n"
    assert b"Traceback" not in stderr
    assert b"RuntimeError" not in stderr
    assert b"canary" not in stderr


def test_selectors_recover_incomplete_jsonl_and_ephemeral_stays_new(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path, snapshot = seed_incomplete_session(home, workspace, "incomplete-1")
    env = isolated_env(
        home, pythonpath=scripted_pythonpath(home, [text_sse("continued")])
    )
    by_id = run_omh(
        [
            "--print",
            "--session-id",
            "incomplete-1",
            "--cwd",
            os.fspath(workspace),
            "continue",
        ],
        env=env,
        cwd=workspace,
        tty_stdin=True,
    )
    continued = run_omh(
        ["--print", "--continue", "--cwd", os.fspath(workspace), "again"],
        env=isolated_env(
            home, pythonpath=scripted_pythonpath(home / "c", [text_sse("again")])
        ),
        cwd=workspace,
        tty_stdin=True,
    )
    by_path = run_omh(
        [
            "--print",
            "--session",
            os.fspath(path),
            "--cwd",
            os.fspath(workspace),
            "path",
        ],
        env=isolated_env(
            home, pythonpath=scripted_pythonpath(home / "p", [text_sse("path")])
        ),
        cwd=workspace,
        tty_stdin=True,
    )
    ephemeral = run_omh(
        [
            "--print",
            "--no-session",
            "--session-id",
            "ephemeral-1",
            "--cwd",
            os.fspath(workspace),
            "mem",
        ],
        env=isolated_env(
            home, pythonpath=scripted_pythonpath(home / "e", [text_sse("mem")])
        ),
        cwd=workspace,
        tty_stdin=True,
    )

    assert identity_record(by_id.stderr) == ("incomplete-1", "recovered")
    assert by_id.returncode == 0
    assert by_id.stdout == b"continued"
    assert identity_record(continued.stderr)[1] == "recovered"
    assert identity_record(by_path.stderr) == ("incomplete-1", "recovered")
    assert identity_record(ephemeral.stderr) == ("ephemeral-1", "new")
    assert ephemeral.returncode == 0
    assert snapshot in path.read_bytes()


def test_recovery_reobtains_trust_and_leaves_idle_history_unchanged(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = isolated_env(
        home, pythonpath=scripted_pythonpath(home, [text_sse("first")])
    )
    created = run_omh(
        [
            "--print",
            "--session-id",
            "keep-me",
            "--cwd",
            os.fspath(workspace),
            "one",
        ],
        env=env,
        cwd=workspace,
        tty_stdin=True,
    )
    files = session_files(home)
    before = files[0].read_bytes()
    process, master = spawn_interactive(
        ["--session-id", "keep-me", "--cwd", os.fspath(workspace)],
        env=isolated_env(home),
        cwd=workspace,
    )
    try:
        trust = read_until(master, b"? [y/n] ")
        os.write(master, b"n\n")
        startup = read_until(master, b"> ")
        os.write(master, b"\x04")
        assert process.wait(timeout=10) == 0
        assert process.stderr is not None
        stderr = process.stderr.read()
    finally:
        close_process(process, master)

    assert b"Trust project resources" in trust
    assert b"session keep-me recovered\r\n> " in startup
    assert b"resume" not in stderr.lower()
    assert b"omh --session" not in stderr
    assert files[0].read_bytes() == before
    assert created.returncode == 0


def test_later_sigint_joins_in_progress_sighup_shutdown(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inject, started_read, started_write, release_read, release_write, cancel_read, cancel_write = (
        blocking_pythonpath(home, text_sse("late"), hold_cancel=True)
    )
    env = isolated_env(home, pythonpath=inject)
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
        assert wait_fd(started_read) == b"1"
        process.send_signal(signal.SIGHUP)
        assert wait_fd(started_read) == b"c"
        process.send_signal(signal.SIGINT)
        os.write(cancel_write, b"1")
        os.write(release_write, b"1")
        transcript = read_until(master, b"run cancelled\r\n")
        assert process.wait(timeout=10) == 129
    finally:
        close_process(process, master, started_read, release_write, cancel_write)

    assert transcript.count(b"run cancelled") == 1


def test_published_session_is_disposed_exactly_once_on_eof(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inject = home / "inject-count-dispose"
    inject.mkdir(parents=True)
    counter = inject / "dispose-count.txt"
    counter.write_text("0", encoding="utf-8")
    (inject / "sitecustomize.py").write_text(
        "from __future__ import annotations\n"
        "from pathlib import Path\n"
        "from oh_my_coding_agent import AgentSession\n"
        f"COUNTER = Path({os.fspath(counter)!r})\n"
        "ORIGINAL = AgentSession.dispose\n"
        "async def counting_dispose(self):\n"
        "    COUNTER.write_text(str(int(COUNTER.read_text(encoding='utf-8')) + 1), encoding='utf-8')\n"
        "    await ORIGINAL(self)\n"
        "AgentSession.dispose = counting_dispose\n",
        encoding="utf-8",
    )
    env = isolated_env(home, pythonpath=inject)
    process, master = spawn_interactive(
        ["--trust-project", "--no-session", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
    )
    try:
        read_until(master, b"> ")
        os.write(master, b"\x04")
        assert process.wait(timeout=10) == 0
    finally:
        close_process(process, master)

    assert counter.read_text(encoding="utf-8") == "1"


def test_interactive_recovers_incomplete_jsonl_without_a_new_run(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path, snapshot = seed_incomplete_session(home, workspace, "incomplete-1")
    process, master = spawn_interactive(
        [
            "--trust-project",
            "--session-id",
            "incomplete-1",
            "--cwd",
            os.fspath(workspace),
        ],
        env=isolated_env(home),
        cwd=workspace,
    )
    try:
        startup = read_until(master, b"> ")
        os.write(master, b"\x04")
        assert process.wait(timeout=10) == 0
        assert process.stderr is not None
        assert process.stderr.read() == b""
    finally:
        close_process(process, master)

    assert b"session incomplete-1 recovered\r\n> " in startup
    assert path.read_bytes() == snapshot


def test_ticket_25_matrix_closes_command_mode_signal_rows(tmp_path: Path) -> None:
    matrix = json.loads((ROOT / "conformance/obligation-matrix.json").read_text())
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    required = {
        "omh-v0.repl-control-inputs": "reference.repl-control-inputs",
        "omh-v0.repl-signal-status": "reference.repl-signal-status",
        "omh-v0.command-posix-terminal-signals": (
            "reference.command-posix-terminal-signals"
        ),
    }
    obligations = {row["id"]: row for row in matrix["obligations"]}
    cases = {case["id"]: case for case in corpus["cases"]}
    for obligation_id, case_id in required.items():
        row = obligations[obligation_id]
        assert cases[case_id]["obligation"] == obligation_id
        assert row["corpusCase"] == case_id
        assert row["executableCases"] == ["ticket-25-signals", "ticket-25-installed"]

    diagnostics = obligations["omh-v0.redacted-command-diagnostics"]
    assert "ticket-25-signals" in diagnostics["executableCases"]
    one_shot = obligations["omh-v0.deterministic-one-shot-terminal-carrier"]
    assert "ticket-25-signals" in one_shot["executableCases"]

    assert control_observations(
        tmp_path / "control-home", tmp_path / "control-workspace"
    ) == _expected("reference.repl-control-inputs")
    assert signal_observations(
        tmp_path / "signal-home", tmp_path / "signal-workspace"
    ) == _expected("reference.repl-signal-status")
    assert platform_signal_observations(
        tmp_path / "platform-home", tmp_path / "platform-workspace"
    ) == _expected("reference.command-posix-terminal-signals")
