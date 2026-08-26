from __future__ import annotations

import asyncio
import errno
import json
import os
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

import oh_my_coding_agent._tools.mutation_queue as mutation_queue
from oh_my_coding_agent import (
    AgentSessionEvent,
    CreateAgentSessionOptions,
    NewSessionOptions,
    SessionManager,
    createAgentSession,
)
from oh_my_llm import LifecycleError, TextContent, ToolResultMessage

ROOT = Path(__file__).parents[2]


_STOP_SSE = (
    b'data: {"id":"chatcmpl-stop","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"Done"},"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-stop","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4,"prompt_cache_hit_tokens":0}}\n\n'
    b"data: [DONE]\n\n"
)


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


class _ScriptedTransport(httpx.AsyncBaseTransport):
    def __init__(self, *bodies: bytes) -> None:
        self.bodies = list(bodies)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = self.bodies.pop(0)
        return httpx.Response(
            200,
            content=body,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )


_HTTPX_ASYNC_CLIENT = httpx.AsyncClient


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport
) -> None:
    class FakeClient(_HTTPX_ASYNC_CLIENT):
        def __init__(self, **kwargs: Any) -> None:
            kwargs = dict(kwargs)
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)


async def _prompt_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    *,
    workspace: Path | None = None,
) -> tuple[Any, list[AgentSessionEvent], _ScriptedTransport]:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    transport = _ScriptedTransport(
        _tool_call_sse("call-write", "write", arguments),
        _STOP_SSE,
    )
    _install_transport(monkeypatch, transport)
    root = tmp_path / "project" if workspace is None else workspace
    root.mkdir(parents=True, exist_ok=True)
    manager = SessionManager.inMemory(
        os.fspath(root), NewSessionOptions(id="write-one")
    )
    session = (
        await createAgentSession(
            CreateAgentSessionOptions(cwd=os.fspath(root), sessionManager=manager)
        )
    ).session
    events: list[AgentSessionEvent] = []
    session.subscribe(events.append)
    await session.prompt("write the file")
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


def test_write_creates_exact_utf8_bytes_and_reports_the_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    _, events, _ = asyncio.run(
        _prompt_write(
            tmp_path,
            monkeypatch,
            {"path": "note.txt", "content": "hello"},
            workspace=workspace,
        )
    )
    end = _tool_end(events)
    result = _tool_result(events)
    assert end.isError is False
    assert result.isError is False
    assert result.toolName == "write"
    assert end.result.details is None
    assert end.result.terminate is None
    assert end.result.content == (
        TextContent(text='Successfully wrote 5 bytes to "note.txt"'),
    )
    assert result.content == end.result.content
    assert (workspace / "note.txt").read_bytes() == b"hello"
    assert not any(
        isinstance(event, AgentSessionEvent.ToolExecutionUpdate) for event in events
    )


def test_write_new_file_permissions_follow_the_host_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    previous_umask = os.umask(0o022)
    try:
        _, events, _ = asyncio.run(
            _prompt_write(
                tmp_path,
                monkeypatch,
                {"path": "note.txt", "content": "hello"},
                workspace=workspace,
            )
        )
    finally:
        os.umask(previous_umask)

    assert _tool_end(events).isError is False
    assert (workspace / "note.txt").stat().st_mode & 0o777 == 0o644


def test_write_accepts_empty_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    _, events, _ = asyncio.run(
        _prompt_write(
            tmp_path,
            monkeypatch,
            {"path": "empty.txt", "content": ""},
            workspace=workspace,
        )
    )
    end = _tool_end(events)
    assert end.isError is False
    assert end.result.content == (
        TextContent(text='Successfully wrote 0 bytes to "empty.txt"'),
    )
    assert (workspace / "empty.txt").read_bytes() == b""


def test_write_overwrites_an_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_bytes(b"old-content-that-is-longer")
    _, events, _ = asyncio.run(
        _prompt_write(
            tmp_path,
            monkeypatch,
            {"path": "note.txt", "content": "new"},
            workspace=workspace,
        )
    )
    end = _tool_end(events)
    assert end.isError is False
    assert end.result.content == (
        TextContent(text='Successfully wrote 3 bytes to "note.txt"'),
    )
    assert target.read_bytes() == b"new"


def test_write_creates_missing_parent_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    _, events, _ = asyncio.run(
        _prompt_write(
            tmp_path,
            monkeypatch,
            {"path": "a/b/c.txt", "content": "nested"},
            workspace=workspace,
        )
    )
    end = _tool_end(events)
    assert end.isError is False
    assert end.result.content == (
        TextContent(text='Successfully wrote 6 bytes to "a/b/c.txt"'),
    )
    assert (workspace / "a" / "b" / "c.txt").read_bytes() == b"nested"


def test_write_preserves_unicode_and_quotes_the_original_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    content = "caf\u00e9\ne\u0301\r\n\U0001f600"
    quoted_name = 'say "hi".txt'
    _, events, _ = asyncio.run(
        _prompt_write(
            tmp_path,
            monkeypatch,
            {"path": quoted_name, "content": content},
            workspace=workspace,
        )
    )
    end = _tool_end(events)
    assert end.isError is False
    assert end.result.content == (
        TextContent(text='Successfully wrote 15 bytes to "say \\"hi\\".txt"'),
    )
    written = (workspace / quoted_name).read_bytes()
    assert written == (
        b"caf\xc3\xa9\ne\xcc\x81\r\n\xf0\x9f\x98\x80"
    )
    control_name = "weird\tname.txt"
    _, control_events, _ = asyncio.run(
        _prompt_write(
            tmp_path,
            monkeypatch,
            {"path": control_name, "content": "ok"},
            workspace=workspace,
        )
    )
    control_end = _tool_end(control_events)
    assert control_end.isError is False
    assert control_end.result.content == (
        TextContent(text='Successfully wrote 2 bytes to "weird\\tname.txt"'),
    )
    assert (workspace / control_name).read_bytes() == b"ok"


def test_write_addresses_literal_paths_without_sandboxing_or_magic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", os.fspath(home))
    target = workspace / "target.txt"
    target.write_bytes(b"linked")
    os.symlink(os.fspath(target), os.fspath(workspace / "alias.txt"))

    def write(path: str, content: str) -> None:
        _, events, _ = asyncio.run(
            _prompt_write(
                tmp_path,
                monkeypatch,
                {"path": path, "content": content},
                workspace=workspace,
            )
        )
        assert _tool_end(events).isError is False

    write(os.fspath(outside), "escaped")
    assert outside.read_bytes() == b"escaped"
    write("../outside.txt", "via-dot-dot")
    assert outside.read_bytes() == b"via-dot-dot"
    write("~/nope.txt", "literal-tilde")
    assert (workspace / "~" / "nope.txt").read_bytes() == b"literal-tilde"
    assert not (home / "nope.txt").exists()
    write("alias.txt", "through-link")
    assert target.read_bytes() == b"through-link"
    assert (workspace / "alias.txt").is_symlink()


def test_write_returns_actionable_outcomes_with_truthful_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    os.symlink(
        os.fspath(workspace / "missing-target"), os.fspath(workspace / "broken")
    )
    (workspace / "notdir").write_bytes(b"file")
    (workspace / "dir").mkdir()
    os.symlink("loop-b", os.fspath(workspace / "loop-a"))
    os.symlink("loop-a", os.fspath(workspace / "loop-b"))
    readonly = workspace / "locked.txt"
    readonly.write_bytes(b"keep")
    readonly.chmod(0o444)

    def outcome(path: str, content: str = "x") -> tuple[bool, str, dict[str, Any]]:
        _, events, _ = asyncio.run(
            _prompt_write(
                tmp_path,
                monkeypatch,
                {"path": path, "content": content},
                workspace=workspace,
            )
        )
        end = _tool_end(events)
        return (
            end.isError,
            end.result.content[0].text,
            dict(cast(Any, end.result.details)),
        )

    try:
        missing_error, missing_text, missing_details = outcome("broken")
        parent_error, parent_text, parent_details = outcome("notdir/child.txt")
        dir_error, dir_text, dir_details = outcome("dir")
        loop_error, loop_text, loop_details = outcome("loop-a")
        denied_error, denied_text, denied_details = outcome("locked.txt", "new")
    finally:
        readonly.chmod(0o644)

    assert (missing_error, missing_text, missing_details) == (
        False,
        'Write path "broken" was not found',
        {
            "code": "not_found",
            "path": "broken",
            "phase": "identity",
            "effect": "none",
        },
    )
    assert (parent_error, parent_text, parent_details) == (
        False,
        'Write parent of "notdir/child.txt" is not a directory',
        {
            "code": "parent_not_directory",
            "path": "notdir/child.txt",
            "phase": "identity",
            "effect": "none",
        },
    )
    assert (dir_error, dir_text, dir_details) == (
        False,
        'Write path "dir" is a directory; the target may be partially or completely changed',
        {
            "code": "target_is_directory",
            "path": "dir",
            "phase": "write",
            "effect": "target_may_be_partial",
        },
    )
    assert (loop_error, loop_text, loop_details) == (
        False,
        'Write path "loop-a" is invalid',
        {
            "code": "invalid_path",
            "path": "loop-a",
            "phase": "identity",
            "effect": "none",
        },
    )
    assert (denied_error, denied_text, denied_details) == (
        False,
        'Write path "locked.txt" is not writable; the target may be partially or completely changed',
        {
            "code": "not_writable",
            "path": "locked.txt",
            "phase": "write",
            "effect": "target_may_be_partial",
        },
    )
    assert readonly.read_bytes() == b"keep"

    import oh_my_coding_agent._tools.write as builtin_tools

    def full(_handle: int, _data: bytes) -> int:
        raise OSError(errno.ENOSPC, "injected")

    monkeypatch.setattr(builtin_tools, "_write_file_bytes", full)
    full_error, full_text, full_details = outcome("full.txt", "payload")
    assert (full_error, full_text, full_details) == (
        False,
        'Write path "full.txt" could not be completed because storage is full; the target may be partially or completely changed',
        {
            "code": "storage_full",
            "path": "full.txt",
            "phase": "write",
            "effect": "target_may_be_partial",
        },
    )
    assert (workspace / "full.txt").exists()

    def boom_parents(path: str) -> None:
        del path
        raise OSError(errno.EACCES, "injected parents")

    monkeypatch.setattr(builtin_tools, "_makedirs", boom_parents)
    parent_fail_error, parent_fail_text, parent_fail_details = outcome(
        "fresh/nested.txt"
    )
    assert (parent_fail_error, parent_fail_text, parent_fail_details) == (
        False,
        'Write path "fresh/nested.txt" is not writable; some parent directories may have been created',
        {
            "code": "not_writable",
            "path": "fresh/nested.txt",
            "phase": "parents",
            "effect": "parents_may_exist",
        },
    )


def test_same_target_writes_serialize_while_other_work_stays_concurrent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oh_my_coding_agent._tools.write as builtin_tools

    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "same.txt").write_bytes(b"seed")
    (workspace / "other.txt").write_bytes(b"other-seed")
    same_first = asyncio.Event()
    same_second = asyncio.Event()
    other_inside = asyncio.Event()
    release_same = asyncio.Event()
    same_holders = 0
    original = mutation_queue.after_queue_hold

    async def gated(signal: Any, key: str) -> None:
        nonlocal same_holders
        if os.path.basename(key) == "same.txt":
            same_holders += 1
            if same_holders == 1:
                same_first.set()
                await release_same.wait()
            else:
                same_second.set()
        else:
            other_inside.set()
        await original(signal, key)

    monkeypatch.setattr(mutation_queue, "after_queue_hold", gated)

    async def scenario() -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
        transport = _ScriptedTransport(
            _tool_calls_sse(
                ("call-a", "write", {"path": "same.txt", "content": "first"}),
                ("call-b", "write", {"path": "same.txt", "content": "second"}),
                ("call-c", "write", {"path": "other.txt", "content": "other"}),
                ("call-d", "read", {"path": "other.txt"}),
            ),
            _STOP_SSE,
        )
        _install_transport(monkeypatch, transport)
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    sessionManager=SessionManager.inMemory(
                        os.fspath(workspace), NewSessionOptions(id="serialize")
                    ),
                )
            )
        ).session
        events: list[AgentSessionEvent] = []
        peer_finished = asyncio.Event()

        def observe(event: AgentSessionEvent) -> None:
            events.append(event)
            if (
                isinstance(event, AgentSessionEvent.ToolExecutionEnd)
                and event.toolCallId in {"call-c", "call-d"}
            ):
                peer_finished.set()

        session.subscribe(observe)
        prompt = asyncio.create_task(session.prompt("mutate"))
        await same_first.wait()
        await other_inside.wait()
        assert same_second.is_set() is False
        assert (workspace / "same.txt").read_bytes() == b"seed"
        await peer_finished.wait()
        assert (workspace / "same.txt").read_bytes() == b"seed"
        release_same.set()
        await prompt
        ends = {
            event.toolCallId: event
            for event in events
            if isinstance(event, AgentSessionEvent.ToolExecutionEnd)
        }
        assert ends["call-a"].isError is False
        assert ends["call-b"].isError is False
        assert ends["call-c"].isError is False
        assert ends["call-d"].isError is False
        assert (workspace / "same.txt").read_bytes() == b"second"
        assert (workspace / "other.txt").read_bytes() == b"other"
        await session.dispose()

    async def bounded() -> None:
        await asyncio.wait_for(scenario(), timeout=10.0)

    asyncio.run(bounded())


def test_write_cancellation_before_effect_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oh_my_coding_agent._tools.write as builtin_tools

    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    transport = _ScriptedTransport(
        _tool_call_sse(
            "call-write", "write", {"path": "held.txt", "content": "secret-bytes"}
        ),
        _STOP_SSE,
    )
    _install_transport(monkeypatch, transport)
    started = asyncio.Event()

    async def hold(signal: Any, key: str) -> None:
        del key
        started.set()
        await signal.wait()
        raise asyncio.CancelledError

    monkeypatch.setattr(mutation_queue, "after_queue_hold", hold)

    async def scenario() -> None:
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    sessionManager=SessionManager.inMemory(
                        os.fspath(workspace), NewSessionOptions(id="cancel-write")
                    ),
                )
            )
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)
        prompt = asyncio.create_task(session.prompt("write held"))
        await started.wait()
        await session.abort()
        await prompt
        end = _tool_end(events)
        result = _tool_result(events)
        assert end.isError is True
        assert result.isError is True
        assert end.result.content[0].text == 'Tool "write" execution was cancelled'
        assert "Successfully wrote" not in end.result.content[0].text
        assert not (workspace / "held.txt").exists()
        await session.dispose()

    asyncio.run(scenario())


def test_write_cancellation_drains_started_phases_without_rollback_or_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oh_my_coding_agent._tools.write as builtin_tools

    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    async def run_cancelled(
        arguments: dict[str, Any],
        *,
        hold: Any,
        target: str,
    ) -> Any:
        transport = _ScriptedTransport(
            _tool_call_sse("call-write", "write", arguments),
            _STOP_SSE,
        )
        _install_transport(monkeypatch, transport)
        started = asyncio.Event()

        async def blocked(*args: Any) -> None:
            started.set()
            try:
                await args[-1].wait()
            finally:
                await asyncio.shield(hold(*args))
            raise asyncio.CancelledError

        monkeypatch.setattr(builtin_tools, target, blocked)
        try:
            session = (
                await createAgentSession(
                    CreateAgentSessionOptions(
                        cwd=os.fspath(workspace),
                        sessionManager=SessionManager.inMemory(
                            os.fspath(workspace), NewSessionOptions(id="cancel-phase")
                        ),
                    )
                )
            ).session
            events: list[AgentSessionEvent] = []
            session.subscribe(events.append)
            prompt = asyncio.create_task(session.prompt("write"))
            await started.wait()
            await session.abort()
            await prompt
            end = _tool_end(events)
            await session.dispose()
            return end
        finally:
            monkeypatch.undo()
            monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    async def drain_parents(parent: str, signal: Any) -> None:
        del signal
        builtin_tools._makedirs(parent)

    end = asyncio.run(
        run_cancelled(
            {"path": "newdir/held.txt", "content": "payload"},
            hold=drain_parents,
            target="_create_parents",
        )
    )
    assert end.isError is True
    assert end.result.content[0].text == 'Tool "write" execution was cancelled'
    assert "Successfully wrote" not in end.result.content[0].text
    assert (workspace / "newdir").is_dir()
    assert not (workspace / "newdir" / "held.txt").exists()

    async def drain_write(handle: int, encoded: bytes, signal: Any) -> None:
        del signal
        builtin_tools._write_file_bytes(handle, encoded)

    (workspace / "partial.txt").write_bytes(b"old")
    end = asyncio.run(
        run_cancelled(
            {"path": "partial.txt", "content": "new-bytes"},
            hold=drain_write,
            target="_write_handle",
        )
    )
    assert end.isError is True
    assert "Successfully wrote" not in end.result.content[0].text
    assert (workspace / "partial.txt").read_bytes() == b"new-bytes"


def test_write_schema_rejection_and_unexpected_io_remain_tool_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()

    def failure(arguments: dict[str, Any]) -> tuple[bool, str]:
        _, events, _ = asyncio.run(
            _prompt_write(
                tmp_path, monkeypatch, arguments, workspace=workspace
            )
        )
        end = _tool_end(events)
        return end.isError, end.result.content[0].text

    empty_error, empty_text = failure({"path": "", "content": "x"})
    assert empty_error is True
    assert empty_text.startswith('Validation failed for tool "write":')
    missing_error, missing_text = failure({"path": "ok.txt"})
    assert missing_error is True
    assert missing_text.startswith('Validation failed for tool "write":')

    import oh_my_coding_agent._tools.write as builtin_tools

    def boom(path: str) -> int:
        del path
        raise OSError(errno.EIO, "injected")

    monkeypatch.setattr(builtin_tools, "_open_write", boom)
    io_error, io_text = failure({"path": "ok.txt", "content": "x"})
    assert io_error is True
    assert io_text == 'Tool "write" execution failed'


def test_write_cleanup_failure_is_a_lifecycle_cleanup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oh_my_coding_agent._tools.write as builtin_tools

    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    transport = _ScriptedTransport(
        _tool_call_sse(
            "call-write", "write", {"path": "ok.txt", "content": "ok"}
        ),
        _STOP_SSE,
    )
    _install_transport(monkeypatch, transport)

    def boom(handle: int) -> None:
        del handle
        raise OSError(errno.EIO, "close failed")

    monkeypatch.setattr(builtin_tools, "_close_handle", boom)

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
            await session.prompt("write ok")
        assert caught.value.code == "cleanup"
        await session.dispose()

    asyncio.run(scenario())


def test_ticket_17_matrix_records_write_obligations() -> None:
    matrix = json.loads((ROOT / "conformance/obligation-matrix.json").read_text())
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    required = {
        "omh-v0.truthful-write-result": "reference.truthful-write-result",
        "omh-v0.literal-tool-paths-write": "reference.literal-tool-paths-write",
        "omh-v0.actionable-write-outcomes": "reference.actionable-write-outcomes",
        "omh-v0.write-mutation-serialization": "reference.write-mutation-serialization",
    }
    rows = {row["id"]: row for row in matrix["obligations"]}
    cases = {case["id"]: case for case in corpus["cases"]}
    assert required.keys() <= rows.keys()
    assert set(required.values()) <= cases.keys()
    for obligation, corpus_case in required.items():
        assert rows[obligation]["corpusCase"] == corpus_case
        assert rows[obligation]["executableRunners"] == [
            "ticket-17-write",
            "ticket-17-installed",
        ]
        assert cases[corpus_case]["obligation"] == obligation
