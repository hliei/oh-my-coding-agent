from __future__ import annotations

import asyncio
import errno
import json
import os
from pathlib import Path
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


_STOP_SSE = (
    b'data: {"id":"chatcmpl-stop","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"Done"},"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-stop","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4,"prompt_cache_hit_tokens":0}}\n\n'
    b"data: [DONE]\n\n"
)
_HTTPX_ASYNC_CLIENT = httpx.AsyncClient
_SIMPLE_OLD = "alpha\nbravo\ncharlie\n"
_SIMPLE_DIFF = " 1 alpha\n-2 bravo\n+2 BRAND\n 3 charlie"


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


async def _one_edit(
    root: Path, arguments: dict[str, Any]
) -> tuple[Any, Path]:
    transport = _ScriptedTransport(
        _tool_calls_sse(("call-edit", "edit", arguments)), _STOP_SSE
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
                        os.fspath(workspace), NewSessionOptions(id="edit")
                    ),
                )
            )
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)
        await session.prompt("edit")
        end = _tool_end(events)
        await session.dispose()
        return end, workspace
    finally:
        setattr(httpx, "AsyncClient", original)


async def _arguments(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / "note.txt"
    target.write_bytes(_SIMPLE_OLD.encode("utf-8"))
    empty, _ = await _one_edit(root, {"path": "note.txt", "edits": []})
    legacy, _ = await _one_edit(
        root,
        {"path": "note.txt", "oldText": "bravo", "newText": "BRAND"},
    )
    stringed, _ = await _one_edit(
        root,
        {
            "path": "note.txt",
            "edits": '[{"oldText":"bravo","newText":"BRAND"}]',
        },
    )
    empty_old, _ = await _one_edit(
        root,
        {"path": "note.txt", "edits": [{"oldText": "", "newText": "x"}]},
    )
    return {
        "A": "declared path+edits[] only",
        "L": ["tool_execution_end"],
        "T": {
            "emptyEditsIsError": empty.isError,
            "legacyTopLevelIsError": legacy.isError,
            "jsonStringEditsIsError": stringed.isError,
            "emptyOldTextIsError": empty_old.isError,
        },
        "E": {"mutated": target.read_bytes() != _SIMPLE_OLD.encode("utf-8")},
        "C": "target_unchanged",
    }


async def _literal(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "note.txt").write_bytes(_SIMPLE_OLD.encode("utf-8"))
    simple, simple_root = await _one_edit(
        root,
        {"path": "note.txt", "edits": [{"oldText": "bravo", "newText": "BRAND"}]},
    )
    (workspace / "bom.txt").write_bytes("\ufeffkeep\nchange\n".encode("utf-8"))
    bom, bom_root = await _one_edit(
        root,
        {"path": "bom.txt", "edits": [{"oldText": "change", "newText": "changed"}]},
    )
    (workspace / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    crlf, crlf_root = await _one_edit(
        root,
        {"path": "crlf.txt", "edits": [{"oldText": "a\nb", "newText": "A\nB"}]},
    )
    (workspace / "cafe.txt").write_bytes("caf\u00e9\n".encode("utf-8"))
    cafe, cafe_root = await _one_edit(
        root,
        {
            "path": "cafe.txt",
            "edits": [{"oldText": "cafe\u0301", "newText": "coffee"}],
        },
    )
    (workspace / "dup.txt").write_bytes(b"aaa\n")
    unique, unique_root = await _one_edit(
        root,
        {"path": "dup.txt", "edits": [{"oldText": "aa", "newText": "b"}]},
    )
    (workspace / "noop.txt").write_bytes(b"abc\n")
    noop, noop_root = await _one_edit(
        root,
        {"path": "noop.txt", "edits": [{"oldText": "a", "newText": "a"}]},
    )
    del bom, cafe
    return {
        "A": "literal unique non-overlapping edits admitted",
        "L": ["tool_execution_end"],
        "T": {
            "success": simple.result.content[0].text,
            "simpleDiff": dict(cast(Any, simple.result.details))["diff"],
            "simplePatch": dict(cast(Any, simple.result.details))["patch"],
            "firstChangedLine": dict(cast(Any, simple.result.details))[
                "firstChangedLine"
            ],
            "isError": simple.isError,
            "occurrences": dict(cast(Any, unique.result.details))["occurrences"],
        },
        "E": {
            "simpleBytes": (simple_root / "note.txt").read_bytes()
            == b"alpha\nBRAND\ncharlie\n",
            "bomPreserved": (bom_root / "bom.txt").read_bytes()
            == "\ufeffkeep\nchanged\n".encode("utf-8"),
            "crlfRejected": (crlf_root / "crlf.txt").read_bytes() == b"a\r\nb\r\n"
            and dict(cast(Any, crlf.result.details))["code"] == "text_not_found",
            "fuzzyUnicodeRejected": (cafe_root / "cafe.txt").read_bytes()
            == "caf\u00e9\n".encode("utf-8"),
            "noChangeWritten": (noop_root / "noop.txt").read_bytes() != b"abc\n",
        },
        "C": "bom_restored_and_rejections_unwritten",
    }


async def _precomputed(root: Path) -> dict[str, object]:
    import oh_my_coding_agent._builtin_tools as builtin_tools

    workspace = root / "project"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "held.txt").write_bytes(b"seed")

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("injected precompute")

    previous = builtin_tools._generate_diff_string
    setattr(builtin_tools, "_generate_diff_string", boom)
    try:
        precompute, precompute_root = await _one_edit(
            root,
            {"path": "held.txt", "edits": [{"oldText": "seed", "newText": "new"}]},
        )
    finally:
        setattr(builtin_tools, "_generate_diff_string", previous)

    transport = _ScriptedTransport(
        _tool_calls_sse(
            (
                "call-edit",
                "edit",
                {"path": "held.txt", "edits": [{"oldText": "seed", "newText": "new"}]},
            )
        ),
        _STOP_SSE,
    )
    original = _install_transport(transport)
    started = asyncio.Event()

    async def hold(signal: Any, key: str) -> None:
        del key
        started.set()
        await signal.wait()
        raise asyncio.CancelledError

    previous_hold = builtin_tools._after_queue_hold
    setattr(builtin_tools, "_after_queue_hold", hold)
    try:
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
        session.subscribe(events.append)
        prompt = asyncio.create_task(session.prompt("edit held"))
        await started.wait()
        await session.abort()
        await prompt
        cancelled = _tool_end(events)
        await session.dispose()
    finally:
        setattr(builtin_tools, "_after_queue_hold", previous_hold)
        setattr(httpx, "AsyncClient", original)

    (workspace / "partial.txt").write_bytes(b"old")
    started_write = asyncio.Event()

    async def drain_write(handle: int, encoded: bytes, signal: Any) -> None:
        del signal
        builtin_tools._write_file_bytes(handle, encoded)

    async def blocked(*args: Any) -> None:
        started_write.set()
        try:
            await args[-1].wait()
        finally:
            await asyncio.shield(drain_write(*args))
        raise asyncio.CancelledError

    transport = _ScriptedTransport(
        _tool_calls_sse(
            (
                "call-edit",
                "edit",
                {
                    "path": "partial.txt",
                    "edits": [{"oldText": "old", "newText": "new-bytes"}],
                },
            )
        ),
        _STOP_SSE,
    )
    original = _install_transport(transport)
    previous_write = builtin_tools._write_handle
    setattr(builtin_tools, "_write_handle", blocked)
    try:
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    sessionManager=SessionManager.inMemory(
                        os.fspath(workspace), NewSessionOptions(id="drain")
                    ),
                )
            )
        ).session
        drain_events: list[AgentSessionEvent] = []
        session.subscribe(drain_events.append)
        prompt = asyncio.create_task(session.prompt("edit"))
        await started_write.wait()
        await session.abort()
        await prompt
        drained = _tool_end(drain_events)
        await session.dispose()
    finally:
        setattr(builtin_tools, "_write_handle", previous_write)
        setattr(httpx, "AsyncClient", original)
    del drained
    return {
        "A": "admitted unique replacement",
        "L": ["tool_execution_end"],
        "T": {
            "precomputeIsError": precompute.isError,
            "cancelledIsError": cancelled.isError,
            "successPublished": "Successfully replaced" in (
                precompute.result.content[0].text + cancelled.result.content[0].text
            ),
        },
        "E": {
            "precomputeUnchanged": (precompute_root / "held.txt").read_bytes()
            == b"seed",
            "heldUnchanged": (workspace / "held.txt").read_bytes() == b"seed",
            "startedOverwriteMayComplete": (workspace / "partial.txt").read_bytes()
            == b"new-bytes",
        },
        "C": "session_reusable",
    }


async def _outcomes(root: Path) -> dict[str, object]:
    import oh_my_coding_agent._builtin_tools as builtin_tools

    workspace = root / "project"
    workspace.mkdir(parents=True, exist_ok=True)
    os.symlink(
        os.fspath(workspace / "missing-target"), os.fspath(workspace / "broken")
    )
    (workspace / "dir").mkdir()
    os.symlink("loop-b", os.fspath(workspace / "loop-a"))
    os.symlink("loop-a", os.fspath(workspace / "loop-b"))
    readonly = workspace / "locked.txt"
    readonly.write_bytes(b"keep")
    readonly.chmod(0o444)
    (workspace / "bin.txt").write_bytes(b"\xff\xfe")
    (workspace / "full.txt").write_bytes(b"keep")
    missing, _ = await _one_edit(
        root, {"path": "broken", "edits": [{"oldText": "keep", "newText": "x"}]}
    )
    directory, _ = await _one_edit(
        root, {"path": "dir", "edits": [{"oldText": "keep", "newText": "x"}]}
    )
    loop, _ = await _one_edit(
        root, {"path": "loop-a", "edits": [{"oldText": "keep", "newText": "x"}]}
    )
    try:
        denied, _ = await _one_edit(
            root,
            {"path": "locked.txt", "edits": [{"oldText": "keep", "newText": "new"}]},
        )
    finally:
        readonly.chmod(0o644)
    text, _ = await _one_edit(
        root, {"path": "bin.txt", "edits": [{"oldText": "keep", "newText": "x"}]}
    )

    def full(_handle: int, _data: bytes) -> int:
        raise OSError(errno.ENOSPC, "injected")

    previous_write = builtin_tools._write_file_bytes
    setattr(builtin_tools, "_write_file_bytes", full)
    try:
        storage, _ = await _one_edit(
            root,
            {"path": "full.txt", "edits": [{"oldText": "keep", "newText": "new"}]},
        )
    finally:
        setattr(builtin_tools, "_write_file_bytes", previous_write)

    def boom(path: str) -> int:
        del path
        raise OSError(5, "injected")

    previous_open = builtin_tools._open_read
    setattr(builtin_tools, "_open_read", boom)
    (workspace / "io.txt").write_bytes(b"keep")
    try:
        unexpected, _ = await _one_edit(
            root, {"path": "io.txt", "edits": [{"oldText": "keep", "newText": "x"}]}
        )
    finally:
        setattr(builtin_tools, "_open_read", previous_open)
    return {
        "A": "expected negatives admitted",
        "L": ["tool_execution_end"],
        "T": {
            "missingIsError": missing.isError,
            "missingCode": dict(cast(Any, missing.result.details))["code"],
            "directoryCode": dict(cast(Any, directory.result.details))["code"],
            "invalidCode": dict(cast(Any, loop.result.details))["code"],
            "deniedCode": dict(cast(Any, denied.result.details))["code"],
            "textCode": dict(cast(Any, text.result.details))["code"],
            "storageCode": dict(cast(Any, storage.result.details))["code"],
            "storagePhase": dict(cast(Any, storage.result.details))["phase"],
            "unexpectedIoIsError": unexpected.isError,
        },
        "E": {"identityWrote": readonly.read_bytes() != b"keep"},
        "C": "session_reusable",
    }


async def _literal_paths(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True, exist_ok=True)
    outside = root / "outside.txt"
    outside.write_bytes(b"old-out")
    home = root / "home"
    home.mkdir()
    os.environ["HOME"] = os.fspath(home)
    tilde = workspace / "~" / "nope.txt"
    tilde.parent.mkdir()
    tilde.write_bytes(b"old-tilde")
    target = workspace / "target.txt"
    target.write_bytes(b"linked")
    os.symlink(os.fspath(target), os.fspath(workspace / "alias.txt"))

    async def edit(path: str, old: str, new: str) -> None:
        end, _ = await _one_edit(
            root, {"path": path, "edits": [{"oldText": old, "newText": new}]}
        )
        if end.isError:
            raise RuntimeError(end.result.content[0].text)

    await edit(os.fspath(outside), "old-out", "escaped")
    await edit("../outside.txt", "escaped", "via-dot-dot")
    await edit("~/nope.txt", "old-tilde", "literal-tilde")
    await edit("alias.txt", "linked", "through-link")
    return {
        "A": "literal nonempty NUL-free paths admitted",
        "L": ["tool_execution_end"],
        "T": {
            "absoluteOutsideWorkspace": outside.read_bytes().decode("utf-8"),
            "dotDot": outside.read_bytes().decode("utf-8"),
            "literalTilde": tilde.read_bytes().decode("utf-8"),
            "symlinkTarget": target.read_bytes().decode("utf-8"),
            "symlinkPreserved": (workspace / "alias.txt").is_symlink(),
        },
        "E": {"sandboxRejected": False, "homeExpanded": (home / "nope.txt").exists()},
        "C": "workspace_unchanged_as_identity",
    }


async def _serialize(root: Path) -> dict[str, object]:
    import oh_my_coding_agent._builtin_tools as builtin_tools

    workspace = root / "project"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "same.txt").write_bytes(b"seed")
    (workspace / "other.txt").write_bytes(b"other-seed")
    same_first = asyncio.Event()
    same_second = asyncio.Event()
    other_inside = asyncio.Event()
    release_same = asyncio.Event()
    same_holders = 0
    original_hold = builtin_tools._after_queue_hold

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
        await original_hold(signal, key)

    setattr(builtin_tools, "_after_queue_hold", gated)
    transport = _ScriptedTransport(
        _tool_calls_sse(
            (
                "call-a",
                "edit",
                {"path": "same.txt", "edits": [{"oldText": "seed", "newText": "first"}]},
            ),
            ("call-b", "write", {"path": "same.txt", "content": "second"}),
            ("call-c", "write", {"path": "other.txt", "content": "other"}),
            ("call-d", "read", {"path": "other.txt"}),
        ),
        _STOP_SSE,
    )
    original_client = _install_transport(transport)
    try:
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
        session.subscribe(events.append)
        prompt = asyncio.create_task(session.prompt("mutate"))
        await same_first.wait()
        await other_inside.wait()
        second_while_first_held = same_second.is_set()
        seed_while_held = (workspace / "same.txt").read_bytes() == b"seed"
        while not any(
            isinstance(event, AgentSessionEvent.ToolExecutionEnd)
            and event.toolCallId in {"call-c", "call-d"}
            for event in events
        ):
            await asyncio.sleep(0)
        concurrent_peer = True
        release_same.set()
        await prompt
        ends = {
            event.toolCallId: event.isError
            for event in events
            if isinstance(event, AgentSessionEvent.ToolExecutionEnd)
        }
        await session.dispose()
        return {
            "A": "parallel batch admitted",
            "L": ["tool_execution_end"],
            "T": {
                "errors": ends,
                "secondWaited": not second_while_first_held,
            },
            "E": {
                "sameFinal": (workspace / "same.txt").read_bytes().decode("utf-8"),
                "otherFinal": (workspace / "other.txt").read_bytes().decode("utf-8"),
                "seedWhileHeld": seed_while_held,
                "peerCompletedWhileHeld": concurrent_peer,
            },
            "C": "same_key_serialized_other_keys_concurrent",
        }
    finally:
        setattr(builtin_tools, "_after_queue_hold", original_hold)
        setattr(httpx, "AsyncClient", original_client)


async def main() -> None:
    os.environ["DEEPSEEK_API_KEY"] = "omh-conformance-canary"
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        actual = {
            "reference.strict-edit-arguments": await _arguments(root / "args"),
            "reference.literal-edit-text": await _literal(root / "literal"),
            "reference.precomputed-edit-result": await _precomputed(root / "pre"),
            "reference.actionable-edit-outcomes": await _outcomes(root / "outcomes"),
            "reference.literal-tool-paths-edit": await _literal_paths(root / "paths"),
            "reference.edit-mutation-serialization": await _serialize(
                root / "serialize"
            ),
        }
        print(json.dumps(actual, sort_keys=True, separators=(",", ":")))


asyncio.run(main())
