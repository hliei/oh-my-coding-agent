from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import httpx

from oh_my_coding_agent import (
    AgentSessionEvent,
    CreateAgentSessionOptions,
    SessionManager,
    createAgentSession,
)
from oh_my_llm import (
    AssistantMessage,
    LifecycleError,
    UserMessage,
    fauxAssistantMessage,
)


_TEXT_SSE = (
    b'data: {"id":"installed","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"Done"},"finish_reason":null}]}\n\n'
    b'data: {"id":"installed","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4,"prompt_cache_hit_tokens":0}}\n\n'
    b"data: [DONE]\n\n"
)


class _LifecycleTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.block_starts: asyncio.Queue[int] = asyncio.Queue()
        self.cancelled = 0
        self.disposal_cancelled = asyncio.Event()
        self.release_disposal = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = len(self.requests)
        if index == 1:
            return httpx.Response(500, json={"error": "redacted"}, request=request)
        if index in (2, 4):
            await self.block_starts.put(index)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                if index == 4:
                    self.disposal_cancelled.set()
                    await self.release_disposal.wait()
                raise
        return httpx.Response(
            200,
            content=_TEXT_SSE,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )


class _SummaryTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            content=_TEXT_SSE.replace(b"Done", b"installed checkpoint"),
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )


class _AutomaticTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self.requests) == 1:
            return httpx.Response(
                400,
                json={"error": {"code": "context_length_exceeded"}},
                request=request,
            )
        content = (
            _TEXT_SSE.replace(b"Done", b"automatic checkpoint")
            if len(self.requests) == 2
            else _TEXT_SSE
        )
        return httpx.Response(
            200,
            content=content,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )


def _install_transport(transport: httpx.AsyncBaseTransport) -> type[httpx.AsyncClient]:
    original = httpx.AsyncClient

    class FakeClient(original):  # type: ignore[valid-type,misc]
        def __init__(self, **kwargs: Any) -> None:
            kwargs = dict(kwargs)
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    setattr(httpx, "AsyncClient", FakeClient)
    return original


async def _disposal_fault_evidence(root: Path) -> dict[str, bool]:
    retry_session = (
        await createAgentSession(
            CreateAgentSessionOptions(
                sessionManager=SessionManager.inMemory(os.fspath(root / "retry"))
            )
        )
    ).session
    retry_unsubscribe = retry_session._agent_unsubscribe
    assert retry_unsubscribe is not None
    retry_calls = 0

    def flaky_retry_unsubscribe() -> None:
        nonlocal retry_calls
        retry_calls += 1
        if retry_calls == 1:
            raise RuntimeError("installed cleanup failure")
        retry_unsubscribe()

    retry_session._agent_unsubscribe = flaky_retry_unsubscribe
    first_cleanup_failed = False
    try:
        await retry_session.dispose()
    except LifecycleError as error:
        first_cleanup_failed = error.code == "disposal"
    await retry_session.dispose()

    context_session = (
        await createAgentSession(
            CreateAgentSessionOptions(
                sessionManager=SessionManager.inMemory(os.fspath(root / "context"))
            )
        )
    ).session
    context_unsubscribe = context_session._agent_unsubscribe
    assert context_unsubscribe is not None
    context_calls = 0

    def flaky_context_unsubscribe() -> None:
        nonlocal context_calls
        context_calls += 1
        if context_calls == 1:
            raise RuntimeError("installed context cleanup failure")
        context_unsubscribe()

    context_session._agent_unsubscribe = flaky_context_unsubscribe
    body_failure = RuntimeError("installed body failure")
    body_primary = False
    disposal_additional = False
    try:
        async with context_session:
            raise body_failure
    except RuntimeError as error:
        body_primary = error is body_failure
        disposal_additional = isinstance(error.__context__, LifecycleError)
    await context_session.dispose()

    return {
        "bodyPrimary": body_primary,
        "cleanupRetry": first_cleanup_failed and retry_calls == 2,
        "disposalAdditional": disposal_additional,
    }


async def _lifecycle(root: Path) -> dict[str, dict[str, object]]:
    transport = _LifecycleTransport()
    original = _install_transport(transport)
    try:
        manager = SessionManager.inMemory(os.fspath(root))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)

        await session.prompt("error")
        cancelled_prompt = asyncio.create_task(session.prompt("cancel"))
        assert await transport.block_starts.get() == 2
        await asyncio.gather(session.abort(), session.abort())
        await cancelled_prompt
        await session.prompt("reuse")

        active_prompt = asyncio.create_task(session.prompt("dispose"))
        assert await transport.block_starts.get() == 4
        first_disposal = asyncio.create_task(session.dispose())
        second_disposal = asyncio.create_task(session.dispose())
        await transport.disposal_cancelled.wait()
        first_disposal.cancel()
        transport.release_disposal.set()
        await active_prompt
        await second_disposal
        cancelled_waiter = False
        try:
            await first_disposal
        except asyncio.CancelledError:
            cancelled_waiter = True
        disposed_code = ""
        try:
            await session.prompt("closed")
        except Exception as error:
            disposed_code = getattr(error, "code", type(error).__name__)

        assistants = [
            message
            for message in manager.buildSessionContext().messages
            if isinstance(message, AssistantMessage)
        ]
        stop_reasons = [message.stopReason for message in assistants]
        disposal_faults = await _disposal_fault_evidence(root / "faults")
        return {
            "reference.session-cancel-error-reuse": {
                "A": "error_cancel_and_reuse",
                "L": [
                    event.type
                    for event in events
                    if event.type in ("agent_end", "agent_settled")
                ],
                "T": {"stopReasons": stop_reasons[:3]},
                "E": {
                    "cancelledRequests": transport.cancelled,
                    "requestCountBeforeDisposal": 3,
                    "settledMarkers": 0,
                },
                "C": {"reuseSucceeded": stop_reasons[2] == "stop"},
            },
            "reference.session-managed-disposal": {
                "A": "concurrent_active_disposal",
                "L": [],
                "T": {"finalStopReason": stop_reasons[-1]},
                "E": {"requestCount": len(transport.requests)},
                "C": {
                    "cancelledWaiter": cancelled_waiter,
                    **disposal_faults,
                    "disposedAdmission": disposed_code,
                    "idle": session.isIdle,
                    "streaming": session.isStreaming,
                },
            },
        }
    finally:
        setattr(httpx, "AsyncClient", original)


async def _compaction(root: Path) -> dict[str, object]:
    transport = _SummaryTransport()
    original = _install_transport(transport)
    try:
        manager = SessionManager.inMemory(os.fspath(root))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        kept = manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        before = len(manager.getEntries())
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)
        result = await session.compact()
        observation: dict[str, object] = {
            "A": "manual_compaction",
            "L": [event.type for event in events],
            "T": {
                "entryDelta": len(manager.getEntries()) - before,
                "firstKeptMatches": result.firstKeptEntryId == kept,
                "fullTreeRetained": len(manager.getEntries()) == before + 1,
                "summary": result.summary,
            },
            "E": {"requestCount": len(transport.requests)},
            "C": {
                "contextRoles": [message.role for message in session.messages],
                "managerMatchesSession": (
                    manager.buildSessionContext().messages == session.messages
                ),
            },
        }
        await session.dispose()
        return observation
    finally:
        setattr(httpx, "AsyncClient", original)


async def _automatic_compaction(root: Path) -> dict[str, object]:
    transport = _AutomaticTransport()
    original = _install_transport(transport)
    try:
        manager = SessionManager.inMemory(os.fspath(root))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        before = len(manager.getEntries())
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)

        await session.prompt("installed overflow")

        observation: dict[str, object] = {
            "agentEndWillRetry": [
                event.willRetry
                for event in events
                if isinstance(event, AgentSessionEvent.AgentEnd)
            ],
            "compactionReasons": [
                event.reason
                for event in events
                if isinstance(event, AgentSessionEvent.CompactionStart)
            ],
            "entryDelta": len(manager.getEntries()) - before,
            "requestCount": len(transport.requests),
            "settled": events[-1].type == "agent_settled",
        }
        await session.dispose()
        return observation
    finally:
        setattr(httpx, "AsyncClient", original)


async def main() -> None:
    os.environ["DEEPSEEK_API_KEY"] = "installed-key"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        observations = await _lifecycle(root / "lifecycle")
        compaction = await _compaction(root / "compaction")
        compaction["automatic"] = await _automatic_compaction(root / "automatic")
        observations["reference.session-compaction"] = compaction
    print(json.dumps(observations, sort_keys=True, separators=(",", ":")))


asyncio.run(main())
