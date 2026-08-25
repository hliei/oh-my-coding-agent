from __future__ import annotations

import asyncio
import errno
import json
import os
from pathlib import Path
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
_SAFE_INTEGER = 2**53 - 1
_PATH_PATTERN = "^[^\x00]+$"

_STOP_SSE = (
    b'data: {"id":"chatcmpl-stop","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"Done"},"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-stop","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4,"prompt_cache_hit_tokens":0}}\n\n'
    b"data: [DONE]\n\n"
)

_PROMPT_SUMMARIES = (
    "Read file contents",
    "Execute bash commands (ls, grep, find, etc.)",
    "Make precise file edits with exact text replacement, including multiple disjoint edits in one call",
    "Create or overwrite files",
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


def _expected_path_schema(description: str) -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "pattern": _PATH_PATTERN,
        "description": description,
    }


def _expected_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": (
                    "Read the contents of a UTF-8 text regular file. Output is "
                    "truncated to 2000 lines or 50KB (whichever is hit first). "
                    "Use offset/limit for large files. When you need the full file, "
                    "continue with offset until complete."
                ),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path"],
                    "properties": {
                        "path": _expected_path_schema(
                            "Path to the file to read (relative or absolute)"
                        ),
                        "offset": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": _SAFE_INTEGER,
                            "description": (
                                "Line number to start reading from (1-indexed)"
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": _SAFE_INTEGER,
                            "description": "Maximum number of lines to read",
                        },
                    },
                },
                "strict": False,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": (
                    "Execute a bash command in the current working directory. "
                    "Returns stdout and stderr. Output is truncated to last 2000 "
                    "lines or 50KB (whichever is hit first). If truncated, full "
                    "output is saved to a temp file. Optionally provide a timeout "
                    "in seconds."
                ),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["command"],
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Bash command to execute",
                        },
                        "timeout": {
                            "type": "number",
                            "exclusiveMinimum": 0,
                            "maximum": 2147483.647,
                            "description": (
                                "Timeout in seconds (optional, no default timeout)"
                            ),
                        },
                    },
                },
                "strict": False,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit",
                "description": (
                    "Edit a single file using exact text replacement. Every "
                    "edits[].oldText must match a unique, non-overlapping region of "
                    "the original file. If two changes affect the same block or "
                    "nearby lines, merge them into one edit instead of emitting "
                    "overlapping edits. Do not include large unchanged regions just "
                    "to connect distant changes."
                ),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "edits"],
                    "properties": {
                        "path": _expected_path_schema(
                            "Path to the file to edit (relative or absolute)"
                        ),
                        "edits": {
                            "type": "array",
                            "minItems": 1,
                            "description": (
                                "One or more targeted replacements. Each edit is "
                                "matched against the original file, not incrementally. "
                                "Do not include overlapping or nested edits. If two "
                                "changes touch the same block or nearby lines, merge "
                                "them into one edit instead."
                            ),
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["oldText", "newText"],
                                "properties": {
                                    "oldText": {
                                        "type": "string",
                                        "minLength": 1,
                                        "description": (
                                            "Exact text for one targeted replacement. "
                                            "It must be unique in the original file "
                                            "and must not overlap with any other "
                                            "edits[].oldText in the same call."
                                        ),
                                    },
                                    "newText": {
                                        "type": "string",
                                        "description": (
                                            "Replacement text for this targeted edit."
                                        ),
                                    },
                                },
                            },
                        },
                    },
                },
                "strict": False,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write",
                "description": (
                    "Write content to a file. Creates the file if it doesn't exist, "
                    "overwrites if it does. Automatically creates parent directories."
                ),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "content"],
                    "properties": {
                        "path": _expected_path_schema(
                            "Path to the file to write (relative or absolute)"
                        ),
                        "content": {
                            "type": "string",
                            "description": "Content to write to the file",
                        },
                    },
                },
                "strict": False,
            },
        },
    ]


def test_product_session_registers_fixed_builtins_and_excludes_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    transport = _ScriptedTransport(_STOP_SSE)
    _install_transport(monkeypatch, transport)
    workspace = tmp_path / "project"
    workspace.mkdir()
    manager = SessionManager.create(
        os.fspath(workspace),
        os.fspath(tmp_path / "sessions"),
        NewSessionOptions(id="registry"),
    )
    session = asyncio.run(
        createAgentSession(
            CreateAgentSessionOptions(
                cwd=os.fspath(workspace), sessionManager=manager
            )
        )
    ).session
    assert "- read: Read file contents" in session.systemPrompt
    assert "- bash: Execute bash commands (ls, grep, find, etc.)" in session.systemPrompt
    assert (
        "- edit: Make precise file edits with exact text replacement, including "
        "multiple disjoint edits in one call"
    ) in session.systemPrompt
    assert "- write: Create or overwrite files" in session.systemPrompt
    assert not hasattr(CreateAgentSessionOptions, "tools")
    asyncio.run(session.prompt("inspect tools"))
    asyncio.run(session.dispose())
    assert len(transport.requests) == 1
    body = json.loads(transport.requests[0].content)
    tools = body["tools"]
    assert [tool["function"]["name"] for tool in tools] == [
        "read",
        "bash",
        "edit",
        "write",
    ]
    assert tools == _expected_tools()
    names = {tool["function"]["name"] for tool in tools}
    assert names.isdisjoint({"grep", "find", "ls", "glob", "webfetch"})
    system_messages = [
        message["content"]
        for message in body["messages"]
        if message["role"] == "system"
    ]
    assert system_messages == [session.systemPrompt]
    for summary in _PROMPT_SUMMARIES:
        assert any(
            message.endswith(": " + summary) or f": {summary}" in message
            for message in system_messages
        )


async def _prompt_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    *,
    workspace: Path | None = None,
) -> tuple[Any, list[AgentSessionEvent], _ScriptedTransport]:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    transport = _ScriptedTransport(
        _tool_call_sse("call-read", "read", arguments),
        _STOP_SSE,
    )
    _install_transport(monkeypatch, transport)
    root = tmp_path / "project" if workspace is None else workspace
    root.mkdir(parents=True, exist_ok=True)
    manager = SessionManager.inMemory(
        os.fspath(root), NewSessionOptions(id="read-one")
    )
    session = (
        await createAgentSession(
            CreateAgentSessionOptions(cwd=os.fspath(root), sessionManager=manager)
        )
    ).session
    events: list[AgentSessionEvent] = []
    session.subscribe(events.append)
    await session.prompt("read the file")
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


def test_read_returns_workspace_relative_utf8_text_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "note.txt").write_bytes(b"\xef\xbb\xbfhello\r\nworld\n")
    _, events, _ = asyncio.run(
        _prompt_read(tmp_path, monkeypatch, {"path": "note.txt"}, workspace=workspace)
    )
    end = _tool_end(events)
    result = _tool_result(events)
    assert end.isError is False
    assert result.isError is False
    assert result.toolName == "read"
    assert end.result.details is None
    assert end.result.terminate is None
    assert end.result.content == (TextContent(text="\ufeffhello\r\nworld\n"),)
    assert result.content == end.result.content
    assert not any(
        isinstance(event, AgentSessionEvent.ToolExecutionUpdate) for event in events
    )


def test_read_addresses_literal_paths_without_sandboxing_or_magic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("escaped", encoding="utf-8")
    (workspace / "inner.txt").write_text("inside", encoding="utf-8")
    literal_tilde = workspace / "~"
    literal_tilde.mkdir()
    (literal_tilde / "nope.txt").write_text("literal-tilde", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    (home / "nope.txt").write_text("expanded", encoding="utf-8")
    monkeypatch.setenv("HOME", os.fspath(home))
    target = workspace / "target.txt"
    target.write_text("linked", encoding="utf-8")
    os.symlink(os.fspath(target), os.fspath(workspace / "alias.txt"))

    def outcome(path: str) -> Any:
        _, events, _ = asyncio.run(
            _prompt_read(tmp_path, monkeypatch, {"path": path}, workspace=workspace)
        )
        return _tool_end(events).result.content[0].text

    assert outcome(os.fspath(outside)) == "escaped"
    assert outcome("../outside.txt") == "escaped"
    assert outcome("~/nope.txt") == "literal-tilde"
    assert outcome("alias.txt") == "linked"


def test_read_paginates_logical_lines_and_reports_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "empty.txt").write_bytes(b"")
    (workspace / "final-lf.txt").write_bytes(b"a\n")
    (workspace / "lines.txt").write_text("one\ntwo\nthree\nfour", encoding="utf-8")

    def read(path: str, **fields: int) -> tuple[str, object, bool]:
        _, events, _ = asyncio.run(
            _prompt_read(
                tmp_path, monkeypatch, {"path": path, **fields}, workspace=workspace
            )
        )
        end = _tool_end(events)
        return end.result.content[0].text, end.result.details, end.isError

    empty_text, empty_details, empty_error = read("empty.txt")
    assert (empty_text, empty_details, empty_error) == ("", None, False)
    trailing, _, _ = read("final-lf.txt", offset=2)
    assert trailing == ""
    selected, details, is_error = read("lines.txt", offset=2, limit=2)
    assert is_error is False
    assert details is None
    assert selected == "two\nthree\n\n[1 more lines in file. Use offset=4 to continue.]"
    beyond, beyond_details, beyond_error = read("lines.txt", offset=8)
    assert beyond_error is False
    assert beyond == 'Read offset 8 is beyond end of "lines.txt" (4 lines)'
    assert dict(cast(Any, beyond_details)) == {
        "code": "offset_out_of_range",
        "path": "lines.txt",
        "offset": 8,
        "totalLines": 4,
    }


def test_read_truncates_complete_lines_and_never_spills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    line_file = workspace / "many-lines.txt"
    line_file.write_text("\n".join(f"L{index}" for index in range(1, 2002)), encoding="utf-8")
    byte_file = workspace / "many-bytes.txt"
    row = "a" * 80
    byte_file.write_text("\n".join([row] * 640), encoding="utf-8")
    huge_file = workspace / "huge-line.txt"
    huge_file.write_bytes(b"b" * 52_480)
    before = {path: path.stat().st_mtime_ns for path in workspace.iterdir()}

    def read(path: str) -> Any:
        _, events, _ = asyncio.run(
            _prompt_read(tmp_path, monkeypatch, {"path": path}, workspace=workspace)
        )
        return _tool_end(events).result

    lines_result = read("many-lines.txt")
    assert lines_result.content[0].text.startswith("L1\nL2\n")
    assert "Use offset=2001 to continue.]" in lines_result.content[0].text
    assert "[Showing lines 1-2000 of 2001." in lines_result.content[0].text
    truncation = dict(cast(Any, lines_result.details["truncation"]))
    assert truncation["truncated"] is True
    assert truncation["truncatedBy"] == "lines"
    assert truncation["totalLines"] == 2001
    assert truncation["outputLines"] == 2000
    assert truncation["lastLinePartial"] is False
    assert truncation["firstLineExceedsLimit"] is False
    assert truncation["maxLines"] == 2000
    assert truncation["maxBytes"] == 51_200
    assert "omh-bash-" not in lines_result.content[0].text

    bytes_result = read("many-bytes.txt")
    assert "(50.0KB limit). Use offset=" in bytes_result.content[0].text
    byte_truncation = dict(cast(Any, bytes_result.details["truncation"]))
    assert byte_truncation["truncatedBy"] == "bytes"
    assert byte_truncation["lastLinePartial"] is False
    assert len(byte_truncation["content"].encode("utf-8")) == byte_truncation["outputBytes"]
    assert byte_truncation["outputBytes"] <= 51_200

    huge_result = read("huge-line.txt")
    assert huge_result.content == (
        TextContent(
            text=(
                "[Line 1 is 51.3KB, exceeds 50.0KB limit. Use bash for an "
                "explicit bounded byte-range read.]"
            )
        ),
    )
    huge_truncation = dict(cast(Any, huge_result.details["truncation"]))
    assert huge_truncation["content"] == ""
    assert huge_truncation["firstLineExceedsLimit"] is True
    assert huge_truncation["outputLines"] == 0
    assert huge_truncation["truncatedBy"] == "bytes"
    after = {path: path.stat().st_mtime_ns for path in workspace.iterdir()}
    assert after == before


def test_read_returns_actionable_outcomes_without_sandboxing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "dir").mkdir()
    fifo = workspace / "pipe"
    os.mkfifo(os.fspath(fifo))
    os.symlink(os.fspath(workspace / "missing-target"), os.fspath(workspace / "broken"))
    (workspace / "binary.bin").write_bytes(b"\xff")
    (workspace / "nul.txt").write_bytes(b"ok\x00no")
    secret = workspace / "secret.txt"
    secret.write_text("hidden", encoding="utf-8")
    secret.chmod(0)

    def read(path: str) -> tuple[bool, str, dict[str, Any]]:
        _, events, _ = asyncio.run(
            _prompt_read(tmp_path, monkeypatch, {"path": path}, workspace=workspace)
        )
        end = _tool_end(events)
        details = end.result.details
        return end.isError, end.result.content[0].text, dict(cast(Any, details))

    try:
        missing_error, missing_text, missing_details = read("absent.txt")
        dir_error, dir_text, dir_details = read("dir")
        fifo_error, fifo_text, fifo_details = read("pipe")
        broken_error, broken_text, broken_details = read("broken")
        text_error, text_text, text_details = read("binary.bin")
        nul_error, nul_text, nul_details = read("nul.txt")
        denied_error, denied_text, denied_details = read("secret.txt")
    finally:
        secret.chmod(0o644)

    assert (missing_error, missing_text, missing_details) == (
        False,
        'Read path "absent.txt" was not found',
        {"code": "not_found", "path": "absent.txt"},
    )
    assert (dir_error, dir_text, dir_details) == (
        False,
        'Read path "dir" is not a regular file',
        {"code": "not_regular", "path": "dir"},
    )
    assert (fifo_error, fifo_text, fifo_details) == (
        False,
        'Read path "pipe" is not a regular file',
        {"code": "not_regular", "path": "pipe"},
    )
    assert (broken_error, broken_text, broken_details) == (
        False,
        'Read path "broken" was not found',
        {"code": "not_found", "path": "broken"},
    )
    assert (text_error, text_text, text_details) == (
        False,
        'Read path "binary.bin" is not valid UTF-8 text',
        {"code": "not_text", "path": "binary.bin"},
    )
    assert (nul_error, nul_text, nul_details) == (
        False,
        'Read path "nul.txt" is not valid UTF-8 text',
        {"code": "not_text", "path": "nul.txt"},
    )
    assert (denied_error, denied_text, denied_details) == (
        False,
        'Read path "secret.txt" is not readable',
        {"code": "not_readable", "path": "secret.txt"},
    )


def test_read_schema_rejection_and_unexpected_io_remain_tool_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "ok.txt").write_text("ok", encoding="utf-8")

    def failure(arguments: dict[str, Any]) -> tuple[bool, str]:
        _, events, _ = asyncio.run(
            _prompt_read(tmp_path, monkeypatch, arguments, workspace=workspace)
        )
        end = _tool_end(events)
        return end.isError, end.result.content[0].text

    empty_error, empty_text = failure({"path": ""})
    assert empty_error is True
    assert empty_text.startswith('Validation failed for tool "read":')
    zero_error, zero_text = failure({"path": "ok.txt", "offset": 0})
    assert zero_error is True
    assert zero_text.startswith('Validation failed for tool "read":')

    import oh_my_coding_agent._tools.read as builtin_tools

    def boom(path: str, *args: object, **kwargs: object) -> Any:
        del path, args, kwargs
        raise OSError(errno.EIO, "injected")

    original_stat = builtin_tools._stat_path
    monkeypatch.setattr(builtin_tools, "_stat_path", boom)
    io_error, io_text = failure({"path": "ok.txt"})
    assert io_error is True
    assert io_text == 'Tool "read" execution failed'
    monkeypatch.setattr(builtin_tools, "_stat_path", original_stat)

    os.symlink("loop-b", os.fspath(workspace / "loop-a"))
    os.symlink("loop-a", os.fspath(workspace / "loop-b"))
    loop_error, loop_text = failure({"path": "loop-a"})
    assert loop_error is True
    assert loop_text == 'Tool "read" execution failed'


def test_read_cleanup_failure_is_a_lifecycle_cleanup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oh_my_coding_agent._tools.read as builtin_tools

    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "ok.txt").write_text("ok", encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    transport = _ScriptedTransport(
        _tool_call_sse("call-read", "read", {"path": "ok.txt"}),
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
            await session.prompt("read ok")
        assert caught.value.code == "cleanup"
        await session.dispose()

    asyncio.run(scenario())


def test_read_cancellation_discards_bytes_and_publishes_no_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oh_my_coding_agent._tools.read as builtin_tools

    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "held.txt").write_text("secret-bytes", encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    transport = _ScriptedTransport(
        _tool_call_sse("call-read", "read", {"path": "held.txt"}),
        _STOP_SSE,
    )
    _install_transport(monkeypatch, transport)
    started = asyncio.Event()

    async def hold(signal: Any) -> None:
        started.set()
        await signal.wait()
        raise asyncio.CancelledError

    monkeypatch.setattr(builtin_tools, "_yield_for_cancellation", hold)

    async def scenario() -> None:
        manager = SessionManager.create(
            os.fspath(workspace),
            os.fspath(tmp_path / "sessions"),
            NewSessionOptions(id="cancel-read"),
        )
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace), sessionManager=manager
                )
            )
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)
        prompt = asyncio.create_task(session.prompt("read held"))
        await started.wait()
        await session.abort()
        await prompt
        end = _tool_end(events)
        result = _tool_result(events)
        assert end.isError is True
        assert result.isError is True
        assert end.result.content[0].text == 'Tool "read" execution was cancelled'
        assert "secret-bytes" not in end.result.content[0].text
        await session.dispose()

    asyncio.run(scenario())


def test_ticket_16_matrix_records_registry_and_read_obligations() -> None:
    matrix = json.loads((ROOT / "conformance/obligation-matrix.json").read_text())
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    required = {
        "omh-v0.builtin-tool-registry": "reference.builtin-tool-registry",
        "omh-v0.literal-tool-paths-read": "reference.literal-tool-paths-read",
        "omh-v0.strict-text-file-read": "reference.strict-text-file-read",
        "omh-v0.strict-read-pagination": "reference.strict-read-pagination",
        "omh-v0.actionable-read-outcomes": "reference.actionable-read-outcomes",
    }
    rows = {row["id"]: row for row in matrix["obligations"]}
    cases = {case["id"]: case for case in corpus["cases"]}
    assert required.keys() <= rows.keys()
    assert set(required.values()) <= cases.keys()
    for obligation, corpus_case in required.items():
        assert rows[obligation]["corpusCase"] == corpus_case
        assert {
            "ticket-16-read",
            "ticket-16-installed",
        } <= set(rows[obligation]["executableCases"])
        assert cases[corpus_case]["obligation"] == obligation
