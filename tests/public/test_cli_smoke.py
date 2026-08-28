from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


OMH = Path(sys.executable).with_name("omh")


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env.pop("DEEPSEEK_API_KEY", None)
    return subprocess.run(
        (os.fspath(OMH), *args),
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )


def test_help_and_version_need_no_credentials_or_session_files(tmp_path: Path) -> None:
    help_run = _run("--help", cwd=tmp_path)
    version_run = _run("--version", cwd=tmp_path)

    assert help_run.returncode == 0
    assert help_run.stderr == b""
    assert help_run.stdout.startswith(b"omh - coding agent\n")
    assert b"omh --print" in help_run.stdout
    assert version_run.returncode == 0
    assert version_run.stdout == b"omh 0.1.0\n"
    assert version_run.stderr == b""
    assert list(tmp_path.iterdir()) == []
