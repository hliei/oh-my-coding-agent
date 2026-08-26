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

_SIMPLE_OLD = "alpha\nbravo\ncharlie\n"
_SIMPLE_NEW = "alpha\nBRAND\ncharlie\n"
_SIMPLE_DIFF = " 1 alpha\n-2 bravo\n+2 BRAND\n 3 charlie"
_SIMPLE_PATCH = (
    "--- note.txt\n+++ note.txt\n@@ -1,3 +1,3 @@\n alpha\n-bravo\n+BRAND\n charlie\n"
)
_ELIDE_OLD = (
    "START\n"
    + "\n".join(f"line{index}" for index in range(12))
    + "\nEND\n"
)
_ELIDE_NEW = (
    "BEGIN\n"
    + "\n".join(f"line{index}" for index in range(12))
    + "\nFINISH\n"
)
_ELIDE_DIFF = (
    "- 1 START\n+ 1 BEGIN\n  2 line0\n  3 line1\n  4 line2\n  5 line3\n"
    "    ...\n 10 line8\n 11 line9\n 12 line10\n 13 line11\n-14 END\n+14 FINISH"
)
_ELIDE_PATCH = (
    "--- big.txt\n+++ big.txt\n@@ -1,5 +1,5 @@\n-START\n+BEGIN\n line0\n"
    " line1\n line2\n line3\n@@ -10,5 +10,5 @@\n line8\n line9\n line10\n"
    " line11\n-END\n+FINISH\n"
)
_NONL_DIFF = "-1 hello world\n+1 hello omh"
_NONL_PATCH = (
    "--- a.txt\n+++ a.txt\n@@ -1,1 +1,1 @@\n-hello world\n"
    "\\ No newline at end of file\n+hello omh\n\\ No newline at end of file\n"
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


async def _prompt_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    *,
    workspace: Path | None = None,
) -> tuple[Any, list[AgentSessionEvent], _ScriptedTransport]:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    transport = _ScriptedTransport(
        _tool_call_sse("call-edit", "edit", arguments),
        _STOP_SSE,
    )
    _install_transport(monkeypatch, transport)
    root = tmp_path / "project" if workspace is None else workspace
    root.mkdir(parents=True, exist_ok=True)
    manager = SessionManager.inMemory(
        os.fspath(root), NewSessionOptions(id="edit-one")
    )
    session = (
        await createAgentSession(
            CreateAgentSessionOptions(cwd=os.fspath(root), sessionManager=manager)
        )
    ).session
    events: list[AgentSessionEvent] = []
    session.subscribe(events.append)
    await session.prompt("edit the file")
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


def test_edit_replaces_a_unique_literal_range_and_precomputes_reference_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_bytes(_SIMPLE_OLD.encode("utf-8"))
    _, events, _ = asyncio.run(
        _prompt_edit(
            tmp_path,
            monkeypatch,
            {
                "path": "note.txt",
                "edits": [{"oldText": "bravo", "newText": "BRAND"}],
            },
            workspace=workspace,
        )
    )
    end = _tool_end(events)
    result = _tool_result(events)
    assert end.isError is False
    assert result.isError is False
    assert result.toolName == "edit"
    assert end.result.terminate is None
    assert end.result.content == (
        TextContent(text="Successfully replaced 1 block(s) in note.txt."),
    )
    assert result.content == end.result.content
    details = dict(end.result.details)
    assert details["diff"] == _SIMPLE_DIFF
    assert details["patch"] == _SIMPLE_PATCH
    assert details["firstChangedLine"] == 2
    assert set(details) == {"diff", "patch", "firstChangedLine"}
    assert target.read_bytes() == _SIMPLE_NEW.encode("utf-8")
    assert not any(
        isinstance(event, AgentSessionEvent.ToolExecutionUpdate) for event in events
    )


def test_edit_applies_disjoint_replacements_from_one_snapshot_in_any_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_bytes(b"aaa bbb ccc\n")
    _, events, _ = asyncio.run(
        _prompt_edit(
            tmp_path,
            monkeypatch,
            {
                "path": "note.txt",
                "edits": [
                    {"oldText": "ccc", "newText": "CCC"},
                    {"oldText": "aaa", "newText": ""},
                    {"oldText": "bbb", "newText": "BBB"},
                ],
            },
            workspace=workspace,
        )
    )
    end = _tool_end(events)
    assert end.isError is False
    assert end.result.content == (
        TextContent(text="Successfully replaced 3 block(s) in note.txt."),
    )
    assert target.read_bytes() == b" BBB CCC\n"
    adjacent = workspace / "touch.txt"
    adjacent.write_bytes(b"abcd")
    _, adjacent_events, _ = asyncio.run(
        _prompt_edit(
            tmp_path,
            monkeypatch,
            {
                "path": "touch.txt",
                "edits": [
                    {"oldText": "cd", "newText": "CD"},
                    {"oldText": "ab", "newText": "AB"},
                ],
            },
            workspace=workspace,
        )
    )
    assert _tool_end(adjacent_events).isError is False
    assert adjacent.read_bytes() == b"ABCD"


def test_edit_restores_bom_and_keeps_literal_newlines_and_scalars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    bom_target = workspace / "bom.txt"
    bom_target.write_bytes("\ufeffkeep\nchange\n".encode("utf-8"))
    _, events, _ = asyncio.run(
        _prompt_edit(
            tmp_path,
            monkeypatch,
            {
                "path": "bom.txt",
                "edits": [{"oldText": "change", "newText": "changed"}],
            },
            workspace=workspace,
        )
    )
    assert _tool_end(events).isError is False
    assert bom_target.read_bytes() == "\ufeffkeep\nchanged\n".encode("utf-8")

    crlf = workspace / "crlf.txt"
    crlf.write_bytes(b"a\r\nb\r\n")
    _, crlf_events, _ = asyncio.run(
        _prompt_edit(
            tmp_path,
            monkeypatch,
            {
                "path": "crlf.txt",
                "edits": [{"oldText": "a\nb", "newText": "A\nB"}],
            },
            workspace=workspace,
        )
    )
    crlf_end = _tool_end(crlf_events)
    assert crlf_end.isError is False
    assert dict(cast(Any, crlf_end.result.details))["code"] == "text_not_found"
    assert crlf.read_bytes() == b"a\r\nb\r\n"
    _, crlf_ok, _ = asyncio.run(
        _prompt_edit(
            tmp_path,
            monkeypatch,
            {
                "path": "crlf.txt",
                "edits": [{"oldText": "a\r\nb", "newText": "A\r\nB"}],
            },
            workspace=workspace,
        )
    )
    assert _tool_end(crlf_ok).isError is False
    assert crlf.read_bytes() == b"A\r\nB\r\n"

    cafe = workspace / "cafe.txt"
    cafe.write_bytes("caf\u00e9\n".encode("utf-8"))
    _, cafe_events, _ = asyncio.run(
        _prompt_edit(
            tmp_path,
            monkeypatch,
            {
                "path": "cafe.txt",
                "edits": [{"oldText": "cafe\u0301", "newText": "coffee"}],
            },
            workspace=workspace,
        )
    )
    cafe_end = _tool_end(cafe_events)
    assert cafe_end.isError is False
    assert dict(cast(Any, cafe_end.result.details))["code"] == "text_not_found"
    assert cafe.read_bytes() == "caf\u00e9\n".encode("utf-8")

    curly = workspace / "quotes.txt"
    curly.write_bytes("\u201chello\u201d\n".encode("utf-8"))
    _, quote_events, _ = asyncio.run(
        _prompt_edit(
            tmp_path,
            monkeypatch,
            {
                "path": "quotes.txt",
                "edits": [{"oldText": '"hello"', "newText": "x"}],
            },
            workspace=workspace,
        )
    )
    quote_end = _tool_end(quote_events)
    assert quote_end.isError is False
    assert dict(cast(Any, quote_end.result.details))["code"] == "text_not_found"
    assert curly.read_bytes() == "\u201chello\u201d\n".encode("utf-8")

    cr_only = workspace / "cr.txt"
    cr_only.write_bytes(b"a\rb\r")
    _, cr_events, _ = asyncio.run(
        _prompt_edit(
            tmp_path,
            monkeypatch,
            {
                "path": "cr.txt",
                "edits": [{"oldText": "a\nb", "newText": "A\nB"}],
            },
            workspace=workspace,
        )
    )
    assert dict(cast(Any, _tool_end(cr_events).result.details))["code"] == "text_not_found"
    assert cr_only.read_bytes() == b"a\rb\r"

    dashed = workspace / "dash.txt"
    dashed.write_bytes("em\u2014dash\n".encode("utf-8"))
    _, dash_events, _ = asyncio.run(
        _prompt_edit(
            tmp_path,
            monkeypatch,
            {
                "path": "dash.txt",
                "edits": [{"oldText": "em-dash", "newText": "x"}],
            },
            workspace=workspace,
        )
    )
    assert dict(cast(Any, _tool_end(dash_events).result.details))["code"] == "text_not_found"
    assert dashed.read_bytes() == "em\u2014dash\n".encode("utf-8")

    spaced = workspace / "space.txt"
    spaced.write_bytes(b"keep  \n")
    _, space_events, _ = asyncio.run(
        _prompt_edit(
            tmp_path,
            monkeypatch,
            {
                "path": "space.txt",
                "edits": [{"oldText": "keep", "newText": "kept"}],
            },
            workspace=workspace,
        )
    )
    space_end = _tool_end(space_events)
    assert space_end.isError is False
    assert (workspace / "space.txt").read_bytes() == b"kept  \n"


def test_edit_rejects_legacy_and_empty_forms_before_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_bytes(_SIMPLE_OLD.encode("utf-8"))

    def failure(arguments: dict[str, Any]) -> tuple[bool, str]:
        _, events, _ = asyncio.run(
            _prompt_edit(
                tmp_path, monkeypatch, arguments, workspace=workspace
            )
        )
        end = _tool_end(events)
        return end.isError, end.result.content[0].text

    empty_error, empty_text = failure({"path": "note.txt", "edits": []})
    assert empty_error is True
    assert empty_text.startswith('Validation failed for tool "edit":')
    missing_error, missing_text = failure(
        {"path": "note.txt", "oldText": "bravo", "newText": "BRAND"}
    )
    assert missing_error is True
    assert missing_text.startswith('Validation failed for tool "edit":')
    string_error, string_text = failure(
        {"path": "note.txt", "edits": '[{"oldText":"bravo","newText":"BRAND"}]'}
    )
    assert string_error is True
    empty_old_error, empty_old_text = failure(
        {"path": "note.txt", "edits": [{"oldText": "", "newText": "x"}]}
    )
    assert empty_old_error is True
    unknown_error, unknown_text = failure(
        {
            "path": "note.txt",
            "edits": [{"oldText": "bravo", "newText": "BRAND", "extra": "nope"}],
        }
    )
    assert unknown_error is True
    alias_error, alias_text = failure(
        {"file_path": "note.txt", "edits": [{"oldText": "bravo", "newText": "BRAND"}]}
    )
    assert alias_error is True
    assert target.read_bytes() == _SIMPLE_OLD.encode("utf-8")


def test_edit_match_outcomes_write_nothing_and_report_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()

    def outcome(
        name: str, original: bytes, edits: list[dict[str, str]]
    ) -> tuple[bool, str, dict[str, Any], bytes]:
        target = workspace / name
        target.write_bytes(original)
        _, events, _ = asyncio.run(
            _prompt_edit(
                tmp_path,
                monkeypatch,
                {"path": name, "edits": edits},
                workspace=workspace,
            )
        )
        end = _tool_end(events)
        return (
            end.isError,
            end.result.content[0].text,
            dict(cast(Any, end.result.details)),
            target.read_bytes(),
        )

    missing_error, missing_text, missing_details, missing_bytes = outcome(
        "missing.txt",
        b"abc\n",
        [{"oldText": "z", "newText": "y"}],
    )
    assert (missing_error, missing_text, missing_details, missing_bytes) == (
        False,
        'Edit text at edits[0] was not found in "missing.txt"',
        {
            "code": "text_not_found",
            "path": "missing.txt",
            "phase": "match",
            "effect": "none",
            "editIndex": 0,
        },
        b"abc\n",
    )
    unique_error, unique_text, unique_details, unique_bytes = outcome(
        "dup.txt",
        b"aaa\n",
        [{"oldText": "aa", "newText": "b"}],
    )
    assert (unique_error, unique_text, unique_details, unique_bytes) == (
        False,
        'Edit text at edits[0] matched 2 locations in "dup.txt"',
        {
            "code": "text_not_unique",
            "path": "dup.txt",
            "phase": "match",
            "effect": "none",
            "editIndex": 0,
            "occurrences": 2,
        },
        b"aaa\n",
    )
    overlap_error, overlap_text, overlap_details, overlap_bytes = outcome(
        "overlap.txt",
        b"abcd",
        [
            {"oldText": "abc", "newText": "x"},
            {"oldText": "cd", "newText": "y"},
        ],
    )
    assert (overlap_error, overlap_text, overlap_details, overlap_bytes) == (
        False,
        'Edit ranges at edits[0] and edits[1] overlap in "overlap.txt"',
        {
            "code": "overlapping_edits",
            "path": "overlap.txt",
            "phase": "match",
            "effect": "none",
            "firstEditIndex": 0,
            "secondEditIndex": 1,
        },
        b"abcd",
    )
    nested_error, nested_text, nested_details, nested_bytes = outcome(
        "nested.txt",
        b"abcd",
        [
            {"oldText": "bc", "newText": "X"},
            {"oldText": "abcd", "newText": "Y"},
        ],
    )
    assert nested_details["code"] == "overlapping_edits"
    assert nested_bytes == b"abcd"
    noop_error, noop_text, noop_details, noop_bytes = outcome(
        "noop.txt",
        b"abc\n",
        [{"oldText": "a", "newText": "a"}],
    )
    assert (noop_error, noop_text, noop_details, noop_bytes) == (
        False,
        'Edit path "noop.txt" would not change',
        {
            "code": "no_change",
            "path": "noop.txt",
            "phase": "match",
            "effect": "none",
        },
        b"abc\n",
    )


def test_edit_returns_actionable_file_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    os.symlink(
        os.fspath(workspace / "missing-target"), os.fspath(workspace / "broken")
    )
    (workspace / "dir").mkdir()
    fifo = workspace / "fifo"
    os.mkfifo(fifo)
    os.symlink("loop-b", os.fspath(workspace / "loop-a"))
    os.symlink("loop-a", os.fspath(workspace / "loop-b"))
    readonly = workspace / "locked.txt"
    readonly.write_bytes(b"keep")
    readonly.chmod(0o444)
    secret = workspace / "secret.txt"
    secret.write_bytes(b"keep")
    secret.chmod(0)
    binary = workspace / "bin.txt"
    binary.write_bytes(b"\xff\xfe")
    nul = workspace / "nul.txt"
    nul.write_bytes(b"a\x00b")

    def outcome(path: str) -> tuple[bool, str, dict[str, Any]]:
        _, events, _ = asyncio.run(
            _prompt_edit(
                tmp_path,
                monkeypatch,
                {
                    "path": path,
                    "edits": [{"oldText": "keep", "newText": "new"}],
                },
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
        dir_error, dir_text, dir_details = outcome("dir")
        fifo_error, fifo_text, fifo_details = outcome("fifo")
        loop_error, loop_text, loop_details = outcome("loop-a")
        denied_error, denied_text, denied_details = outcome("locked.txt")
        unread_error, unread_text, unread_details = outcome("secret.txt")
        text_error, text_text, text_details = outcome("bin.txt")
        nul_error, nul_text, nul_details = outcome("nul.txt")
        absent_error, absent_text, absent_details = outcome("gone.txt")
    finally:
        readonly.chmod(0o644)
        secret.chmod(0o644)

    assert (missing_error, missing_text, missing_details) == (
        False,
        'Edit path "broken" was not found',
        {
            "code": "not_found",
            "path": "broken",
            "phase": "identity",
            "effect": "none",
        },
    )
    assert (absent_error, absent_text, absent_details) == (
        False,
        'Edit path "gone.txt" was not found',
        {
            "code": "not_found",
            "path": "gone.txt",
            "phase": "identity",
            "effect": "none",
        },
    )
    assert (dir_error, dir_text, dir_details) == (
        False,
        'Edit path "dir" is not a regular file',
        {
            "code": "not_regular",
            "path": "dir",
            "phase": "identity",
            "effect": "none",
        },
    )
    assert (fifo_error, fifo_text, fifo_details) == (
        False,
        'Edit path "fifo" is not a regular file',
        {
            "code": "not_regular",
            "path": "fifo",
            "phase": "identity",
            "effect": "none",
        },
    )
    assert (loop_error, loop_text, loop_details) == (
        False,
        'Edit path "loop-a" is invalid',
        {
            "code": "invalid_path",
            "path": "loop-a",
            "phase": "identity",
            "effect": "none",
        },
    )
    assert (denied_error, denied_text, denied_details) == (
        False,
        'Edit path "locked.txt" is not writable',
        {
            "code": "not_writable",
            "path": "locked.txt",
            "phase": "identity",
            "effect": "none",
        },
    )
    assert readonly.read_bytes() == b"keep"
    assert (unread_error, unread_text, unread_details) == (
        False,
        'Edit path "secret.txt" is not readable',
        {
            "code": "not_readable",
            "path": "secret.txt",
            "phase": "identity",
            "effect": "none",
        },
    )
    assert (text_error, text_text, text_details) == (
        False,
        'Edit path "bin.txt" is not valid UTF-8 text',
        {
            "code": "not_text",
            "path": "bin.txt",
            "phase": "read",
            "effect": "none",
        },
    )
    assert (nul_error, nul_text, nul_details) == (
        False,
        'Edit path "nul.txt" is not valid UTF-8 text',
        {
            "code": "not_text",
            "path": "nul.txt",
            "phase": "read",
            "effect": "none",
        },
    )

    import oh_my_coding_agent._tools.edit as builtin_tools

    payload = workspace / "full.txt"
    payload.write_bytes(b"keep")

    def full(_handle: int, _data: bytes) -> int:
        raise OSError(errno.ENOSPC, "injected")

    monkeypatch.setattr(builtin_tools, "_write_file_bytes", full)
    full_error, full_text, full_details = outcome("full.txt")
    assert (full_error, full_text, full_details) == (
        False,
        (
            'Edit path "full.txt" could not be completed because storage is '
            "full; the target may be partially or completely changed"
        ),
        {
            "code": "storage_full",
            "path": "full.txt",
            "phase": "write",
            "effect": "target_may_be_partial",
        },
    )


def test_edit_addresses_literal_paths_without_sandboxing_or_magic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"old-out")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", os.fspath(home))
    tilde = workspace / "~" / "nope.txt"
    tilde.parent.mkdir()
    tilde.write_bytes(b"old-tilde")
    target = workspace / "target.txt"
    target.write_bytes(b"linked")
    os.symlink(os.fspath(target), os.fspath(workspace / "alias.txt"))

    def edit(path: str, old: str, new: str) -> None:
        _, events, _ = asyncio.run(
            _prompt_edit(
                tmp_path,
                monkeypatch,
                {"path": path, "edits": [{"oldText": old, "newText": new}]},
                workspace=workspace,
            )
        )
        assert _tool_end(events).isError is False

    edit(os.fspath(outside), "old-out", "escaped")
    assert outside.read_bytes() == b"escaped"
    edit("../outside.txt", "escaped", "via-dot-dot")
    assert outside.read_bytes() == b"via-dot-dot"
    edit("~/nope.txt", "old-tilde", "literal-tilde")
    assert tilde.read_bytes() == b"literal-tilde"
    assert not (home / "nope.txt").exists()
    edit("alias.txt", "linked", "through-link")
    assert target.read_bytes() == b"through-link"
    assert (workspace / "alias.txt").is_symlink()


def test_edit_elides_context_and_preserves_no_final_newline_in_reference_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "big.txt").write_bytes(_ELIDE_OLD.encode("utf-8"))
    _, events, _ = asyncio.run(
        _prompt_edit(
            tmp_path,
            monkeypatch,
            {
                "path": "big.txt",
                "edits": [
                    {"oldText": "START", "newText": "BEGIN"},
                    {"oldText": "END", "newText": "FINISH"},
                ],
            },
            workspace=workspace,
        )
    )
    details = dict(cast(Any, _tool_end(events).result.details))
    assert details["diff"] == _ELIDE_DIFF
    assert details["patch"] == _ELIDE_PATCH
    assert details["firstChangedLine"] == 1
    assert (workspace / "big.txt").read_bytes() == _ELIDE_NEW.encode("utf-8")

    (workspace / "a.txt").write_bytes(b"hello world")
    _, nonl_events, _ = asyncio.run(
        _prompt_edit(
            tmp_path,
            monkeypatch,
            {
                "path": "a.txt",
                "edits": [{"oldText": "hello world", "newText": "hello omh"}],
            },
            workspace=workspace,
        )
    )
    nonl = dict(cast(Any, _tool_end(nonl_events).result.details))
    assert nonl["diff"] == _NONL_DIFF
    assert nonl["patch"] == _NONL_PATCH
    assert (workspace / "a.txt").read_bytes() == b"hello omh"


def test_same_target_edit_and_write_serialize_while_other_work_stays_concurrent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oh_my_coding_agent._tools.edit as builtin_tools

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
                (
                    "call-a",
                    "edit",
                    {
                        "path": "same.txt",
                        "edits": [{"oldText": "seed", "newText": "first"}],
                    },
                ),
                (
                    "call-b",
                    "write",
                    {"path": "same.txt", "content": "second"},
                ),
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


def test_edit_cancellation_before_overwrite_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oh_my_coding_agent._tools.edit as builtin_tools

    workspace = tmp_path / "project"
    workspace.mkdir()
    target = workspace / "held.txt"
    target.write_bytes(b"seed")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    transport = _ScriptedTransport(
        _tool_call_sse(
            "call-edit",
            "edit",
            {"path": "held.txt", "edits": [{"oldText": "seed", "newText": "new"}]},
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
                        os.fspath(workspace), NewSessionOptions(id="cancel-edit")
                    ),
                )
            )
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)
        prompt = asyncio.create_task(session.prompt("edit held"))
        await started.wait()
        await session.abort()
        await prompt
        end = _tool_end(events)
        result = _tool_result(events)
        assert end.isError is True
        assert result.isError is True
        assert end.result.content[0].text == 'Tool "edit" execution was cancelled'
        assert "Successfully replaced" not in end.result.content[0].text
        assert target.read_bytes() == b"seed"
        await session.dispose()

    asyncio.run(scenario())


def test_precomputed_edit_failure_and_started_overwrite_do_not_promise_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oh_my_coding_agent._tools.edit as builtin_tools

    workspace = tmp_path / "project"
    workspace.mkdir()
    target = workspace / "held.txt"
    target.write_bytes(b"seed")

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("injected precompute")

    monkeypatch.setattr(builtin_tools, "_generate_diff_string", boom)
    _, events, _ = asyncio.run(
        _prompt_edit(
            tmp_path,
            monkeypatch,
            {"path": "held.txt", "edits": [{"oldText": "seed", "newText": "new"}]},
            workspace=workspace,
        )
    )
    end = _tool_end(events)
    assert end.isError is True
    assert end.result.content[0].text == 'Tool "edit" execution failed'
    assert target.read_bytes() == b"seed"

    monkeypatch.undo()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    async def run_cancelled() -> Any:
        transport = _ScriptedTransport(
            _tool_call_sse(
                "call-edit",
                "edit",
                {
                    "path": "partial.txt",
                    "edits": [{"oldText": "old", "newText": "new-bytes"}],
                },
            ),
            _STOP_SSE,
        )
        _install_transport(monkeypatch, transport)
        started = asyncio.Event()

        async def drain_write(handle: int, encoded: bytes, signal: Any) -> None:
            del signal
            builtin_tools._write_file_bytes(handle, encoded)

        async def blocked(*args: Any) -> None:
            started.set()
            try:
                await args[-1].wait()
            finally:
                await asyncio.shield(drain_write(*args))
            raise asyncio.CancelledError

        monkeypatch.setattr(builtin_tools, "_write_handle", blocked)
        (workspace / "partial.txt").write_bytes(b"old")
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
        prompt = asyncio.create_task(session.prompt("edit"))
        await started.wait()
        await session.abort()
        await prompt
        end = _tool_end(events)
        await session.dispose()
        return end

    cancelled = asyncio.run(run_cancelled())
    assert cancelled.isError is True
    assert "Successfully replaced" not in cancelled.result.content[0].text
    assert (workspace / "partial.txt").read_bytes() == b"new-bytes"


def test_edit_unexpected_io_and_cleanup_remain_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oh_my_coding_agent._tools.edit as builtin_tools

    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "ok.txt").write_bytes(b"seed")

    def boom_open(path: str) -> int:
        del path
        raise OSError(errno.EIO, "injected")

    monkeypatch.setattr(builtin_tools, "_open_read", boom_open)
    _, events, _ = asyncio.run(
        _prompt_edit(
            tmp_path,
            monkeypatch,
            {"path": "ok.txt", "edits": [{"oldText": "seed", "newText": "new"}]},
            workspace=workspace,
        )
    )
    end = _tool_end(events)
    assert end.isError is True
    assert end.result.content[0].text == 'Tool "edit" execution failed'
    assert (workspace / "ok.txt").read_bytes() == b"seed"

    monkeypatch.undo()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    transport = _ScriptedTransport(
        _tool_call_sse(
            "call-edit",
            "edit",
            {"path": "ok.txt", "edits": [{"oldText": "seed", "newText": "new"}]},
        ),
        _STOP_SSE,
    )
    _install_transport(monkeypatch, transport)

    def boom_close(handle: int) -> None:
        del handle
        raise OSError(errno.EIO, "close failed")

    monkeypatch.setattr(builtin_tools, "_close_handle", boom_close)

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
            await session.prompt("edit ok")
        assert caught.value.code == "cleanup"
        await session.dispose()

    asyncio.run(scenario())


def test_ticket_18_matrix_records_edit_obligations() -> None:
    matrix = json.loads((ROOT / "conformance/obligation-matrix.json").read_text())
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    required = {
        "omh-v0.strict-edit-arguments": "reference.strict-edit-arguments",
        "omh-v0.literal-edit-text": "reference.literal-edit-text",
        "omh-v0.precomputed-edit-result": "reference.precomputed-edit-result",
        "omh-v0.actionable-edit-outcomes": "reference.actionable-edit-outcomes",
        "omh-v0.literal-tool-paths-edit": "reference.literal-tool-paths-edit",
        "omh-v0.edit-mutation-serialization": "reference.edit-mutation-serialization",
    }
    rows = {row["id"]: row for row in matrix["obligations"]}
    cases = {case["id"]: case for case in corpus["cases"]}
    assert required.keys() <= rows.keys()
    assert set(required.values()) <= cases.keys()
    for obligation, corpus_case in required.items():
        assert rows[obligation]["corpusCase"] == corpus_case
        assert rows[obligation]["executableRunners"] == [
            "ticket-18-edit",
            "ticket-18-installed",
        ]
        assert cases[corpus_case]["obligation"] == obligation
