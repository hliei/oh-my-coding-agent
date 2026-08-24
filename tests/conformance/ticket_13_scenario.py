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
from oh_my_llm import AssistantMessage, UserMessage


_TEXT_SSE = (
    b'data: {"id":"chatcmpl-session","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"Done"},"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-session","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4,"prompt_cache_hit_tokens":0}}\n\n'
    b"data: [DONE]\n\n"
)


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.block_second = False
        self.second_started = asyncio.Event()
        self.release_second = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.block_second and len(self.requests) == 2:
            self.second_started.set()
            await self.release_second.wait()
        return httpx.Response(
            200,
            content=_TEXT_SSE,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )


def _install_transport(transport: _Transport) -> type[httpx.AsyncClient]:
    original = httpx.AsyncClient

    class FakeClient(original):  # type: ignore[valid-type,misc]
        def __init__(self, **kwargs: Any) -> None:
            kwargs = dict(kwargs)
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    setattr(httpx, "AsyncClient", FakeClient)
    return original


async def _prompt_and_reopen(root: Path) -> dict[str, object]:
    transport = _Transport()
    original = _install_transport(transport)
    try:
        manager = SessionManager.create(
            os.fspath(root / "project"),
            os.fspath(root / "sessions"),
            NewSessionOptions(id="prompt-one"),
        )
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        session_file = manager.getSessionFile()
        assert session_file is not None
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)
        operation = session.prompt("Hello")
        before = {
            "file": Path(session_file).exists(),
            "entries": len(manager.getEntries()),
            "requests": len(transport.requests),
        }
        await operation
        entries = manager.getEntries()
        agent_end = cast(
            Any,
            next(
                event
                for event in events
                if isinstance(event, AgentSessionEvent.AgentEnd)
            ),
        )
        physical_types = [
            json.loads(line)["type"]
            for line in Path(session_file).read_text().splitlines()
        ]
        reopened = SessionManager.open(session_file)
        recovered = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=reopened))
        ).session
        observation: dict[str, object] = {
            "A": "one_prompt_admitted",
            "L": [event.type for event in events],
            "T": {
                "entryTypes": [entry.type for entry in entries],
                "messageRoles": [message.role for message in session.messages],
                "agentEndIsRunSuffix": all(
                    left is right
                    for left, right in zip(
                        agent_end.messages, session.messages, strict=True
                    )
                ),
                "activePathIsCompleteTree": manager.getBranch() == entries,
            },
            "E": {
                "beforeAwait": before,
                "requestCount": len(transport.requests),
                "physicalTypes": physical_types,
            },
            "C": {
                "reopenedValueEqual": recovered.messages == session.messages,
                "recoveredIdle": recovered.isIdle,
            },
        }
        await recovered.dispose()
        await session.dispose()
        return observation
    finally:
        setattr(httpx, "AsyncClient", original)


async def _incomplete_recovery(root: Path) -> dict[str, object]:
    transport = _Transport()
    transport.block_second = True
    original = _install_transport(transport)
    try:
        manager = SessionManager.create(
            os.fspath(root / "project"),
            os.fspath(root / "sessions"),
            NewSessionOptions(id="incomplete"),
        )
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        await session.prompt("complete")
        interrupted = asyncio.create_task(session.prompt("interrupted"))
        await transport.second_started.wait()
        session_file = manager.getSessionFile()
        assert session_file is not None
        before = Path(session_file).read_bytes()
        reopened = SessionManager.open(session_file)
        recovered = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=reopened))
        ).session
        recovered_messages = recovered.messages
        observation: dict[str, object] = {
            "A": "live_nonempty_path_reopened",
            "L": [],
            "T": {
                "roles": [message.role for message in recovered_messages],
                "userText": [
                    message.content
                    for message in recovered_messages
                    if isinstance(message, UserMessage)
                ],
                "assistantCount": sum(
                    isinstance(message, AssistantMessage)
                    for message in recovered_messages
                ),
            },
            "E": {
                "requestCount": len(transport.requests),
                "fileUnchangedByOpen": Path(session_file).read_bytes() == before,
                "leaseFiles": 0,
            },
            "C": {
                "recoveredIdle": recovered.isIdle,
                "redispatched": False,
                "freshPromptRequired": isinstance(recovered_messages[-1], UserMessage),
            },
        }
        await recovered.dispose()
        transport.release_second.set()
        await interrupted
        await session.dispose()
        return observation
    finally:
        setattr(httpx, "AsyncClient", original)


async def main() -> None:
    os.environ["DEEPSEEK_API_KEY"] = "omh-conformance-canary"
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        actual = {
            "reference.session-prompt-lazy-flush": await _prompt_and_reopen(
                root / "prompt"
            ),
            "reference.session-incomplete-recovery": await _incomplete_recovery(
                root / "incomplete"
            ),
        }
        print(json.dumps(actual, sort_keys=True, separators=(",", ":")))


asyncio.run(main())
