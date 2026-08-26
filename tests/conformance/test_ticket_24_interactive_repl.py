from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import termios
import time
from typing import Iterable

from ticket_23_scenario import (
    isolated_env,
    model_error_sse,
    omh_bin,
    scripted_pythonpath,
    session_files,
    text_sse,
)


ROOT = Path(__file__).parents[2]


def _text_chunks_sse(chunks: list[str]) -> bytes:
    payloads: list[dict[str, object] | str] = [
        {
            "id": "chatcmpl-chunks",
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": chunk},
                    "finish_reason": None,
                }
            ],
        }
        for chunk in chunks
    ]
    payloads.extend(
        [
            {
                "id": "chatcmpl-chunks",
                "model": "deepseek-v4-flash",
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 4,
                    "total_tokens": 12,
                    "prompt_cache_hit_tokens": 0,
                },
            },
            "[DONE]",
        ]
    )
    return b"".join(
        b"data: [DONE]\n\n"
        if payload == "[DONE]"
        else (
            b"data: "
            + json.dumps(payload, separators=(",", ":")).encode("utf-8")
            + b"\n\n"
        )
        for payload in payloads
    )


def _tool_calls_sse(
    calls: list[tuple[str, str, dict[str, object]]],
) -> bytes:
    def event(payload: dict[str, object] | str) -> bytes:
        if payload == "[DONE]":
            return b"data: [DONE]\n\n"
        return (
            b"data: "
            + json.dumps(payload, separators=(",", ":")).encode("utf-8")
            + b"\n\n"
        )

    payloads: tuple[dict[str, object] | str, ...] = (
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
                                    "id": tool_call_id,
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(
                                            arguments, separators=(",", ":")
                                        ),
                                    },
                                }
                                for index, (tool_call_id, name, arguments) in enumerate(
                                    calls
                                )
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-tool",
                "model": "deepseek-v4-flash",
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
                ],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 4,
                    "total_tokens": 12,
                    "prompt_cache_hit_tokens": 0,
                },
            },
            "[DONE]",
        )
    return b"".join(event(payload) for payload in payloads)


def _read_until(master: int, marker: bytes, *, timeout: float = 10.0) -> bytes:
    output = bytearray()
    deadline = time.monotonic() + timeout
    while marker not in output:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"PTY output did not contain {marker!r}: {bytes(output)!r}")
        readable, _, _ = select.select([master], [], [], remaining)
        if not readable:
            continue
        output.extend(os.read(master, 4096))
    return bytes(output)


def _spawn_interactive(
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


def _blocking_pythonpath(
    home: Path, body: bytes, *, fail_stderr: bool = False
) -> tuple[Path, int, int, int, int]:
    inject = home / "inject-blocking"
    inject.mkdir(parents=True, exist_ok=True)
    started_read, started_write = os.pipe()
    release_read, release_write = os.pipe()
    stderr_failure = (
        "import sys\n"
        "import oh_my_coding_agent._terminal as terminal\n"
        "ORIGINAL_WRITE = terminal._write\n"
        "def fail_stderr_write(buffer: object, payload: bytes) -> None:\n"
        "    if buffer is sys.stderr.buffer:\n"
        "        os.write(STARTED, b'e')\n"
        "        raise OSError('stderr canary')\n"
        "    ORIGINAL_WRITE(buffer, payload)\n"
        "terminal._write = fail_stderr_write\n"
        if fail_stderr
        else ""
    )
    (inject / "sitecustomize.py").write_text(
        "from __future__ import annotations\n"
        "import asyncio\n"
        "import os\n"
        "import httpx\n"
        f"BODY = {body!r}\n"
        "STARTED = int(os.environ['OMH_TEST_STARTED_FD'])\n"
        "RELEASE = int(os.environ['OMH_TEST_RELEASE_FD'])\n"
        "class BarrierTransport(httpx.AsyncBaseTransport):\n"
        "    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:\n"
        "        os.write(STARTED, b'1')\n"
        "        try:\n"
        "            await asyncio.to_thread(os.read, RELEASE, 1)\n"
        "        except asyncio.CancelledError:\n"
        "            os.write(STARTED, b'c')\n"
        "            raise\n"
        "        return httpx.Response(200, content=BODY, headers={'Content-Type': 'text/event-stream'}, request=request)\n"
        "class FakeClient(httpx.AsyncClient):\n"
        "    def __init__(self, **kwargs: object) -> None:\n"
        "        values = dict(kwargs)\n"
        "        values['transport'] = BarrierTransport()\n"
        "        super().__init__(**values)\n"
        "httpx.AsyncClient = FakeClient\n"
        + stderr_failure,
        encoding="utf-8",
    )
    return inject, started_read, started_write, release_read, release_write


def test_interactive_trusts_starts_runs_and_returns_to_idle(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = isolated_env(
        home,
        pythonpath=scripted_pythonpath(home, [_text_chunks_sse(["hel", "lo"])]),
    )
    process, master = _spawn_interactive(
        ["--cwd", os.fspath(workspace)], env=env, cwd=workspace
    )
    try:
        transcript = _read_until(master, b'? [y/n] ')
        os.write(master, b"y\n")
        transcript += _read_until(master, b"> ")
        os.write(master, b"ping\n")
        transcript += _read_until(master, b"run completed\r\n> ")
        os.write(master, b"\x04")
        assert process.wait(timeout=10) == 0
        assert process.stderr is not None
        assert process.stderr.read() == b""
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait()

    session_id = session_files(home)[0].stem.split("_", 1)[1]
    assert transcript == (
        f'Trust project resources in "{workspace}"? [y/n] '
        f"session {session_id} new\r\n"
        "> "
        "assistant start\r\n"
        "hello\r\n"
        "assistant end\r\n"
        "run completed\r\n"
        "> "
    ).encode("utf-8")


def test_nonempty_input_while_run_is_busy_is_discarded(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inject, started_read, started_write, release_read, release_write = (
        _blocking_pythonpath(home, text_sse("done"))
    )
    env = isolated_env(home, pythonpath=inject)
    env["OMH_TEST_STARTED_FD"] = str(started_write)
    env["OMH_TEST_RELEASE_FD"] = str(release_read)
    process, master = _spawn_interactive(
        ["--trust-project", "--no-session", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
        pass_fds=(started_write, release_read),
    )
    os.close(started_write)
    os.close(release_read)
    try:
        _read_until(master, b"> ")
        os.write(master, b"first\n")
        assert select.select([started_read], [], [], 10.0)[0] == [started_read]
        assert os.read(started_read, 1) == b"1"
        os.write(master, b"discard me\n")
        assert process.stderr is not None
        stderr_fd = process.stderr.fileno()
        assert select.select([stderr_fd], [], [], 10.0)[0] == [stderr_fd]
        assert os.read(stderr_fd, 4096) == (
            b"busy: Product Session has an active Run\n"
        )
        os.write(release_write, b"1")
        transcript = _read_until(master, b"run completed\r\n> ")
        os.write(master, b"\x04")
        assert process.wait(timeout=10) == 0
        assert b"done\r\nassistant end\r\nrun completed\r\n> " in transcript
        assert os.read(started_read, 1) == b""
    finally:
        for descriptor in (master, started_read, release_write):
            os.close(descriptor)
        if process.poll() is None:
            process.kill()
            process.wait()


def test_failed_busy_diagnostic_aborts_before_later_progression(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inject, started_read, started_write, release_read, release_write = (
        _blocking_pythonpath(home, text_sse("committed"), fail_stderr=True)
    )
    env = isolated_env(home, pythonpath=inject)
    env["OMH_TEST_STARTED_FD"] = str(started_write)
    env["OMH_TEST_RELEASE_FD"] = str(release_read)
    process, master = _spawn_interactive(
        ["--trust-project", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
        pass_fds=(started_write, release_read),
    )
    os.close(started_write)
    os.close(release_read)
    try:
        _read_until(master, b"> ")
        os.write(master, b"first\n")
        assert select.select([started_read], [], [], 10.0)[0] == [started_read]
        assert os.read(started_read, 1) == b"1"
        os.write(master, b"discard me\n")
        assert select.select([started_read], [], [], 10.0)[0] == [started_read]
        assert os.read(started_read, 1) == b"e"
        assert select.select([started_read], [], [], 10.0)[0] == [started_read]
        assert os.read(started_read, 1) == b"c"
        os.write(release_write, b"1")
        assert process.wait(timeout=10) == 1
        assert process.stderr is not None
        assert process.stderr.read() == b""
        assert os.read(started_read, 1) == b"e"
        assert os.read(started_read, 1) == b""
    finally:
        for descriptor in (master, started_read, release_write):
            os.close(descriptor)
        if process.poll() is None:
            process.kill()
            process.wait()

    persisted = session_files(home)[0].read_text(encoding="utf-8")
    assert '"stopReason":"aborted"' in persisted
    assert '"stopReason":"stop"' not in persisted


def test_failed_pre_run_diagnostic_is_fatal_instead_of_returning_to_idle(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inject = home / "inject-stderr-failure"
    inject.mkdir(parents=True)
    (inject / "sitecustomize.py").write_text(
        "from __future__ import annotations\n"
        "import sys\n"
        "import oh_my_coding_agent._terminal as terminal\n"
        "ORIGINAL_WRITE = terminal._write\n"
        "def fail_stderr_write(buffer: object, payload: bytes) -> None:\n"
        "    if buffer is sys.stderr.buffer:\n"
        "        raise OSError('stderr canary')\n"
        "    ORIGINAL_WRITE(buffer, payload)\n"
        "terminal._write = fail_stderr_write\n",
        encoding="utf-8",
    )
    env = isolated_env(home, pythonpath=inject)
    process, master = _spawn_interactive(
        ["--trust-project", "--no-session", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
    )
    try:
        _read_until(master, b"> ")
        os.write(master, b"/quit\n")
        assert process.wait(timeout=10) == 1
        assert process.stderr is not None
        assert process.stderr.read() == b""
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait()

    assert session_files(home) == []


def test_assistant_suffix_is_flushed_before_run_settlement(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inject = scripted_pythonpath(home, [_text_chunks_sse(["vis", "ible"])])
    started_read, started_write = os.pipe()
    release_read, release_write = os.pipe()
    sitecustomize = inject / "sitecustomize.py"
    sitecustomize.write_text(
        sitecustomize.read_text(encoding="utf-8")
        + "\nimport os\n"
        + "import sys\n"
        + "import oh_my_coding_agent._terminal as terminal\n"
        + "ORIGINAL_WRITE = terminal._write\n"
        + "STARTED = int(os.environ['OMH_TEST_STARTED_FD'])\n"
        + "RELEASE = int(os.environ['OMH_TEST_RELEASE_FD'])\n"
        + "def hold_visible_suffix(buffer: object, payload: bytes) -> None:\n"
        + "    ORIGINAL_WRITE(buffer, payload)\n"
        + "    if buffer is sys.stdout.buffer and payload == b'vis':\n"
        + "        os.write(STARTED, b'1')\n"
        + "        os.read(RELEASE, 1)\n"
        + "terminal._write = hold_visible_suffix\n",
        encoding="utf-8",
    )
    env = isolated_env(home, pythonpath=inject)
    env["OMH_TEST_STARTED_FD"] = str(started_write)
    env["OMH_TEST_RELEASE_FD"] = str(release_read)
    process, master = _spawn_interactive(
        ["--trust-project", "--no-session", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
        pass_fds=(started_write, release_read),
    )
    os.close(started_write)
    os.close(release_read)
    try:
        _read_until(master, b"> ")
        os.write(master, b"prompt\n")
        assert select.select([started_read], [], [], 10.0)[0] == [started_read]
        assert os.read(started_read, 1) == b"1"
        incremental = _read_until(master, b"vis")
        assert incremental == b"assistant start\r\nvis"
        assert b"assistant end" not in incremental
        assert b"run " not in incremental
        os.write(release_write, b"1")
        terminal = _read_until(master, b"run completed\r\n> ")
        assert terminal == b"ible\r\nassistant end\r\nrun completed\r\n> "
        os.write(master, b"\x04")
        assert process.wait(timeout=10) == 0
    finally:
        for descriptor in (master, started_read, release_write):
            os.close(descriptor)
        if process.poll() is None:
            process.kill()
            process.wait()


def test_distinct_assistant_text_blocks_append_without_replaying_prefix(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inject = home / "inject-multiple-text-blocks"
    inject.mkdir(parents=True)
    (inject / "sitecustomize.py").write_text(
        "from __future__ import annotations\n"
        "from dataclasses import replace\n"
        "from oh_my_llm import (\n"
        "    AssistantMessageDoneEvent, AssistantMessageStartEvent,\n"
        "    AssistantMessageTextDeltaEvent, AssistantMessageTextEndEvent,\n"
        "    AssistantMessageTextStartEvent, TextContent,\n"
        ")\n"
        "import oh_my_llm.providers.deepseek as deepseek\n"
        "async def multiple_text_blocks(model, context, options, operation, validator):\n"
        "    del context, options, operation\n"
        "    empty = deepseek._empty_partial(model)\n"
        "    yield deepseek._emit(validator, AssistantMessageStartEvent(partial=empty))\n"
        "    first_open = replace(empty, content=(TextContent(text=''),))\n"
        "    yield deepseek._emit(validator, AssistantMessageTextStartEvent(contentIndex=0, partial=first_open))\n"
        "    first = replace(first_open, content=(TextContent(text='first'),))\n"
        "    yield deepseek._emit(validator, AssistantMessageTextDeltaEvent(contentIndex=0, delta='first', partial=first))\n"
        "    yield deepseek._emit(validator, AssistantMessageTextEndEvent(contentIndex=0, content='first', partial=first))\n"
        "    second_open = replace(first, content=(*first.content, TextContent(text='')))\n"
        "    yield deepseek._emit(validator, AssistantMessageTextStartEvent(contentIndex=1, partial=second_open))\n"
        "    second = replace(second_open, content=(first.content[0], TextContent(text=' second')))\n"
        "    yield deepseek._emit(validator, AssistantMessageTextDeltaEvent(contentIndex=1, delta=' second', partial=second))\n"
        "    yield deepseek._emit(validator, AssistantMessageTextEndEvent(contentIndex=1, content=' second', partial=second))\n"
        "    yield deepseek._emit(validator, AssistantMessageDoneEvent(reason='stop', message=second))\n"
        "deepseek._stream_simple_operation = multiple_text_blocks\n",
        encoding="utf-8",
    )
    env = isolated_env(home, pythonpath=inject)
    process, master = _spawn_interactive(
        ["--trust-project", "--no-session", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
    )
    try:
        _read_until(master, b"> ")
        os.write(master, b"prompt\n")
        transcript = _read_until(master, b"run completed\r\n> ")
        os.write(master, b"\x04")
        assert process.wait(timeout=10) == 0
        assert process.stderr is not None
        assert process.stderr.read() == b""
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait()

    assert transcript == (
        b"assistant start\r\n"
        b"first second\r\n"
        b"assistant end\r\n"
        b"run completed\r\n"
        b"> "
    )


def test_confirmed_cancellation_renders_once_and_session_is_reusable(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inject = home / "inject-cancel"
    inject.mkdir(parents=True)
    started_read, started_write = os.pipe()
    recovered = text_sse("reused")
    (inject / "sitecustomize.py").write_text(
        "from __future__ import annotations\n"
        "import asyncio\n"
        "import os\n"
        "import httpx\n"
        f"RECOVERED = {recovered!r}\n"
        "STARTED = int(os.environ['OMH_TEST_STARTED_FD'])\n"
        "REQUESTS = 0\n"
        "class CancelThenRecover(httpx.AsyncBaseTransport):\n"
        "    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:\n"
        "        global REQUESTS\n"
        "        REQUESTS += 1\n"
        "        if REQUESTS == 1:\n"
        "            os.write(STARTED, b'1')\n"
        "            await asyncio.Event().wait()\n"
        "        return httpx.Response(200, content=RECOVERED, headers={'Content-Type': 'text/event-stream'}, request=request)\n"
        "class FakeClient(httpx.AsyncClient):\n"
        "    def __init__(self, **kwargs: object) -> None:\n"
        "        values = dict(kwargs)\n"
        "        values['transport'] = CancelThenRecover()\n"
        "        super().__init__(**values)\n"
        "httpx.AsyncClient = FakeClient\n",
        encoding="utf-8",
    )
    env = isolated_env(home, pythonpath=inject)
    env["OMH_TEST_STARTED_FD"] = str(started_write)
    process, master = _spawn_interactive(
        ["--trust-project", "--no-session", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
        pass_fds=(started_write,),
    )
    os.close(started_write)
    try:
        _read_until(master, b"> ")
        os.write(master, b"cancel this\n")
        assert select.select([started_read], [], [], 10.0)[0] == [started_read]
        assert os.read(started_read, 1) == b"1"
        process.send_signal(signal.SIGINT)
        cancelled = _read_until(master, b"run cancelled\r\n> ")
        assert cancelled.count(b"run cancelled") == 1
        assert b"run completed" not in cancelled
        os.write(master, b"reuse\n")
        reused = _read_until(master, b"run completed\r\n> ")
        assert b"reused\r\nassistant end\r\nrun completed\r\n> " in reused
        os.write(master, b"\x04")
        assert process.wait(timeout=10) == 0
        assert process.stderr is not None
        assert process.stderr.read() == b""
    finally:
        os.close(master)
        os.close(started_read)
        if process.poll() is None:
            process.kill()
            process.wait()


def test_negative_tool_result_is_rendered_as_an_outcome(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = isolated_env(
        home,
        pythonpath=scripted_pythonpath(
            home,
            [
                _tool_calls_sse(
                    [
                        (
                            'call-"\x1b\u202e\\',
                            "read",
                            {"path": "absent\u202e.txt"},
                        )
                    ]
                ),
                text_sse("fi\x1b\u202e\r\t\n"),
            ],
        ),
    )
    process, master = _spawn_interactive(
        ["--trust-project", "--no-session", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
    )
    try:
        _read_until(master, b"> ")
        os.write(master, b"inspect\n")
        transcript = _read_until(master, b"run completed\r\n> ")
        os.write(master, b"\x04")
        assert process.wait(timeout=10) == 0
        assert process.stderr is not None
        assert process.stderr.read() == b""
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait()

    assert transcript == (
        b"assistant start\r\n"
        b"\r\nassistant end\r\n"
        b'tool start name="read" id="call-\\u0022\\u001B\\u202E\\u005C" arguments={"path":"absent\\u202E.txt"}\r\n'
        b'tool outcome name="read" id="call-\\u0022\\u001B\\u202E\\u005C" result={"content":["Read path \\"absent\\u202E.txt\\" was not found"],"details":{"code":"not_found","path":"absent\\u202E.txt"},"terminate":null}\r\n'
        b"assistant start\r\n"
        b"fi\\u001B\\u202E\\u000D\t\r\n"
        b"assistant end\r\n"
        b"run completed\r\n"
        b"> "
    )


def test_tool_failure_and_model_error_are_distinct_transcript_records(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = isolated_env(
        home,
        pythonpath=scripted_pythonpath(
            home,
            [
                _tool_calls_sse([("call-2", "missing", {})]),
                text_sse("recovered"),
                model_error_sse("partial"),
            ],
        ),
    )
    process, master = _spawn_interactive(
        ["--trust-project", "--no-session", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
    )
    try:
        _read_until(master, b"> ")
        os.write(master, b"tool\n")
        first = _read_until(master, b"run completed\r\n> ")
        os.write(master, b"again\n")
        second = _read_until(master, b"run model_error\r\n> ")
        os.write(master, b"\x04")
        assert process.wait(timeout=10) == 0
        assert process.stderr is not None
        assert process.stderr.read() == b""
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait()

    assert (
        b'tool failure name="missing" id="call-2" result={"content":["Tool \\"missing\\" was not found"],"details":{},"terminate":null}\r\n'
        in first
    )
    assert b"tool_execution_update" not in first
    assert b"turn_" not in first
    assert b"agent_" not in first
    assert second == (
        b"assistant start\r\n"
        b"partial\r\n"
        b"assistant end\r\n"
        b"run model_error\r\n"
        b"> "
    )


def test_parallel_tool_records_keep_public_event_arrival_order(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = isolated_env(
        home,
        pythonpath=scripted_pythonpath(
            home,
            [
                _tool_calls_sse(
                    [
                        ("call-a", "read", {"path": "absent-a.txt"}),
                        ("call-b", "read", {"path": "absent-b.txt"}),
                    ]
                ),
                text_sse("done"),
            ],
        ),
    )
    process, master = _spawn_interactive(
        ["--trust-project", "--no-session", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
    )
    try:
        _read_until(master, b"> ")
        os.write(master, b"inspect both\n")
        transcript = _read_until(master, b"run completed\r\n> ")
        os.write(master, b"\x04")
        assert process.wait(timeout=10) == 0
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait()

    records = [
        line
        for line in transcript.split(b"\r\n")
        if line.startswith(b"tool ")
    ]
    assert [record.split(b" ", 2)[:2] for record in records] == [
        [b"tool", b"start"],
        [b"tool", b"start"],
        [b"tool", b"outcome"],
        [b"tool", b"outcome"],
    ]
    assert [b'id="call-a"' in record for record in records] == [
        True,
        False,
        True,
        False,
    ]


def test_empty_and_launcher_shaped_lines_are_not_intercepted(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = isolated_env(
        home,
        pythonpath=scripted_pythonpath(home, [text_sse("bang")]),
    )
    process, master = _spawn_interactive(
        [
            "--trust-project",
            "--no-session",
            "--session-id",
            "memory-id",
            "--cwd",
            os.fspath(workspace),
        ],
        env=env,
        cwd=workspace,
    )
    try:
        startup = _read_until(master, b"> ")
        os.write(master, b" \t\n")
        empty = _read_until(master, b"> ")
        os.write(master, b" \t/quit\xc2\xa0\n")
        slash = _read_until(master, b"> ")
        assert process.stderr is not None
        stderr_fd = process.stderr.fileno()
        assert select.select([stderr_fd], [], [], 10.0)[0] == [stderr_fd]
        assert os.read(stderr_fd, 4096) == b"error: invalid value\n"
        os.write(master, b"!!\n")
        bang = _read_until(master, b"run completed\r\n> ")
        os.write(master, b"\x04")
        assert process.wait(timeout=10) == 0
        assert process.stderr is not None
        assert process.stderr.read() == b""
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait()

    assert startup == b"session memory-id new\r\n> "
    assert empty == b"> "
    assert slash == b"> "
    assert b"bang\r\nassistant end\r\nrun completed\r\n> " in bang
    assert session_files(home) == []


def test_sigint_at_trust_prompt_exits_before_session_construction(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = isolated_env(home)
    process, master = _spawn_interactive(
        ["--cwd", os.fspath(workspace)], env=env, cwd=workspace
    )
    try:
        transcript = _read_until(master, b"? [y/n] ")
        process.send_signal(signal.SIGINT)
        transcript += _read_until(master, b"trust cancelled\r\n")
        assert process.wait(timeout=10) == 130
        assert process.stderr is not None
        assert process.stderr.read() == b""
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait()

    assert transcript.endswith(b"? [y/n] trust cancelled\r\n")
    assert session_files(home) == []


def test_renderer_write_failure_is_fatal_without_a_run_record(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inject = scripted_pythonpath(home, [text_sse("never rendered")])
    sitecustomize = inject / "sitecustomize.py"
    sitecustomize.write_text(
        sitecustomize.read_text(encoding="utf-8")
        + "\nimport sys\n"
        + "import oh_my_coding_agent._terminal as terminal\n"
        + "ORIGINAL_WRITE = terminal._write\n"
        + "STDOUT_WRITES = 0\n"
        + "def fail_fourth_stdout(buffer: object, payload: bytes) -> None:\n"
        + "    global STDOUT_WRITES\n"
        + "    if buffer is sys.stdout.buffer:\n"
        + "        STDOUT_WRITES += 1\n"
        + "        if STDOUT_WRITES == 4:\n"
        + "            raise OSError('renderer canary')\n"
        + "    ORIGINAL_WRITE(buffer, payload)\n"
        + "terminal._write = fail_fourth_stdout\n",
        encoding="utf-8",
    )
    env = isolated_env(home, pythonpath=inject)
    process, master = _spawn_interactive(
        ["--trust-project", "--cwd", os.fspath(workspace)],
        env=env,
        cwd=workspace,
    )
    try:
        _read_until(master, b"> ")
        os.write(master, b"prompt\n")
        transcript = _read_until(master, b"assistant start\r\n")
        assert process.wait(timeout=10) == 1
        assert process.stderr is not None
        assert process.stderr.read() == (
            b"lifecycle listener: Agent listener failed\n"
        )
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait()

    assert b"run " not in transcript
    assert session_files(home) == []


def test_ticket_24_matrix_connects_interactive_repl_rows() -> None:
    matrix = json.loads((ROOT / "conformance/obligation-matrix.json").read_text())
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    required = {
        "omh-v0.plain-repl-transcript": "reference.plain-repl-transcript",
        "omh-v0.repl-reject-busy-ordinary-message": (
            "reference.repl-reject-busy-ordinary-message"
        ),
    }
    obligations = {row["id"]: row for row in matrix["obligations"]}
    cases = {case["id"]: case for case in corpus["cases"]}
    for obligation_id, case_id in required.items():
        row = obligations[obligation_id]
        assert cases[case_id]["obligation"] == obligation_id
        assert row["corpusCase"] == case_id
        assert row["executableCases"] == ["ticket-24-repl", "ticket-24-installed"]
