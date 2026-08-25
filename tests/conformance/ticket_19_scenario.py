from __future__ import annotations

import asyncio
import errno
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, cast

import httpx

from oh_my_coding_agent import (
    AgentSessionEvent,
    CreateAgentSessionOptions,
    NewSessionOptions,
    SessionManager,
    createAgentSession,
)


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


def _tool_calls_sse(
    *calls: tuple[str, str, dict[str, Any]],
) -> bytes:
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
                                "index": index,
                                "id": call_id,
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(
                                        arguments, separators=(",", ":")
                                    ),
                                },
                            }
                            for index, (call_id, name, arguments) in enumerate(calls)
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


def _install_transport(transport: httpx.AsyncBaseTransport) -> type[httpx.AsyncClient]:
    original = httpx.AsyncClient

    class FakeClient(_HTTPX_ASYNC_CLIENT):
        def __init__(self, **kwargs: Any) -> None:
            kwargs = dict(kwargs)
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    setattr(httpx, "AsyncClient", FakeClient)
    return original


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


def _tool_end(events: list[AgentSessionEvent]) -> Any:
    return next(
        event
        for event in events
        if isinstance(event, AgentSessionEvent.ToolExecutionEnd)
    )


async def _one_bash(
    root: Path, arguments: dict[str, Any]
) -> tuple[Any, list[AgentSessionEvent], Path]:
    transport = _ScriptedTransport(
        _tool_calls_sse(("call-bash", "bash", arguments)), _STOP_SSE
    )
    original = _install_transport(transport)
    try:
        workspace = root / "project"
        workspace.mkdir(parents=True, exist_ok=True)
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    sessionManager=SessionManager.inMemory(
                        os.fspath(workspace), NewSessionOptions(id="bash")
                    ),
                )
            )
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)
        await session.prompt("bash")
        end = _tool_end(events)
        await session.dispose()
        return end, events, workspace
    finally:
        setattr(httpx, "AsyncClient", original)


async def _surface(root: Path) -> dict[str, object]:
    empty, _, _ = await _one_bash(root / "empty", {"command": ""})
    extra, _, _ = await _one_bash(
        root / "cwd", {"command": "pwd", "cwd": "/tmp"}
    )
    zero, _, _ = await _one_bash(
        root / "zero", {"command": "pwd", "timeout": 0}
    )
    marker, _, workspace = await _one_bash(
        root / "marker",
        {"command": "printf ok > marker.txt"},
    )
    del marker
    return {
        "A": "command plus optional timeout admitted",
        "L": ["tool_execution_update", "tool_execution_end"],
        "T": {
            "empty": empty.result.content[0].text,
            "extraCwdIsError": extra.isError,
            "zeroTimeoutIsError": zero.isError,
        },
        "E": {"wroteMarker": (workspace / "marker.txt").read_text() == "ok"},
        "C": "workspace_unchanged",
    }


async def _environment(root: Path) -> dict[str, object]:
    os.environ["OMH_BASH_FIXTURE"] = "inherited-secret"
    end, _, _ = await _one_bash(
        root,
        {
            "command": (
                f"{PYTHON} -c "
                + json.dumps(
                    "import os; print(os.environ['OMH_BASH_FIXTURE']); "
                    "print(os.environ['PATH'])"
                )
            )
        },
    )
    fixture, path = end.result.content[0].text.splitlines()
    return {
        "A": "command admitted",
        "L": ["tool_execution_end"],
        "T": {
            "fixtureMatched": fixture == "inherited-secret",
            "pathMatched": path == os.environ["PATH"],
        },
        "E": {"productPathInjected": False},
        "C": "fresh_host_snapshot",
    }


async def _updates(root: Path) -> dict[str, object]:
    command = (
        f"{PYTHON} -c "
        + json.dumps(
            "import sys; "
            "sys.stdout.buffer.write(b'AB\\xff'); "
            "sys.stdout.flush()"
        )
    )
    end, events, _ = await _one_bash(root, {"command": command})
    updates = [
        event
        for event in events
        if isinstance(event, AgentSessionEvent.ToolExecutionUpdate)
    ]
    return {
        "A": "command admitted",
        "L": ["tool_execution_update", "tool_execution_end"],
        "T": {
            "emptyFirstUpdate": updates[0].partialResult.content == (),
            "replacementPresent": "\ufffd" in end.result.content[0].text,
            "stdoutOrderPreserved": end.result.content[0].text.startswith("AB"),
        },
        "E": {"realPipes": True},
        "C": "terminal_matches_tail",
    }


async def _spill(root: Path) -> dict[str, object]:
    lines, _, _ = await _one_bash(
        root / "lines",
        {
            "command": (
                f"{PYTHON} -c " + json.dumps("import sys; sys.stdout.write('x\\n' * 2001)")
            )
        },
    )
    huge, _, _ = await _one_bash(
        root / "bytes",
        {
            "command": (
                f"{PYTHON} -c " + json.dumps("import sys; sys.stdout.write('a' * 61440)")
            )
        },
    )
    line_details = dict(cast(Any, lines.result.details))
    byte_details = dict(cast(Any, huge.result.details))
    line_path = Path(cast(str, line_details["fullOutputPath"]))
    byte_path = Path(cast(str, byte_details["fullOutputPath"]))
    return {
        "A": "truncated command admitted",
        "L": ["tool_execution_end"],
        "T": {
            "bytePartial": byte_details["truncation"]["lastLinePartial"],
            "lineTruncatedBy": line_details["truncation"]["truncatedBy"],
        },
        "E": {
            "ownerOnly": (line_path.stat().st_mode & 0o777) == 0o600
            and (byte_path.stat().st_mode & 0o777) == 0o600,
            "rawBytesComplete": line_path.read_bytes() == b"x\n" * 2001
            and byte_path.read_bytes() == b"a" * 61440,
        },
        "C": "spill_retained_after_dispose",
    }


async def _outcomes(root: Path) -> dict[str, object]:
    import oh_my_coding_agent._bash as bash

    nonzero, _, _ = await _one_bash(root / "nz", {"command": "exit 7"})
    timed, _, workspace = await _one_bash(
        root / "to",
        {
            "command": (
                f"{PYTHON} -c "
                + json.dumps(
                    "import os, time, pathlib; "
                    "pathlib.Path('child.pid').write_text(str(os.getpid())); "
                    "time.sleep(30)"
                )
            ),
            "timeout": 0.4,
        },
    )
    invalid, _, _ = await _one_bash(root / "nul", {"command": "echo \u0000hi"})
    tree_dead = False
    pid_file = workspace / "child.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text()), 0)
        except OSError:
            tree_dead = True
    previous = bash.resolve_shell
    setattr(bash, "resolve_shell", lambda: None)
    missing_shell, _, _ = await _one_bash(root / "shell", {"command": "pwd"})
    setattr(bash, "resolve_shell", previous)

    async def denied(*_args: Any, **_kwargs: Any) -> Any:
        error = OSError(errno.EACCES, "denied")
        error.errno = errno.EACCES
        raise bash._OutputFault("spawn", error)

    previous_spawn = bash._spawn_process
    setattr(bash, "_spawn_process", denied)
    spawn, _, _ = await _one_bash(root / "spawn", {"command": "pwd"})
    setattr(bash, "_spawn_process", previous_spawn)

    def full(_path: str) -> int:
        error = OSError(errno.ENOSPC, "full")
        error.errno = errno.ENOSPC
        raise error

    previous_open = bash._open_spill
    setattr(bash, "_open_spill", full)
    storage, _, _ = await _one_bash(
        root / "full",
        {
            "command": (
                f"{PYTHON} -c " + json.dumps("import sys; sys.stdout.write('a' * 61440)")
            )
        },
    )

    def boom(_path: str) -> int:
        error = OSError(errno.EIO, "injected")
        error.errno = errno.EIO
        raise error

    setattr(bash, "_open_spill", boom)
    unexpected, _, _ = await _one_bash(
        root / "eio",
        {
            "command": (
                f"{PYTHON} -c " + json.dumps("import sys; sys.stdout.write('a' * 61440)")
            )
        },
    )
    setattr(bash, "_open_spill", previous_open)
    del missing_shell
    return {
        "A": "expected negatives admitted",
        "L": ["tool_execution_end"],
        "T": {
            "invalidCode": dict(cast(Any, invalid.result.details))["code"],
            "nonzeroCode": dict(cast(Any, nonzero.result.details))["code"],
            "spawnCode": dict(cast(Any, spawn.result.details))["code"],
            "storageCode": dict(cast(Any, storage.result.details))["code"],
            "timeoutCode": dict(cast(Any, timed.result.details))["code"],
            "unexpectedIoIsError": unexpected.isError,
        },
        "E": {"timeoutKilledTree": tree_dead},
        "C": "session_reusable",
    }


async def _signal_row(root: Path) -> dict[str, object]:
    end, _, _ = await _one_bash(root, {"command": "kill -s TERM $$"})
    details = dict(cast(Any, end.result.details))
    return {
        "A": "command admitted",
        "L": ["tool_execution_end"],
        "T": {
            "code": details["code"],
            "isError": end.isError,
            "signal": details["signal"],
        },
        "E": {"signaled": True},
        "C": "reusable",
    }


async def _cancel(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True, exist_ok=True)
    transport = _ScriptedTransport(
        _tool_calls_sse(
            (
                "call-bash",
                "bash",
                {
                    "command": (
                        f"{PYTHON} -c "
                        + json.dumps(
                            "import os, sys, time; "
                            "sys.stdout.write(str(os.getpid()) + '\\n'); "
                            "sys.stdout.flush(); "
                            "time.sleep(30)"
                        )
                    )
                },
            )
        ),
        _STOP_SSE,
    )
    original = _install_transport(transport)

    async def exercise() -> dict[str, object]:
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    sessionManager=SessionManager.inMemory(
                        os.fspath(workspace), NewSessionOptions(id="cancel")
                    ),
                )
            )
        ).session
        events: list[AgentSessionEvent] = []
        ready_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        def observe(event: AgentSessionEvent) -> None:
            events.append(event)
            if not isinstance(event, AgentSessionEvent.ToolExecutionUpdate):
                return
            if not event.partialResult.content or ready_pid.done():
                return
            text = event.partialResult.content[0].text.strip()
            if text.isdigit():
                ready_pid.set_result(int(text))

        session.subscribe(observe)
        prompt = asyncio.create_task(session.prompt("run"))
        try:
            pid = await ready_pid
            await session.abort()
            await prompt
            end = _tool_end(events)
            try:
                os.kill(pid, 0)
                tree_dead = False
            except OSError:
                tree_dead = True
            return {
                "A": "started command",
                "L": ["tool_execution_end"],
                "T": {
                    "cancelledText": end.result.content[0].text,
                    "isError": end.isError,
                },
                "E": {"treeDead": tree_dead},
                "C": "reusable_after_cancel",
            }
        finally:
            if not prompt.done():
                prompt.cancel()
            await asyncio.gather(prompt, return_exceptions=True)
            await session.dispose()

    try:
        return await asyncio.wait_for(exercise(), timeout=10.0)
    finally:
        setattr(httpx, "AsyncClient", original)


async def main() -> None:
    os.environ["DEEPSEEK_API_KEY"] = "omh-conformance-canary"
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        actual = {
            "reference.bash-command-surface": await _surface(root / "surface"),
            "reference.unmodified-host-shell-environment": await _environment(
                root / "env"
            ),
            "reference.bash-output-updates": await _updates(root / "updates"),
            "reference.private-bash-output-spill": await _spill(root / "spill"),
            "reference.actionable-bash-outcomes": await _outcomes(root / "outcomes"),
            "reference.truthful-bash-signal-termination": await _signal_row(
                root / "signal"
            ),
            "reference.bash-cancellation-cleanup": await _cancel(root / "cancel"),
        }
        print(json.dumps(actual, sort_keys=True, separators=(",", ":")))


asyncio.run(main())
