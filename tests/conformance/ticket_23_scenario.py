from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import re
import subprocess
import sys
from typing import Any


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

_SESSION_RECORD = re.compile(rb"^session ([^ ]+) (new|recovered)\n\Z")


def _sse(*payloads: dict[str, Any] | str) -> bytes:
    chunks: list[bytes] = []
    for payload in payloads:
        if payload == "[DONE]":
            chunks.append(b"data: [DONE]\n\n")
        else:
            chunks.append(
                b"data: "
                + json.dumps(payload, separators=(",", ":")).encode("utf-8")
                + b"\n\n"
            )
    return b"".join(chunks)


def text_sse(text: str) -> bytes:
    return _sse(
        {
            "id": "chatcmpl-print",
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": text},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-print",
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 4,
                "total_tokens": 12,
                "prompt_cache_hit_tokens": 0,
            },
        },
        "[DONE]",
    )


def model_error_sse(partial: str) -> bytes:
    return (
        _sse(
            {
                "id": "chatcmpl-error",
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": partial},
                        "finish_reason": None,
                    }
                ],
            }
        )
        + b"data: not-json\n\n"
    )


def omh_bin() -> Path:
    return Path(sys.executable).with_name("omh")


def isolated_env(home: Path, *, pythonpath: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = os.fspath(home)
    env["DEEPSEEK_API_KEY"] = "omh-conformance-canary"
    if pythonpath is not None:
        env["PYTHONPATH"] = os.fspath(pythonpath)
    else:
        env.pop("PYTHONPATH", None)
    return env


def scripted_pythonpath(home: Path, bodies: list[bytes]) -> Path:
    inject = home / "inject"
    inject.mkdir(parents=True, exist_ok=True)
    fixture = inject / "responses.json"
    fixture.write_text(json.dumps([body.hex() for body in bodies]), encoding="utf-8")
    counter = inject / "index.txt"
    counter.write_text("0", encoding="utf-8")
    path = os.fspath(fixture)
    marker = os.fspath(counter)
    (inject / "sitecustomize.py").write_text(
        "from __future__ import annotations\n"
        "import json\n"
        "from pathlib import Path\n"
        "import httpx\n"
        f"BODIES = tuple(bytes.fromhex(item) for item in json.loads(Path({path!r}).read_text()))\n"
        f"COUNTER = Path({marker!r})\n"
        "def _handle(request: httpx.Request) -> httpx.Response:\n"
        "    index = int(COUNTER.read_text(encoding='utf-8'))\n"
        "    COUNTER.write_text(str(index + 1), encoding='utf-8')\n"
        "    body = BODIES[index] if index < len(BODIES) else BODIES[-1]\n"
        "    return httpx.Response("
        "200, content=body, headers={'Content-Type': 'text/event-stream'}, request=request)\n"
        "class FakeClient(httpx.AsyncClient):\n"
        "    def __init__(self, **kwargs: object) -> None:\n"
        "        values = dict(kwargs)\n"
        "        values['transport'] = httpx.MockTransport(_handle)\n"
        "        super().__init__(**values)\n"
        "httpx.AsyncClient = FakeClient\n",
        encoding="utf-8",
    )
    return inject


def hang_pythonpath(home: Path, started: Path) -> Path:
    inject = home / "inject-hang"
    inject.mkdir(parents=True, exist_ok=True)
    marker = os.fspath(started)
    (inject / "sitecustomize.py").write_text(
        "from __future__ import annotations\n"
        "import asyncio\n"
        "from pathlib import Path\n"
        "import httpx\n"
        "class HangTransport(httpx.AsyncBaseTransport):\n"
        "    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:\n"
        f"        Path({marker!r}).write_text('started', encoding='utf-8')\n"
        "        await asyncio.Event().wait()\n"
        "        raise RuntimeError('hang transport resumed')\n"
        "class FakeClient(httpx.AsyncClient):\n"
        "    def __init__(self, **kwargs: object) -> None:\n"
        "        values = dict(kwargs)\n"
        "        values['transport'] = HangTransport()\n"
        "        super().__init__(**values)\n"
        "httpx.AsyncClient = FakeClient\n",
        encoding="utf-8",
    )
    return inject


def run_omh(
    args: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    stdin: bytes | None = None,
    tty_stdin: bool = False,
    timeout: float = 30,
) -> subprocess.CompletedProcess[bytes]:
    command = [os.fspath(omh_bin()), *args]
    if tty_stdin:
        master, slave = pty.openpty()
        try:
            return subprocess.run(
                command,
                stdin=slave,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                timeout=timeout,
            )
        finally:
            os.close(master)
            os.close(slave)
    return subprocess.run(
        command,
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        timeout=timeout,
    )


def identity_record(stderr: bytes) -> tuple[str, str]:
    first, _, _rest = stderr.partition(b"\n")
    match = _SESSION_RECORD.fullmatch(first + b"\n")
    assert match is not None, stderr
    return match.group(1).decode("ascii"), match.group(2).decode("ascii")


def session_files(home: Path) -> list[Path]:
    root = home / ".omh" / "agent" / "sessions"
    if not root.exists():
        return []
    return sorted(root.rglob("*.jsonl"))


def admission_observations(home: Path, workspace: Path) -> dict[str, object]:
    home.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    env = isolated_env(home)
    env.pop("DEEPSEEK_API_KEY", None)
    help_run = run_omh(["--help"], env=env, cwd=workspace)
    version_run = run_omh(["--version"], env=env, cwd=workspace)
    unknown = run_omh(["--print", "--unknown"], env=env, cwd=workspace)
    missing = run_omh(["--print"], env=env, cwd=workspace, stdin=b"")
    empty = run_omh(["--print"], env=env, cwd=workspace, stdin=b"   \n")
    both = run_omh(["--print", "hello"], env=env, cwd=workspace, stdin=b"other")
    extra = run_omh(
        ["--print", "one", "two"], env=env, cwd=workspace, tty_stdin=True
    )
    interactive = run_omh([], env=env, cwd=workspace, stdin=b"")
    help_with_print = run_omh(["--help", "--print"], env=env, cwd=workspace)
    return {
        "A": {
            "help": help_run.returncode,
            "version": version_run.returncode,
            "unknown": unknown.returncode,
            "missing": missing.returncode,
            "empty": empty.returncode,
            "both": both.returncode,
            "extra": extra.returncode,
            "interactiveNonTty": interactive.returncode,
            "helpWithPrint": help_with_print.returncode,
        },
        "L": [],
        "T": {
            "helpStdout": help_run.stdout.decode("utf-8"),
            "versionStdout": version_run.stdout.decode("utf-8"),
            "unknownStderr": unknown.stderr.decode("utf-8"),
            "interactiveStderr": interactive.stderr.decode("utf-8"),
        },
        "E": {
            "helpStderrEmpty": help_run.stderr == b"",
            "sessionFiles": len(session_files(home)),
        },
        "C": "no_session_or_resource_effects",
    }


def one_shot_observations(home: Path, workspace: Path) -> dict[str, object]:
    home.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    inject = scripted_pythonpath(
        home,
        [
            text_sse("hello"),
            text_sse("again"),
            model_error_sse("partial"),
            text_sse("ephemeral"),
        ],
    )
    env = isolated_env(home, pythonpath=inject)
    completed = run_omh(
        ["--print", "--cwd", os.fspath(workspace), "ping"],
        env=env,
        cwd=workspace,
        tty_stdin=True,
    )
    session_id, kind = identity_record(completed.stderr)
    continued = run_omh(
        ["--print", "-c", "--cwd", os.fspath(workspace), "next"],
        env=env,
        cwd=workspace,
        tty_stdin=True,
    )
    continued_id, continued_kind = identity_record(continued.stderr)
    errored = run_omh(
        ["--print", "--no-session", "--cwd", os.fspath(workspace), "boom"],
        env=env,
        cwd=workspace,
        tty_stdin=True,
    )
    files_before_ephemeral = session_files(home)
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
        env=env,
        cwd=workspace,
        tty_stdin=True,
    )
    missing_auth = isolated_env(home, pythonpath=inject)
    missing_auth.pop("DEEPSEEK_API_KEY", None)
    unauth = run_omh(
        ["--print", "--no-session", "--cwd", os.fspath(workspace), "nope"],
        env=missing_auth,
        cwd=workspace,
        tty_stdin=True,
    )
    error_lines = [
        line.decode("utf-8") for line in errored.stderr.split(b"\n") if line
    ]
    return {
        "A": {
            "completed": completed.returncode,
            "continued": continued.returncode,
            "modelError": errored.returncode,
            "ephemeral": ephemeral.returncode,
            "unauth": unauth.returncode,
        },
        "L": {
            "firstIdentity": kind,
            "continuedIdentity": continued_kind,
            "sameSession": continued_id == session_id,
            "ephemeralIdentity": identity_record(ephemeral.stderr)[1],
        },
        "T": {
            "stdout": completed.stdout.decode("utf-8"),
            "continuedStdout": continued.stdout.decode("utf-8"),
            "modelErrorStdout": errored.stdout.decode("utf-8"),
            "modelErrorDiagnostic": error_lines[-1],
            "unauthStderr": unauth.stderr.decode("utf-8"),
        },
        "E": {
            "stdoutHasNoRunRecord": b"run " not in completed.stdout,
            "stderrHasNoTool": b"tool " not in completed.stderr,
            "persistentFiles": len(files_before_ephemeral) > 0,
            "ephemeralAddedFiles": session_files(home) == files_before_ephemeral,
            "unauthEffects": unauth.returncode == 1 and session_files(home) == files_before_ephemeral,
        },
        "C": {
            "exitZeroIsCompletionOnly": completed.returncode == 0,
            "ephemeralId": identity_record(ephemeral.stderr)[0],
        },
    }


def diagnostics_observations(home: Path, workspace: Path) -> dict[str, object]:
    home.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    env = isolated_env(
        home, pythonpath=scripted_pythonpath(home, [model_error_sse("partial")])
    )
    unknown = run_omh(["--print", "--secret-flag"], env=env, cwd=workspace)
    missing = isolated_env(home)
    missing.pop("DEEPSEEK_API_KEY", None)
    unauth = run_omh(
        ["--print", "--no-session", "--cwd", os.fspath(workspace), "nope"],
        env=missing,
        cwd=workspace,
        tty_stdin=True,
    )
    errored = run_omh(
        ["--print", "--no-session", "--cwd", os.fspath(workspace), "boom"],
        env=env,
        cwd=workspace,
        tty_stdin=True,
    )
    stderr = unknown.stderr + unauth.stderr + errored.stderr
    return {
        "A": {
            "unknown": unknown.returncode,
            "unauth": unauth.returncode,
            "modelError": errored.returncode,
        },
        "L": "identity_then_primary",
        "T": {
            "unknown": unknown.stderr.decode("utf-8"),
            "unauth": unauth.stderr.decode("utf-8"),
            "modelError": [line.decode("utf-8") for line in errored.stderr.split(b"\n") if line][-1],
        },
        "E": {
            "traceback": b"Traceback" in stderr,
            "exceptionClass": b"ModelsError" in stderr or b"ValueError" in stderr,
            "secret": b"omh-conformance-canary" in stderr,
            "argvEcho": b"secret-flag" in unknown.stderr,
        },
        "C": "stderr_not_redirected_to_stdout",
    }


def main() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        actual = {
            "reference.explicit-command-mode-admission": admission_observations(
                root / "admit-home", root / "admit-workspace"
            ),
            "reference.deterministic-one-shot-terminal-carrier": one_shot_observations(
                root / "print-home", root / "print-workspace"
            ),
            "reference.redacted-command-diagnostics": diagnostics_observations(
                root / "diag-home", root / "diag-workspace"
            ),
        }
        print(json.dumps(actual, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
