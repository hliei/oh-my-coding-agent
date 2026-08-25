from __future__ import annotations

import asyncio
import errno
import os
import shutil
import signal
import stat
import sys


class _ShellConfig:
    __slots__ = ("shell", "args", "command_from_stdin")

    def __init__(
        self, shell: str, args: tuple[str, ...], *, command_from_stdin: bool = False
    ) -> None:
        self.shell = shell
        self.args = args
        self.command_from_stdin = command_from_stdin


def _is_legacy_wsl_bash(path: str) -> bool:
    normalized = path.replace("/", "\\").lower()
    return (
        len(normalized) >= 22
        and normalized[0].isalpha()
        and normalized.startswith(
            (
                normalized[0] + ":\\windows\\system32\\bash.exe",
                normalized[0] + ":\\windows\\sysnative\\bash.exe",
            )
        )
    )


def _bash_config(shell: str) -> _ShellConfig:
    if _is_legacy_wsl_bash(shell):
        return _ShellConfig(shell, ("-s",), command_from_stdin=True)
    return _ShellConfig(shell, ("-c",))


def _usable_shell(path: str) -> bool:
    try:
        status = os.stat(path)
    except OSError as error:
        if error.errno not in (errno.ENOENT, errno.ENOTDIR, errno.EACCES, errno.EPERM):
            raise
        return False
    return stat.S_ISREG(status.st_mode) and os.access(path, os.X_OK)


def _first_on_path(name: str) -> str | None:
    found = shutil.which(name)
    if found is None or not _usable_shell(found):
        return None
    return found


def resolve_shell() -> _ShellConfig | None:
    if sys.platform == "win32":
        roots: list[str] = []
        program_files = os.environ.get("ProgramFiles")
        if program_files:
            roots.append(os.path.join(program_files, "Git", "bin", "bash.exe"))
        program_files_x86 = os.environ.get("ProgramFiles(x86)")
        if program_files_x86:
            roots.append(os.path.join(program_files_x86, "Git", "bin", "bash.exe"))
        for path in roots:
            if _usable_shell(path):
                return _bash_config(path)
        found = _first_on_path("bash.exe")
        if found is not None:
            return _bash_config(found)
        return None
    if _usable_shell("/bin/bash"):
        return _bash_config("/bin/bash")
    found = _first_on_path("bash")
    if found is not None:
        return _bash_config(found)
    found_sh = _first_on_path("sh")
    if found_sh is not None:
        return _ShellConfig(found_sh, ("-c",))
    return None


def _host_environment() -> dict[str, str]:
    return dict(os.environ)


async def _kill_process_tree(pid: int) -> None:
    if sys.platform == "win32":
        helper = await asyncio.create_subprocess_exec(
            "taskkill",
            "/F",
            "/T",
            "/PID",
            str(pid),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await helper.wait()
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
