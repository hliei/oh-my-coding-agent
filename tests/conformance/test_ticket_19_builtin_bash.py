from __future__ import annotations

import asyncio
import errno
import json
import os
from pathlib import Path
import sys
from typing import Any, cast

import httpx
import pytest

from oh_my_coding_agent import (
    AgentSessionEvent,
    CreateAgentSessionOptions,
    NewSessionOptions,
    SessionManager,
    createAgentSession,
)
from oh_my_llm import LifecycleError, TextContent, ToolResultMessage


ROOT = Path(__file__).parents[2]
PYTHON = json.dumps(sys.executable)

_STOP_SSE = (
    b'data: {"id":"chatcmpl-stop","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"Done"},"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-stop","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4,"prompt_cache_hit_tokens":0}}\n\n'
    b"data: [DONE]\n\n"
)
_HTTPX_ASYNC_CLIENT = httpx.AsyncClient


def _sse(*payloads: dict[str, Any] | str) -> bytes:
    chunks: list[bytes] = []
    for payload in payloads:
        if payload == "[DONE]":
            chunks.append(b"data: [DONE]\n\n")
        else:
            chunks.append(b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n")
    return b"".join(chunks)


def _tool_call_sse(call_id: str, name: str, arguments: dict[str, Any]) -> bytes:
    return _sse(
        {
            "id": "chatcmpl-tool",
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(
                                        arguments, separators=(",", ":")
                                    ),
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-tool",
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 4,
                "total_tokens": 12,
                "prompt_cache_hit_tokens": 0,
            },
        },
        "[DONE]",
    )


class _ScriptedTransport(httpx.AsyncBaseTransport):
    def __init__(self, *bodies: bytes) -> None:
        self.bodies = list(bodies)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            content=self.bodies.pop(0),
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport
) -> None:
    class FakeClient(_HTTPX_ASYNC_CLIENT):
        def __init__(self, **kwargs: Any) -> None:
            kwargs = dict(kwargs)
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)


async def _prompt_bash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    *,
    workspace: Path | None = None,
) -> tuple[Any, list[AgentSessionEvent], _ScriptedTransport]:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    transport = _ScriptedTransport(
        _tool_call_sse("call-bash", "bash", arguments),
        _STOP_SSE,
    )
    _install_transport(monkeypatch, transport)
    root = tmp_path / "project" if workspace is None else workspace
    root.mkdir(parents=True, exist_ok=True)
    session = (
        await createAgentSession(
            CreateAgentSessionOptions(
                cwd=os.fspath(root),
                sessionManager=SessionManager.inMemory(
                    os.fspath(root), NewSessionOptions(id="bash-one")
                ),
            )
        )
    ).session
    events: list[AgentSessionEvent] = []
    session.subscribe(events.append)
    await session.prompt("run bash")
    await session.dispose()
    return session, events, transport


def _tool_end(events: list[AgentSessionEvent]) -> Any:
    return next(
        event
        for event in events
        if isinstance(event, AgentSessionEvent.ToolExecutionEnd)
    )


def _tool_result(events: list[AgentSessionEvent]) -> ToolResultMessage:
    return next(
        message
        for event in events
        if isinstance(event, AgentSessionEvent.MessageEnd)
        and isinstance(message := event.message, ToolResultMessage)
    )


def _updates(events: list[AgentSessionEvent]) -> list[Any]:
    return [
        event
        for event in events
        if isinstance(event, AgentSessionEvent.ToolExecutionUpdate)
    ]


def test_bash_rejects_unknown_fields_and_invalid_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failure(arguments: dict[str, Any]) -> Any:
        _, events, _ = asyncio.run(
            _prompt_bash(tmp_path, monkeypatch, arguments)
        )
        return _tool_end(events)

    cwd = failure({"command": "pwd", "cwd": "/tmp"})
    zero = failure({"command": "pwd", "timeout": 0})
    huge = failure({"command": "pwd", "timeout": 2147483.648})
    assert cwd.isError is True
    assert zero.isError is True
    assert huge.isError is True


def test_bash_runs_in_workspace_with_host_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("OMH_BASH_FIXTURE", "inherited-secret")
    _, events, _ = asyncio.run(
        _prompt_bash(
            tmp_path,
            monkeypatch,
            {
                "command": (
                    f"{PYTHON} -c "
                    + json.dumps(
                        "import os, pathlib, sys; "
                        "pathlib.Path('marker.txt').write_text('ok'); "
                        "print(os.getcwd()); "
                        "print(os.environ['OMH_BASH_FIXTURE']); "
                        "print(os.environ['PATH'])"
                    )
                )
            },
            workspace=workspace,
        )
    )
    end = _tool_end(events)
    result = _tool_result(events)
    text = end.result.content[0].text
    cwd_line, fixture_line, path_line = text.splitlines()
    assert end.isError is False
    assert result.isError is False
    assert result.toolName == "bash"
    assert end.result.details is None
    assert end.result.terminate is None
    assert os.path.realpath(cwd_line) == os.path.realpath(workspace)
    assert fixture_line == "inherited-secret"
    assert path_line == os.environ["PATH"]
    assert (workspace / "marker.txt").read_text() == "ok"
    updates = _updates(events)
    assert updates[0].partialResult.content == ()
    assert updates[0].partialResult.details is None


def test_bash_returns_empty_output_and_merged_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_end = _tool_end(
        asyncio.run(_prompt_bash(tmp_path, monkeypatch, {"command": ""}))[1]
    )
    assert empty_end.isError is False
    assert empty_end.result.content == (TextContent(text="(no output)"),)

    command = (
        f"{PYTHON} -c "
        + json.dumps(
            "import sys; "
            "sys.stdout.buffer.write(b'OUT\\xff'); "
            "sys.stdout.flush(); "
            "sys.stderr.buffer.write(b'ERR'); "
            "sys.stderr.flush()"
        )
    )
    _, events, _ = asyncio.run(_prompt_bash(tmp_path, monkeypatch, {"command": command}))
    end = _tool_end(events)
    text = end.result.content[0].text
    assert end.isError is False
    assert "OUT\ufffd" in text
    assert "ERR" in text
    assert "OUT\ufffdERR" in text or "ERROUT\ufffd" in text


def test_bash_emits_cumulative_throttled_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = (
        f"{PYTHON} -c "
        + json.dumps(
            "import sys, time; "
            "sys.stdout.write('one\\n'); sys.stdout.flush(); "
            "time.sleep(0.22); "
            "sys.stdout.write('two\\n'); sys.stdout.flush(); "
            "time.sleep(0.22); "
            "sys.stdout.write('three\\n'); sys.stdout.flush()"
        )
    )
    _, events, _ = asyncio.run(_prompt_bash(tmp_path, monkeypatch, {"command": command}))
    end = _tool_end(events)
    updates = _updates(events)
    assert updates[0].partialResult.content == ()
    output_updates = updates[1:]
    assert len(output_updates) >= 3
    texts = [event.partialResult.content[0].text for event in output_updates]
    assert texts[0] == "one\n"
    assert "one\n" in texts[-1] and "three" in texts[-1]
    assert all("Command " not in text for text in texts)
    assert all(event.partialResult.details is None for event in output_updates)
    assert all(event.partialResult.terminate is None for event in output_updates)
    stamps = [event.partialResult for event in output_updates]
    del stamps
    later = [
        event
        for previous, event in zip(output_updates, output_updates[1:])
        if event.partialResult.content[0].text != previous.partialResult.content[0].text
    ]
    assert later
    assert end.result.content[0].text == "one\ntwo\nthree\n"


def test_bash_truncates_tail_and_spills_owner_only_raw_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = (
        f"{PYTHON} -c "
        + json.dumps("import sys; sys.stdout.write('x\\n' * 2001)")
    )
    _, line_events, _ = asyncio.run(
        _prompt_bash(tmp_path, monkeypatch, {"command": lines})
    )
    line_end = _tool_end(line_events)
    line_text = line_end.result.content[0].text
    line_details = dict(cast(Any, line_end.result.details))
    line_path = Path(cast(str, line_details["fullOutputPath"]))
    assert line_end.isError is False
    assert line_text.startswith("x\n")
    assert line_text.endswith(
        f"[Showing lines 2-2001 of 2001. Full output: {line_path}]"
    )
    assert line_details["truncation"]["truncated"] is True
    assert line_details["truncation"]["truncatedBy"] == "lines"
    assert line_details["truncation"]["totalLines"] == 2001
    assert line_details["truncation"]["outputLines"] == 2000
    assert line_details["truncation"]["lastLinePartial"] is False
    assert line_path.name.startswith("omh-bash-")
    assert line_path.suffix == ".log"
    assert os.path.realpath(line_path.parent) == os.path.realpath(
        __import__("tempfile").gettempdir()
    )
    assert stat_owner_only(line_path)
    assert line_path.read_bytes() == b"x\n" * 2001

    huge = (
        f"{PYTHON} -c "
        + json.dumps("import sys; sys.stdout.write('a' * 61440)")
    )
    _, byte_events, _ = asyncio.run(
        _prompt_bash(tmp_path, monkeypatch, {"command": huge})
    )
    byte_end = _tool_end(byte_events)
    byte_details = dict(cast(Any, byte_end.result.details))
    byte_path = Path(cast(str, byte_details["fullOutputPath"]))
    notice = (
        f"[Showing last 50.0KB of line 1 (line is 60.0KB). Full output: {byte_path}]"
    )
    assert byte_end.result.content[0].text.endswith(notice)
    assert byte_details["truncation"]["truncatedBy"] == "bytes"
    assert byte_details["truncation"]["lastLinePartial"] is True
    assert byte_details["truncation"]["outputBytes"] == 51200
    assert stat_owner_only(byte_path)
    assert byte_path.read_bytes() == b"a" * 61440


def stat_owner_only(path: Path) -> bool:
    mode = path.stat().st_mode & 0o777
    return mode == 0o600


def test_bash_reports_nonzero_exit_and_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, nonzero_events, _ = asyncio.run(
        _prompt_bash(tmp_path, monkeypatch, {"command": "exit 7"})
    )
    nonzero = _tool_end(nonzero_events)
    assert nonzero.isError is False
    assert nonzero.result.content[0].text == "(no output)\n\nCommand exited with code 7"
    assert dict(cast(Any, nonzero.result.details)) == {
        "code": "nonzero_exit",
        "exitCode": 7,
    }

    _, signal_events, _ = asyncio.run(
        _prompt_bash(tmp_path, monkeypatch, {"command": "kill -s TERM $$"})
    )
    signaled = _tool_end(signal_events)
    assert signaled.isError is False
    assert signaled.result.content[0].text.endswith(
        "Command terminated by signal 15"
    )
    details = dict(cast(Any, signaled.result.details))
    assert details["code"] == "signal_exit"
    assert details["signal"] == 15


def test_bash_timeout_kills_process_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    marker = workspace / "child.pid"
    command = (
        f"{PYTHON} -c "
        + json.dumps(
            "import os, time, pathlib; "
            "pathlib.Path('child.pid').write_text(str(os.getpid())); "
            "time.sleep(30)"
        )
    )
    _, events, _ = asyncio.run(
        _prompt_bash(
            tmp_path,
            monkeypatch,
            {"command": command, "timeout": 0.4},
            workspace=workspace,
        )
    )
    end = _tool_end(events)
    assert end.isError is False
    assert end.result.content[0].text.endswith(
        "Command timed out after 0.4 seconds"
    )
    details = dict(cast(Any, end.result.details))
    assert details["code"] == "timed_out"
    assert details["timeout"] == 0.4
    pid = int(marker.read_text())
    with pytest.raises(OSError):
        os.kill(pid, 0)


def test_bash_pre_spawn_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, nul_events, _ = asyncio.run(
        _prompt_bash(tmp_path, monkeypatch, {"command": "echo \u0000hi"})
    )
    nul = _tool_end(nul_events)
    assert nul.isError is False
    assert nul.result.content[0].text == "Bash command is invalid"
    assert dict(cast(Any, nul.result.details)) == {
        "code": "invalid_command",
        "phase": "command",
        "effect": "none",
    }

    workspace = tmp_path / "gone"
    workspace.mkdir()
    os.chmod(workspace, 0)
    try:
        _, missing_events, _ = asyncio.run(
            _prompt_bash(
                tmp_path,
                monkeypatch,
                {"command": "pwd"},
                workspace=workspace,
            )
        )
    finally:
        os.chmod(workspace, 0o700)
    missing = _tool_end(missing_events)
    assert dict(cast(Any, missing.result.details))["code"] == "workspace_unavailable"
    assert missing.result.content[0].text == (
        f"Bash Workspace {json.dumps(os.fspath(workspace))} is unavailable"
    )

    import oh_my_coding_agent._bash as bash

    monkeypatch.setattr(bash, "resolve_shell", lambda: None)
    _, shell_events, _ = asyncio.run(
        _prompt_bash(tmp_path, monkeypatch, {"command": "pwd"})
    )
    shell = _tool_end(shell_events)
    assert shell.result.content[0].text == "Bash shell is unavailable"
    assert dict(cast(Any, shell.result.details)) == {
        "code": "shell_unavailable",
        "phase": "shell",
        "effect": "none",
    }

    async def denied(*_args: Any, **_kwargs: Any) -> Any:
        error = OSError(errno.EACCES, "denied")
        error.errno = errno.EACCES
        raise bash._OutputFault("spawn", error)

    monkeypatch.undo()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    monkeypatch.setattr(bash, "_spawn_process", denied)
    _, spawn_events, _ = asyncio.run(
        _prompt_bash(tmp_path, monkeypatch, {"command": "pwd"})
    )
    spawn = _tool_end(spawn_events)
    assert spawn.result.content[0].text == "Bash process creation is not permitted"
    assert dict(cast(Any, spawn.result.details))["code"] == "spawn_denied"


def test_bash_output_infrastructure_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oh_my_coding_agent._bash as bash

    def full(_path: str) -> int:
        error = OSError(errno.ENOSPC, "full")
        error.errno = errno.ENOSPC
        raise error

    monkeypatch.setattr(bash, "_open_spill", full)
    command = (
        f"{PYTHON} -c " + json.dumps("import sys; sys.stdout.write('a' * 61440)")
    )
    _, full_events, _ = asyncio.run(
        _prompt_bash(tmp_path, monkeypatch, {"command": command})
    )
    full_end = _tool_end(full_events)
    assert full_end.isError is False
    assert full_end.result.content[0].text.endswith(
        "Command output could not be saved completely because storage is full"
    )
    assert dict(cast(Any, full_end.result.details)) == {
        "code": "output_storage_full",
        "phase": "spill_create",
        "effect": "command_may_have_effects",
        "output": "partial",
    }

    def refused(_path: str) -> int:
        error = OSError(errno.EACCES, "denied")
        error.errno = errno.EACCES
        raise error

    monkeypatch.setattr(bash, "_open_spill", refused)
    _, denied_events, _ = asyncio.run(
        _prompt_bash(tmp_path, monkeypatch, {"command": command})
    )
    denied = _tool_end(denied_events)
    assert denied.result.content[0].text.endswith(
        "Command output could not be collected completely"
    )
    assert dict(cast(Any, denied.result.details))["code"] == "output_unavailable"

    def boom(_path: str) -> int:
        error = OSError(errno.EIO, "injected")
        error.errno = errno.EIO
        raise error

    monkeypatch.setattr(bash, "_open_spill", boom)
    _, fail_events, _ = asyncio.run(
        _prompt_bash(tmp_path, monkeypatch, {"command": command})
    )
    failed = _tool_end(fail_events)
    assert failed.isError is True
    assert failed.result.content[0].text == 'Tool "bash" execution failed'


def test_bash_cancellation_kills_and_does_not_publish_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oh_my_coding_agent._bash as bash

    workspace = tmp_path / "project"
    workspace.mkdir()
    marker = workspace / "child.pid"
    started = asyncio.Event()

    async def hold(signal: Any, pid: int) -> None:
        del pid
        started.set()
        await signal.wait()
        raise asyncio.CancelledError

    monkeypatch.setattr(bash, "_after_spawn", hold)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    command = (
        f"{PYTHON} -c "
        + json.dumps(
            "import os, time, pathlib; "
            "pathlib.Path('child.pid').write_text(str(os.getpid())); "
            "time.sleep(30)"
        )
    )
    transport = _ScriptedTransport(
        _tool_call_sse("call-bash", "bash", {"command": command}),
        _STOP_SSE,
    )
    _install_transport(monkeypatch, transport)

    async def scenario() -> Any:
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    sessionManager=SessionManager.inMemory(
                        os.fspath(workspace), NewSessionOptions(id="cancel-bash")
                    ),
                )
            )
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)
        prompt = asyncio.create_task(session.prompt("run"))
        await started.wait()
        for _ in range(50):
            if marker.exists():
                break
            await asyncio.sleep(0.02)
        await session.abort()
        await prompt
        end = _tool_end(events)
        await session.dispose()
        return end

    end = asyncio.run(scenario())
    assert end.isError is True
    assert end.result.content[0].text == 'Tool "bash" execution was cancelled'
    assert marker.exists()
    with pytest.raises(OSError):
        os.kill(int(marker.read_text()), 0)


def test_bash_close_failure_is_cleanup_lifecycle_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oh_my_coding_agent._bash as bash

    def boom_close(handle: int) -> None:
        del handle
        raise OSError(errno.EIO, "close failed")

    monkeypatch.setattr(bash, "_close_spill", boom_close)
    command = (
        f"{PYTHON} -c " + json.dumps("import sys; sys.stdout.write('a' * 61440)")
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    transport = _ScriptedTransport(
        _tool_call_sse("call-bash", "bash", {"command": command}),
        _STOP_SSE,
    )
    _install_transport(monkeypatch, transport)
    workspace = tmp_path / "project"
    workspace.mkdir()

    async def scenario() -> None:
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    sessionManager=SessionManager.inMemory(
                        os.fspath(workspace), NewSessionOptions(id="cleanup")
                    ),
                )
            )
        ).session
        with pytest.raises(LifecycleError) as caught:
            await session.prompt("bash")
        assert caught.value.code == "cleanup"
        await session.dispose()

    asyncio.run(scenario())


def test_ticket_19_matrix_records_bash_obligations() -> None:
    matrix = json.loads((ROOT / "conformance/obligation-matrix.json").read_text())
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    required = {
        "omh-v0.bash-command-surface": "reference.bash-command-surface",
        "omh-v0.unmodified-host-shell-environment": "reference.unmodified-host-shell-environment",
        "omh-v0.bash-output-updates": "reference.bash-output-updates",
        "omh-v0.private-bash-output-spill": "reference.private-bash-output-spill",
        "omh-v0.actionable-bash-outcomes": "reference.actionable-bash-outcomes",
        "omh-v0.truthful-bash-signal-termination": "reference.truthful-bash-signal-termination",
        "omh-v0.bash-cancellation-cleanup": "reference.bash-cancellation-cleanup",
    }
    rows = {row["id"]: row for row in matrix["obligations"]}
    cases = {case["id"]: case for case in corpus["cases"]}
    assert required.keys() <= rows.keys()
    assert set(required.values()) <= cases.keys()
    for obligation, corpus_case in required.items():
        assert rows[obligation]["corpusCase"] == corpus_case
        assert rows[obligation]["executableCases"] == [
            "ticket-19-bash",
            "ticket-19-installed",
        ]
        assert cases[corpus_case]["obligation"] == obligation
