from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import time

from ticket_23_scenario import (
    HELP_TEXT,
    admission_observations,
    diagnostics_observations,
    hang_pythonpath,
    identity_record,
    isolated_env,
    model_error_sse,
    omh_bin,
    one_shot_observations,
    run_omh,
    scripted_pythonpath,
    session_files,
    text_sse,
)


ROOT = Path(__file__).parents[2]


def _expected(case_id: str) -> object:
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    case = next(item for item in corpus["cases"] if item["id"] == case_id)
    return case.get("omhExpectation", case["observations"])


def test_help_and_version_are_pure_terminal_actions(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    env = isolated_env(home)
    env.pop("DEEPSEEK_API_KEY", None)
    (workspace / "secret.txt").write_text("leave-me", encoding="utf-8")

    help_run = run_omh(["--help"], env=env, cwd=workspace)
    version_run = run_omh(["--version"], env=env, cwd=workspace)
    combined = run_omh(["--help", "--version"], env=env, cwd=workspace)
    help_with_end = run_omh(["--help", "--"], env=env, cwd=workspace)
    help_with_prompt = run_omh(["--help", "hello"], env=env, cwd=workspace)
    version_with_print = run_omh(["--version", "--print"], env=env, cwd=workspace)

    assert help_run.returncode == 0
    assert help_run.stderr == b""
    assert help_run.stdout.decode("utf-8") == HELP_TEXT
    assert version_run.returncode == 0
    assert version_run.stderr == b""
    assert version_run.stdout == b"omh 0.1.0\n"
    assert combined.returncode == 2
    assert combined.stdout == b""
    assert combined.stderr.startswith(b"usage: ")
    assert help_with_end.returncode == 2
    assert help_with_prompt.returncode == 2
    assert version_with_print.returncode == 2
    assert session_files(home) == []
    assert (workspace / "secret.txt").read_text(encoding="utf-8") == "leave-me"


def test_closed_flag_set_rejects_unknown_repeated_and_conflicting_selectors(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    env = isolated_env(home)
    cases = [
        ["--print", "-p", "hello"],
        ["--print", "--print", "hello"],
        ["--print", "--continue", "--session", "abc"],
        ["--print", "--session", "abc", "--session-id", "abc"],
        ["--print", "--no-session", "--continue"],
        ["--print", "--no-session", "--session", "abc"],
        ["--print", "--cwd"],
        ["--print", "--session-id", "bad id"],
        ["--print", "--unknown"],
    ]
    for args in cases:
        completed = run_omh(args, env=env, cwd=workspace, tty_stdin=True)
        assert completed.returncode == 2, args
        assert completed.stdout == b""
        assert completed.stderr.startswith(b"usage: ")
        assert b"Traceback" not in completed.stderr
        assert session_files(home) == []


def test_one_shot_source_admission_rejects_conflicting_empty_and_invalid_utf8(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    env = isolated_env(home)
    missing = run_omh(["--print"], env=env, cwd=workspace, stdin=b"")
    empty = run_omh(["--print"], env=env, cwd=workspace, stdin=b"\t  \n")
    both = run_omh(["--print", "hello"], env=env, cwd=workspace, stdin=b"other")
    extra = run_omh(
        ["--print", "one", "two"], env=env, cwd=workspace, tty_stdin=True
    )
    invalid = run_omh(["--print"], env=env, cwd=workspace, stdin=b"\xff")
    interactive = run_omh([], env=env, cwd=workspace, stdin=b"hi\n")

    for completed, reason in (
        (missing, b"usage: missing prompt\n"),
        (empty, b"usage: empty prompt\n"),
        (both, b"usage: prompt and stdin both provided\n"),
        (extra, b"usage: extra prompt\n"),
        (invalid, b"usage: invalid UTF-8\n"),
        (interactive, b"usage: interactive mode requires a TTY; use --print\n"),
    ):
        assert completed.returncode == 2
        assert completed.stdout == b""
        assert completed.stderr == reason
        assert session_files(home) == []


def test_one_shot_writes_identity_then_only_assistant_text(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    env = isolated_env(
        home, pythonpath=scripted_pythonpath(home, [text_sse("hello\nworld")])
    )
    completed = run_omh(
        ["--print", "--cwd", os.fspath(workspace), "  ping  "],
        env=env,
        cwd=workspace,
        tty_stdin=True,
    )
    session_id, kind = identity_record(completed.stderr)

    assert completed.returncode == 0
    assert completed.stdout == b"hello\nworld"
    assert b"run " not in completed.stdout
    assert b"tool " not in completed.stdout
    assert b"assistant " not in completed.stdout
    assert completed.stderr == f"session {session_id} new\n".encode("ascii")
    assert kind == "new"
    assert session_files(home)


def test_piped_stdin_is_the_one_shot_source(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    env = isolated_env(home, pythonpath=scripted_pythonpath(home, [text_sse("piped")]))
    completed = run_omh(
        ["--print", "--no-session", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
        stdin=b"  from-stdin  \n",
    )
    _session_id, kind = identity_record(completed.stderr)
    assert completed.returncode == 0
    assert completed.stdout == b"piped"
    assert kind == "new"
    assert session_files(home) == []


def test_session_selectors_recover_create_and_conflict(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    env = isolated_env(
        home,
        pythonpath=scripted_pythonpath(
            home,
            [text_sse("first"), text_sse("id"), text_sse("again"), text_sse("path")],
        ),
    )
    created = run_omh(
        [
            "--print",
            "--session-id",
            "chosen-id",
            "--cwd",
            os.fspath(workspace),
            "one",
        ],
        env=env,
        cwd=workspace,
        tty_stdin=True,
    )
    created_id, created_kind = identity_record(created.stderr)
    recovered = run_omh(
        [
            "--print",
            "--session-id",
            "chosen-id",
            "--cwd",
            os.fspath(workspace),
            "two",
        ],
        env=env,
        cwd=workspace,
        tty_stdin=True,
    )
    recovered_id, recovered_kind = identity_record(recovered.stderr)
    continued = run_omh(
        ["--print", "--continue", "--cwd", os.fspath(workspace), "three"],
        env=env,
        cwd=workspace,
        tty_stdin=True,
    )
    continued_id, continued_kind = identity_record(continued.stderr)
    files = session_files(home)
    by_path = run_omh(
        [
            "--print",
            "--session",
            os.fspath(files[0]),
            "--cwd",
            os.fspath(workspace),
            "four",
        ],
        env=env,
        cwd=workspace,
        tty_stdin=True,
    )
    missing = run_omh(
        ["--print", "--session", "no-such-session", "--cwd", os.fspath(workspace), "x"],
        env=env,
        cwd=workspace,
        tty_stdin=True,
    )
    prefix = run_omh(
        ["--print", "--session", "chosen", "--cwd", os.fspath(workspace), "five"],
        env=isolated_env(home, pythonpath=scripted_pythonpath(home, [text_sse("pfx")])),
        cwd=workspace,
        tty_stdin=True,
    )

    assert created.returncode == 0
    assert created_id == "chosen-id"
    assert created_kind == "new"
    assert recovered.returncode == 0
    assert recovered_id == "chosen-id"
    assert recovered_kind == "recovered"
    assert continued.returncode == 0
    assert continued_id == "chosen-id"
    assert continued_kind == "recovered"
    assert identity_record(by_path.stderr) == ("chosen-id", "recovered")
    assert missing.returncode == 1
    assert missing.stdout == b""
    assert missing.stderr == b"error: session not found\n"
    assert identity_record(prefix.stderr) == ("chosen-id", "recovered")


def test_one_shot_never_prompts_for_trust_and_omission_is_untrusted(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    extension = workspace / ".omh" / "extensions"
    extension.mkdir(parents=True)
    (extension / "trip.py").write_text(
        "from oh_my_coding_agent import ExtensionAPI\n"
        "def register(api: ExtensionAPI) -> None:\n"
        "    raise RuntimeError('trusted')\n",
        encoding="utf-8",
    )
    env = isolated_env(home, pythonpath=scripted_pythonpath(home, [text_sse("ok")]))
    completed = run_omh(
        ["--print", "--cwd", os.fspath(workspace), "hello"],
        env=env,
        cwd=workspace,
        tty_stdin=True,
    )
    assert completed.returncode == 0
    assert completed.stdout == b"ok"
    assert b"Trust project" not in completed.stdout
    assert b"Trust project" not in completed.stderr


def test_trust_project_flag_trusts_without_prompting(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    extension = workspace / ".omh" / "extensions"
    extension.mkdir(parents=True)
    (extension / "trip.py").write_text(
        "from oh_my_coding_agent import ExtensionAPI\n"
        "def extension(api: ExtensionAPI) -> None:\n"
        "    raise RuntimeError('trusted')\n",
        encoding="utf-8",
    )
    env = isolated_env(home, pythonpath=scripted_pythonpath(home, [text_sse("ok")]))
    completed = run_omh(
        [
            "--print",
            "--trust-project",
            "--no-session",
            "--cwd",
            os.fspath(workspace),
            "hello",
        ],
        env=env,
        cwd=workspace,
        tty_stdin=True,
    )
    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr.startswith(b"lifecycle hook: ")
    assert b"Trust project" not in completed.stderr
    assert session_files(home) == []


def test_continue_without_existing_session_creates_new(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    env = isolated_env(home, pythonpath=scripted_pythonpath(home, [text_sse("fresh")]))
    completed = run_omh(
        ["--print", "--continue", "--cwd", os.fspath(workspace), "hello"],
        env=env,
        cwd=workspace,
        tty_stdin=True,
    )
    _session_id, kind = identity_record(completed.stderr)
    assert completed.returncode == 0
    assert completed.stdout == b"fresh"
    assert kind == "new"
    assert session_files(home)


def test_one_shot_body_encoding_escapes_controls(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    env = isolated_env(
        home, pythonpath=scripted_pythonpath(home, [text_sse("ok\r\n")])
    )
    completed = run_omh(
        ["--print", "--no-session", "--cwd", os.fspath(workspace), "hello"],
        env=env,
        cwd=workspace,
        tty_stdin=True,
    )
    assert completed.returncode == 0
    assert completed.stdout == b"ok\\u000D\n"


def test_model_error_and_missing_auth_use_stable_status_table(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    env = isolated_env(
        home, pythonpath=scripted_pythonpath(home, [model_error_sse("partial")])
    )
    errored = run_omh(
        ["--print", "--no-session", "--cwd", os.fspath(workspace), "boom"],
        env=env,
        cwd=workspace,
        tty_stdin=True,
    )
    missing_auth = isolated_env(home)
    missing_auth.pop("DEEPSEEK_API_KEY", None)
    unauth = run_omh(
        ["--print", "--no-session", "--cwd", os.fspath(workspace), "nope"],
        env=missing_auth,
        cwd=workspace,
        tty_stdin=True,
    )
    _session_id, kind = identity_record(errored.stderr)
    assert errored.returncode == 1
    assert errored.stdout == b"partial"
    assert errored.stderr.endswith(b"model error\n")
    assert kind == "new"
    assert unauth.returncode == 1
    assert unauth.stdout == b""
    assert unauth.stderr == b"error: DeepSeek authentication is required\n"
    assert b"Traceback" not in unauth.stderr
    assert session_files(home) == []


def test_confirmed_sigint_cancels_one_shot_with_status_130(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    started = home / "started"
    env = isolated_env(home, pythonpath=hang_pythonpath(home, started))
    master = None
    import pty
    import subprocess

    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            [
                os.fspath(omh_bin()),
                "--print",
                "--no-session",
                "--cwd",
                os.fspath(workspace),
                "hang",
            ],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workspace,
            env=env,
        )
        deadline = time.monotonic() + 10
        while not started.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError((process.returncode, stdout, stderr))
            if time.monotonic() > deadline:
                process.kill()
                raise AssertionError("one-shot did not start the Provider request")
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=10)
    finally:
        os.close(master)
        os.close(slave)
    assert process.returncode == 130
    assert b"cancelled\n" in stderr
    assert b"session " in stderr
    assert stdout == b"" or b"run " not in stdout


def test_end_of_options_keeps_dash_prompt(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    env = isolated_env(home, pythonpath=scripted_pythonpath(home, [text_sse("dash")]))
    completed = run_omh(
        ["--print", "--cwd", os.fspath(workspace), "--", "--help"],
        env=env,
        cwd=workspace,
        tty_stdin=True,
    )
    assert completed.returncode == 0
    assert completed.stdout == b"dash"


def test_ticket_23_matrix_connects_command_mode_rows(tmp_path: Path) -> None:
    matrix = json.loads((ROOT / "conformance/obligation-matrix.json").read_text())
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    required = {
        "omh-v0.explicit-command-mode-admission": (
            "reference.explicit-command-mode-admission"
        ),
        "omh-v0.deterministic-one-shot-terminal-carrier": (
            "reference.deterministic-one-shot-terminal-carrier"
        ),
        "omh-v0.redacted-command-diagnostics": "reference.redacted-command-diagnostics",
    }
    obligations = {row["id"]: row for row in matrix["obligations"]}
    cases = {case["id"]: case for case in corpus["cases"]}
    for obligation_id, case_id in required.items():
        row = obligations[obligation_id]
        assert cases[case_id]["obligation"] == obligation_id
        assert row["corpusCase"] == case_id
        assert "ticket-23-one-shot" in row["executableRunners"]

    assert admission_observations(
        tmp_path / "admit-home", tmp_path / "admit-workspace"
    ) == _expected("reference.explicit-command-mode-admission")
    assert one_shot_observations(
        tmp_path / "print-home", tmp_path / "print-workspace"
    ) == _expected("reference.deterministic-one-shot-terminal-carrier")
    assert diagnostics_observations(
        tmp_path / "diag-home", tmp_path / "diag-workspace"
    ) == _expected("reference.redacted-command-diagnostics")
