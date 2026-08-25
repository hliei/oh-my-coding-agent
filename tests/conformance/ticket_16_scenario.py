from __future__ import annotations

import asyncio
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
_PROMPT_SUMMARIES = [
    "Read file contents",
    "Execute bash commands (ls, grep, find, etc.)",
    "Make precise file edits with exact text replacement, including multiple disjoint edits in one call",
    "Create or overwrite files",
]
_HTTPX_ASYNC_CLIENT = httpx.AsyncClient


def _sse(*payloads: dict[str, Any] | str) -> bytes:
    chunks: list[bytes] = []
    for payload in payloads:
        if payload == "[DONE]":
            chunks.append(b"data: [DONE]\n\n")
        else:
            chunks.append(b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n")
    return b"".join(chunks)


def _tool_call_sse(name: str, arguments: dict[str, Any]) -> bytes:
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
                                "id": "call-read",
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


async def _one_read(
    root: Path, arguments: dict[str, Any]
) -> tuple[Any, list[httpx.Request]]:
    transport = _ScriptedTransport(_tool_call_sse("read", arguments), _STOP_SSE)
    original = _install_transport(transport)
    try:
        workspace = root / "project"
        workspace.mkdir(parents=True, exist_ok=True)
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    sessionManager=SessionManager.inMemory(
                        os.fspath(workspace), NewSessionOptions(id="read")
                    ),
                )
            )
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)
        await session.prompt("read")
        end = _tool_end(events)
        await session.dispose()
        return end, transport.requests
    finally:
        setattr(httpx, "AsyncClient", original)


async def _registry(root: Path) -> dict[str, object]:
    transport = _ScriptedTransport(_STOP_SSE)
    original = _install_transport(transport)
    try:
        workspace = root / "project"
        workspace.mkdir(parents=True, exist_ok=True)
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    sessionManager=SessionManager.inMemory(
                        os.fspath(workspace), NewSessionOptions(id="registry")
                    ),
                )
            )
        ).session
        summaries = session.systemPrompt.split("\n")
        await session.prompt("inspect")
        body = json.loads(transport.requests[0].content)
        names = [tool["function"]["name"] for tool in body["tools"]]
        await session.dispose()
        return {
            "A": "four reserved built-ins admitted",
            "L": [],
            "T": {
                "names": names,
                "promptSummaries": summaries,
                "excluded": [
                    name for name in ("grep", "find", "ls") if name not in names
                ],
            },
            "E": {"selectionField": hasattr(CreateAgentSessionOptions, "tools")},
            "C": "no override configuration",
        }
    finally:
        setattr(httpx, "AsyncClient", original)


async def _literal_paths(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True, exist_ok=True)
    (root / "outside.txt").write_text("escaped", encoding="utf-8")
    (workspace / "~").mkdir()
    (workspace / "~" / "nope.txt").write_text("literal-tilde", encoding="utf-8")
    (workspace / "target.txt").write_text("linked", encoding="utf-8")
    os.symlink(os.fspath(workspace / "target.txt"), os.fspath(workspace / "alias.txt"))

    async def text(path: str) -> Any:
        end, _ = await _one_read(root, {"path": path})
        return end.result.content[0].text

    return {
        "A": "literal nonempty NUL-free paths admitted",
        "L": ["tool_execution_end"],
        "T": {
            "absoluteOutsideWorkspace": await text(os.fspath(root / "outside.txt")),
            "dotDot": await text("../outside.txt"),
            "literalTilde": await text("~/nope.txt"),
            "symlink": await text("alias.txt"),
        },
        "E": {"sandboxRejected": False},
        "C": "workspace_unchanged",
    }


async def _strict_text(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "note.txt").write_bytes(b"\xef\xbb\xbfhello\r\n")
    (workspace / "dir").mkdir()
    os.mkfifo(os.fspath(workspace / "pipe"))
    (workspace / "bad.bin").write_bytes(b"\xff")
    (workspace / "nul.txt").write_bytes(b"a\x00b")
    (workspace / "huge.txt").write_bytes(b"b" * 51_201)

    async def end_for(path: str) -> Any:
        result, _ = await _one_read(root, {"path": path})
        return result

    bom = await end_for("note.txt")
    directory = await end_for("dir")
    fifo = await end_for("pipe")
    invalid = await end_for("bad.bin")
    nul = await end_for("nul.txt")
    huge = await end_for("huge.txt")
    return {
        "A": "regular UTF-8 admitted; other kinds are Outcomes",
        "L": ["tool_execution_end"],
        "T": {
            "bomCrlfPreserved": bom.result.content[0].text == "\ufeffhello\r\n",
            "directory": dict(cast(Any, directory.result.details))["code"],
            "fifo": dict(cast(Any, fifo.result.details))["code"],
            "invalidUtf8": dict(cast(Any, invalid.result.details))["code"],
            "decodedNul": dict(cast(Any, nul.result.details))["code"],
            "firstLineExceedsLimit": dict(
                cast(Any, huge.result.details)["truncation"]
            )["firstLineExceedsLimit"],
            "isError": any(
                item.isError
                for item in (bom, directory, fifo, invalid, nul, huge)
            ),
        },
        "E": {"spillArtifact": "omh-bash-" in huge.result.content[0].text},
        "C": "files_unchanged",
    }


async def _pagination(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "empty.txt").write_bytes(b"")
    (workspace / "final-lf.txt").write_bytes(b"a\n")
    (workspace / "lines.txt").write_text("one\ntwo\nthree\nfour", encoding="utf-8")

    async def read(path: str, **fields: int) -> Any:
        result, _ = await _one_read(root, {"path": path, **fields})
        return result

    empty = await read("empty.txt")
    trailing = await read("final-lf.txt", offset=2)
    window = await read("lines.txt", offset=2, limit=2)
    beyond = await read("lines.txt", offset=8)
    zero = await read("lines.txt", offset=0)
    return {
        "A": "positive safe integers admitted; offset 0 rejected by schema",
        "L": ["tool_execution_end"],
        "T": {
            "empty": empty.result.content[0].text,
            "terminalLf": trailing.result.content[0].text,
            "window": window.result.content[0].text,
            "beyondEnd": dict(cast(Any, beyond.result.details))["code"],
            "zeroOffsetIsError": zero.isError,
        },
        "E": {"fileMutated": False},
        "C": "continuation_available",
    }


async def _outcomes(root: Path) -> dict[str, object]:
    import oh_my_coding_agent._tools.read as builtin_tools

    workspace = root / "project"
    workspace.mkdir(parents=True, exist_ok=True)
    secret = workspace / "secret.txt"
    secret.write_text("hidden", encoding="utf-8")
    secret.chmod(0)
    try:
        missing, _ = await _one_read(root, {"path": "absent.txt"})
        denied, _ = await _one_read(root, {"path": "secret.txt"})
    finally:
        secret.chmod(0o644)

    held = workspace / "held.txt"
    held.write_text("secret-bytes", encoding="utf-8")
    transport = _ScriptedTransport(
        _tool_call_sse("read", {"path": "held.txt"}), _STOP_SSE
    )
    original = _install_transport(transport)
    started = asyncio.Event()

    async def hold(signal: Any) -> None:
        started.set()
        await signal.wait()
        raise asyncio.CancelledError

    previous_yield = builtin_tools._yield_for_cancellation
    builtin_tools._yield_for_cancellation = hold
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
        prompt = asyncio.create_task(session.prompt("read held"))
        await started.wait()
        await session.abort()
        await prompt
        cancelled = _tool_end(events)
        await session.dispose()
    finally:
        builtin_tools._yield_for_cancellation = previous_yield
        setattr(httpx, "AsyncClient", original)

    def boom(path: str, *args: object, **kwargs: object) -> Any:
        del path, args, kwargs
        raise OSError(5, "injected")

    previous_stat = builtin_tools._stat_path
    builtin_tools._stat_path = boom
    try:
        unexpected, _ = await _one_read(root, {"path": "held.txt"})
    finally:
        builtin_tools._stat_path = previous_stat
    return {
        "A": "expected negatives admitted",
        "L": ["tool_execution_end"],
        "T": {
            "missingIsError": missing.isError,
            "missingCode": dict(cast(Any, missing.result.details))["code"],
            "unreadableIsError": denied.isError,
            "cancelledIsError": cancelled.isError,
            "unexpectedIoIsError": unexpected.isError,
        },
        "E": {"laterIoAfterCancel": "secret-bytes" in cancelled.result.content[0].text},
        "C": "session_reusable",
    }


async def main() -> None:
    os.environ["DEEPSEEK_API_KEY"] = "omh-conformance-canary"
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        actual = {
            "reference.builtin-tool-registry": await _registry(root / "registry"),
            "reference.literal-tool-paths-read": await _literal_paths(root / "paths"),
            "reference.strict-text-file-read": await _strict_text(root / "text"),
            "reference.strict-read-pagination": await _pagination(root / "pages"),
            "reference.actionable-read-outcomes": await _outcomes(root / "outcomes"),
        }
        print(json.dumps(actual, sort_keys=True, separators=(",", ":")))


asyncio.run(main())
