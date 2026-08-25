from __future__ import annotations

import asyncio
import errno
import json
import os
from pathlib import Path
import tempfile
from typing import Any, cast

import httpx

import oh_my_coding_agent._tools.mutation_queue as mutation_queue
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


async def _one_write(
    root: Path, arguments: dict[str, Any]
) -> tuple[Any, Path]:
    transport = _ScriptedTransport(
        _tool_calls_sse(("call-write", "write", arguments)), _STOP_SSE
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
                        os.fspath(workspace), NewSessionOptions(id="write")
                    ),
                )
            )
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)
        await session.prompt("write")
        end = _tool_end(events)
        await session.dispose()
        return end, workspace
    finally:
        setattr(httpx, "AsyncClient", original)


async def _truthful(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "old.txt").write_bytes(b"old-content-that-is-longer")
    empty, empty_root = await _one_write(root, {"path": "empty.txt", "content": ""})
    quoted, quoted_root = await _one_write(
        root,
        {"path": 'say "hi".txt', "content": "caf\u00e9\ne\u0301\r\n\U0001f600"},
    )
    nested, nested_root = await _one_write(
        root, {"path": "a/b/c.txt", "content": "nested"}
    )
    overwritten, overwritten_root = await _one_write(
        root, {"path": "old.txt", "content": "new"}
    )
    control, control_root = await _one_write(
        root, {"path": "weird\tname.txt", "content": "ok"}
    )
    return {
        "A": "literal path and Unicode-scalar content admitted",
        "L": ["tool_execution_end"],
        "T": {
            "empty": empty.result.content[0].text,
            "unicode": quoted.result.content[0].text,
            "control": control.result.content[0].text,
            "parents": nested.result.content[0].text,
            "overwrite": overwritten.result.content[0].text,
            "isError": any(
                item.isError
                for item in (empty, quoted, nested, overwritten, control)
            ),
        },
        "E": {
            "emptyBytes": (empty_root / "empty.txt").read_bytes() == b"",
            "unicodeBytes": (quoted_root / 'say "hi".txt').read_bytes()
            == b"caf\xc3\xa9\ne\xcc\x81\r\n\xf0\x9f\x98\x80",
            "nestedBytes": (nested_root / "a" / "b" / "c.txt").read_bytes()
            == b"nested",
            "overwritten": (overwritten_root / "old.txt").read_bytes() == b"new",
            "controlBytes": (control_root / "weird\tname.txt").read_bytes() == b"ok",
        },
        "C": "workspace_files_match_reported_bytes",
    }


async def _literal_paths(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True, exist_ok=True)
    outside = root / "outside.txt"
    home = root / "home"
    home.mkdir()
    os.environ["HOME"] = os.fspath(home)
    target = workspace / "target.txt"
    target.write_bytes(b"linked")
    os.symlink(os.fspath(target), os.fspath(workspace / "alias.txt"))

    async def write(path: str, content: str) -> bool:
        end, _ = await _one_write(root, {"path": path, "content": content})
        return end.isError is False

    await write(os.fspath(outside), "escaped")
    await write("../outside.txt", "via-dot-dot")
    await write("~/nope.txt", "literal-tilde")
    await write("alias.txt", "through-link")
    return {
        "A": "literal nonempty NUL-free paths admitted",
        "L": ["tool_execution_end"],
        "T": {
            "absoluteOutsideWorkspace": outside.read_bytes().decode("utf-8"),
            "dotDot": outside.read_bytes().decode("utf-8"),
            "literalTilde": (workspace / "~" / "nope.txt").read_bytes().decode(
                "utf-8"
            ),
            "symlinkTarget": target.read_bytes().decode("utf-8"),
            "symlinkPreserved": (workspace / "alias.txt").is_symlink(),
        },
        "E": {"sandboxRejected": False, "homeExpanded": (home / "nope.txt").exists()},
        "C": "workspace_unchanged_as_identity",
    }


async def _outcomes(root: Path) -> dict[str, object]:
    import oh_my_coding_agent._tools.write as builtin_tools

    workspace = root / "project"
    workspace.mkdir(parents=True, exist_ok=True)
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
    missing, _ = await _one_write(root, {"path": "broken", "content": "x"})
    parent, _ = await _one_write(root, {"path": "notdir/child.txt", "content": "x"})
    directory, _ = await _one_write(root, {"path": "dir", "content": "x"})
    loop, _ = await _one_write(root, {"path": "loop-a", "content": "x"})
    try:
        denied, _ = await _one_write(root, {"path": "locked.txt", "content": "new"})
    finally:
        readonly.chmod(0o644)

    transport = _ScriptedTransport(
        _tool_calls_sse(
            ("call-write", "write", {"path": "held.txt", "content": "secret-bytes"})
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

    previous = mutation_queue.after_queue_hold
    mutation_queue.after_queue_hold = hold
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
        prompt = asyncio.create_task(session.prompt("write held"))
        await started.wait()
        await session.abort()
        await prompt
        cancelled = _tool_end(events)
        await session.dispose()
    finally:
        mutation_queue.after_queue_hold = previous
        setattr(httpx, "AsyncClient", original)

    def boom(path: str) -> int:
        del path
        raise OSError(5, "injected")

    previous_open = builtin_tools._open_write
    builtin_tools._open_write = boom
    try:
        unexpected, _ = await _one_write(root, {"path": "io.txt", "content": "x"})
    finally:
        builtin_tools._open_write = previous_open

    def full(handle: int, data: bytes) -> int:
        del handle, data
        raise OSError(errno.ENOSPC, "injected")

    previous_write = builtin_tools._write_file_bytes
    builtin_tools._write_file_bytes = full
    try:
        storage, _ = await _one_write(root, {"path": "full.txt", "content": "payload"})
    finally:
        builtin_tools._write_file_bytes = previous_write
    return {
        "A": "expected negatives admitted",
        "L": ["tool_execution_end"],
        "T": {
            "missingIsError": missing.isError,
            "missingCode": dict(cast(Any, missing.result.details))["code"],
            "parentCode": dict(cast(Any, parent.result.details))["code"],
            "parentEffect": dict(cast(Any, parent.result.details))["effect"],
            "directoryCode": dict(cast(Any, directory.result.details))["code"],
            "directoryEffect": dict(cast(Any, directory.result.details))["effect"],
            "invalidCode": dict(cast(Any, loop.result.details))["code"],
            "deniedCode": dict(cast(Any, denied.result.details))["code"],
            "storageCode": dict(cast(Any, storage.result.details))["code"],
            "storagePhase": dict(cast(Any, storage.result.details))["phase"],
            "cancelledIsError": cancelled.isError,
            "unexpectedIoIsError": unexpected.isError,
        },
        "E": {"heldCreated": (workspace / "held.txt").exists()},
        "C": "session_reusable",
    }


async def _serialize(root: Path) -> dict[str, object]:
    import oh_my_coding_agent._tools.write as builtin_tools

    workspace = root / "project"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "same.txt").write_bytes(b"seed")
    (workspace / "other.txt").write_bytes(b"other-seed")
    same_first = asyncio.Event()
    same_second = asyncio.Event()
    other_inside = asyncio.Event()
    release_same = asyncio.Event()
    same_holders = 0
    original_hold = mutation_queue.after_queue_hold

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

    mutation_queue.after_queue_hold = gated
    transport = _ScriptedTransport(
        _tool_calls_sse(
            ("call-a", "write", {"path": "same.txt", "content": "first"}),
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
        second_while_first_held = same_second.is_set()
        seed_while_held = (workspace / "same.txt").read_bytes() == b"seed"
        await peer_finished.wait()
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
        mutation_queue.after_queue_hold = original_hold
        setattr(httpx, "AsyncClient", original_client)


async def main() -> None:
    os.environ["DEEPSEEK_API_KEY"] = "omh-conformance-canary"
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        actual = {
            "reference.truthful-write-result": await _truthful(root / "truth"),
            "reference.literal-tool-paths-write": await _literal_paths(root / "paths"),
            "reference.actionable-write-outcomes": await _outcomes(root / "outcomes"),
            "reference.write-mutation-serialization": await asyncio.wait_for(
                _serialize(root / "serialize"), timeout=10.0
            ),
        }
        print(json.dumps(actual, sort_keys=True, separators=(",", ":")))


asyncio.run(main())
