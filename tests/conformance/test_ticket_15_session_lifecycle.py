from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import os
from pathlib import Path
from typing import Any, BinaryIO

import httpx
import pytest

import oh_my_coding_agent._session_manager as session_manager_module

from oh_my_coding_agent import (
    AgentSessionEvent,
    CreateAgentSessionOptions,
    SessionMessageEntry,
    SessionManager,
    createAgentSession,
)
from oh_my_llm import (
    AssistantMessage,
    LifecycleError,
    ModelsError,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
    fauxAssistantMessage,
    fauxText,
)


ROOT = Path(__file__).parents[2]


_TEXT_SSE = (
    b'data: {"id":"chatcmpl-session","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"Done"},"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-session","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4,"prompt_cache_hit_tokens":0}}\n\n'
    b"data: [DONE]\n\n"
)

_HIGH_USAGE_SSE = (
    b'data: {"id":"high","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"answer"},"finish_reason":null}]}\n\n'
    b'data: {"id":"high","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":114999,"completion_tokens":1,"total_tokens":115000,"prompt_cache_hit_tokens":0}}\n\n'
    b"data: [DONE]\n\n"
)

_HIGH_LENGTH_SSE = (
    b'data: {"id":"length","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
    b'data: {"id":"length","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"length"}],"usage":{"prompt_tokens":129999,"completion_tokens":1,"total_tokens":130000,"prompt_cache_hit_tokens":0}}\n\n'
    b"data: [DONE]\n\n"
)


class _FirstRequestBarrier(httpx.AsyncBaseTransport):
    def __init__(self, *, hold_cancellation: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self.first_started = asyncio.Event()
        self.first_cancelled = asyncio.Event()
        self.release_cancellation = asyncio.Event()
        self.hold_cancellation = hold_cancellation

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self.requests) == 1:
            self.first_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.first_cancelled.set()
                if self.hold_cancellation:
                    await self.release_cancellation.wait()
                raise
        return httpx.Response(
            200,
            content=_TEXT_SSE,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )


class _SecondRequestBarrier(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.second_started = asyncio.Event()
        self.release_second = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self.requests) == 2:
            self.second_started.set()
            await self.release_second.wait()
        return httpx.Response(
            200,
            content=_TEXT_SSE,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport
) -> None:
    class FakeClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            kwargs = dict(kwargs)
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)


def test_concurrent_abort_settles_model_history_before_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    async def scenario() -> None:
        transport = _FirstRequestBarrier()
        _install_transport(monkeypatch, transport)
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        settled_histories: list[tuple[str, ...]] = []

        def listener(event: AgentSessionEvent) -> None:
            if isinstance(event, AgentSessionEvent.AgentSettled):
                settled_histories.append(
                    tuple(message.role for message in session.messages)
                )

        session.subscribe(listener)
        prompt = asyncio.create_task(session.prompt("cancel me"))
        await transport.first_started.wait()

        first_abort = asyncio.create_task(session.abort())
        second_abort = asyncio.create_task(session.abort())
        await asyncio.gather(first_abort, second_abort)
        await prompt

        assert transport.first_cancelled.is_set()
        assert len(transport.requests) == 1
        assert settled_histories == [("user", "assistant")]
        assert len(manager.getEntries()) == 4
        first_user, aborted = session.messages
        assert isinstance(first_user, UserMessage)
        assert isinstance(aborted, AssistantMessage)
        assert aborted.stopReason == "aborted"
        assert aborted.errorMessage == "Operation aborted"
        assert session.isIdle is True

        await session.prompt("reuse")
        assert len(transport.requests) == 2
        assert [message.role for message in session.messages] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert isinstance(session.messages[-1], AssistantMessage)
        assert session.messages[-1].stopReason == "stop"
        await session.dispose()

    asyncio.run(scenario())


def test_cancelled_abort_waiter_shields_the_captured_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    async def scenario() -> None:
        transport = _FirstRequestBarrier(hold_cancellation=True)
        _install_transport(monkeypatch, transport)
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    sessionManager=SessionManager.inMemory(os.fspath(tmp_path))
                )
            )
        ).session
        prompt = asyncio.create_task(session.prompt("cancel me"))
        await transport.first_started.wait()
        abort_returned = asyncio.Event()

        async def abort_waiter() -> None:
            try:
                await session.abort()
            finally:
                abort_returned.set()

        waiter = asyncio.create_task(abort_waiter())
        await transport.first_cancelled.wait()
        waiter.cancel()

        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        assert abort_returned.is_set() is False

        transport.release_cancellation.set()
        await prompt
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert session.isIdle is True
        await session.dispose()

    asyncio.run(scenario())


def test_cancelled_idle_waiter_cancels_and_settles_the_captured_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    async def scenario() -> None:
        transport = _FirstRequestBarrier(hold_cancellation=True)
        _install_transport(monkeypatch, transport)
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    sessionManager=SessionManager.inMemory(os.fspath(tmp_path))
                )
            )
        ).session
        prompt = asyncio.create_task(session.prompt("cancel me"))
        await transport.first_started.wait()
        waiter_returned = asyncio.Event()
        waiter_started = asyncio.Event()

        async def idle_waiter() -> None:
            try:
                waiter_started.set()
                await session.waitForIdle()
            finally:
                waiter_returned.set()

        waiter = asyncio.create_task(idle_waiter())
        await waiter_started.wait()
        waiter.cancel()

        cancellation_checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(cancellation_checkpoint.set)
        await cancellation_checkpoint.wait()
        assert waiter_returned.is_set() is False
        await transport.first_cancelled.wait()

        transport.release_cancellation.set()
        await prompt
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert isinstance(session.messages[-1], AssistantMessage)
        assert session.messages[-1].stopReason == "aborted"
        await session.dispose()

    asyncio.run(scenario())


def test_listener_failure_is_shared_without_fabricating_agent_settled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    async def scenario() -> None:
        transport = _FirstRequestBarrier()
        _install_transport(monkeypatch, transport)
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    sessionManager=SessionManager.inMemory(os.fspath(tmp_path))
                )
            )
        ).session
        listener_started = asyncio.Event()
        release_listener = asyncio.Event()
        event_types: list[str] = []

        async def listener(event: AgentSessionEvent) -> None:
            event_types.append(event.type)
            if isinstance(event, AgentSessionEvent.AgentStart):
                listener_started.set()
                await release_listener.wait()
                raise RuntimeError("listener broke")

        session.subscribe(listener)
        prompt = asyncio.create_task(session.prompt("fail"))
        await listener_started.wait()
        idle = asyncio.create_task(session.waitForIdle())
        release_listener.set()

        with pytest.raises(LifecycleError) as prompt_failure:
            await prompt
        with pytest.raises(LifecycleError) as idle_failure:
            await idle
        assert idle_failure.value is prompt_failure.value
        assert prompt_failure.value.code == "listener"
        assert "agent_settled" not in event_types
        assert len(transport.requests) == 0
        assert session.isIdle is True
        await session.dispose()

    asyncio.run(scenario())


def test_cancelled_disposal_waiter_does_not_abandon_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    async def scenario() -> None:
        transport = _FirstRequestBarrier(hold_cancellation=True)
        _install_transport(monkeypatch, transport)
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    sessionManager=SessionManager.inMemory(os.fspath(tmp_path))
                )
            )
        ).session
        unsubscribe = session.subscribe(lambda event: None)
        prompt = asyncio.create_task(session.prompt("dispose me"))
        await transport.first_started.wait()
        disposal = asyncio.create_task(session.dispose())
        await transport.first_cancelled.wait()

        with pytest.raises(LifecycleError) as closing_prompt:
            await session.prompt("too late")
        assert closing_prompt.value.code == "closing"
        with pytest.raises(LifecycleError) as closing_subscription:
            session.subscribe(lambda event: None)
        assert closing_subscription.value.code == "closing"

        disposal.cancel()
        disposal_returned = asyncio.Event()
        disposal.add_done_callback(lambda _: disposal_returned.set())
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        assert disposal_returned.is_set() is False

        transport.release_cancellation.set()
        await prompt
        with pytest.raises(asyncio.CancelledError):
            await disposal
        assert session.isIdle is True
        assert session.isStreaming is False
        unsubscribe()
        unsubscribe()
        with pytest.raises(LifecycleError) as disposed_prompt:
            await session.prompt("still too late")
        assert disposed_prompt.value.code == "disposed"
        await session.abort()
        await session.waitForIdle()
        await session.dispose()

    asyncio.run(scenario())


def test_listener_reentrant_session_barriers_fail_before_changing_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=_TEXT_SSE,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )

    transport = httpx.MockTransport(handle)
    _install_transport(monkeypatch, transport)

    async def scenario() -> None:
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    sessionManager=SessionManager.inMemory(os.fspath(tmp_path))
                )
            )
        ).session
        observed_codes: list[str] = []

        async def listener(event: AgentSessionEvent) -> None:
            if not isinstance(event, AgentSessionEvent.AgentStart):
                return
            for operation in (
                session.prompt("nested"),
                session.abort(),
                session.waitForIdle(),
                session.dispose(),
            ):
                with pytest.raises(LifecycleError) as failure:
                    await operation
                observed_codes.append(failure.value.code)

        session.subscribe(listener)
        await session.prompt("outer")
        assert observed_codes == ["reentrant"] * 4
        assert len(requests) == 1
        await session.dispose()

    asyncio.run(scenario())


def test_model_error_returns_normally_and_session_reuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                500,
                json={"error": {"message": "SECRET_PROVIDER_CANARY"}},
                request=request,
            )
        return httpx.Response(
            200,
            content=_TEXT_SSE,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )

    _install_transport(monkeypatch, httpx.MockTransport(handle))

    async def scenario() -> None:
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    sessionManager=SessionManager.inMemory(os.fspath(tmp_path))
                )
            )
        ).session
        await session.prompt("fail")
        failed = session.messages[-1]
        assert isinstance(failed, AssistantMessage)
        assert failed.stopReason == "error"
        assert failed.errorMessage == "DeepSeek request failed"
        assert "SECRET_PROVIDER_CANARY" not in repr(session.messages)

        await session.prompt("recover")
        recovered = session.messages[-1]
        assert isinstance(recovered, AssistantMessage)
        assert recovered.stopReason == "stop"
        assert len(requests) == 2
        await session.dispose()

    asyncio.run(scenario())


def test_prompt_task_cancellation_settles_history_before_reraising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    async def scenario() -> None:
        transport = _FirstRequestBarrier(hold_cancellation=True)
        _install_transport(monkeypatch, transport)
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    sessionManager=SessionManager.inMemory(os.fspath(tmp_path))
                )
            )
        ).session
        prompt = asyncio.create_task(session.prompt("cancel owner"))
        await transport.first_started.wait()
        prompt.cancel()
        await transport.first_cancelled.wait()

        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        assert prompt.done() is False

        transport.release_cancellation.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await prompt
        assert cancelled.value.__cause__ is None
        assert isinstance(session.messages[-1], AssistantMessage)
        assert session.messages[-1].stopReason == "aborted"
        assert session.isIdle is True

        await session.prompt("reuse")
        recovered = session.messages[-1]
        assert isinstance(recovered, AssistantMessage)
        assert recovered.stopReason == "stop"
        await session.dispose()

    asyncio.run(scenario())


def test_manager_append_failure_is_shared_raw_and_keeps_memory_first_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    async def scenario() -> None:
        transport = _SecondRequestBarrier()
        _install_transport(monkeypatch, transport)
        manager = SessionManager.create(
            os.fspath(tmp_path / "project"), os.fspath(tmp_path / "sessions")
        )
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        await session.prompt("durable")
        session_file = manager.getSessionFile()
        assert session_file is not None
        message_end_started = asyncio.Event()
        release_message_end = asyncio.Event()

        async def listener(event: AgentSessionEvent) -> None:
            if isinstance(event, AgentSessionEvent.MessageEnd) and isinstance(
                event.message, AssistantMessage
            ):
                message_end_started.set()
                await release_message_end.wait()

        session.subscribe(listener)
        write_bytes: Callable[[BinaryIO, bytes], None] = (
            session_manager_module._write_bytes
        )

        def fail_write(_output: BinaryIO, _data: bytes) -> None:
            raise OSError("session append cutoff")

        prompt = asyncio.create_task(session.prompt("persist"))
        await transport.second_started.wait()
        persisted_user_prefix = Path(session_file).read_bytes()
        monkeypatch.setattr(session_manager_module, "_write_bytes", fail_write)
        idle = asyncio.create_task(session.waitForIdle())
        transport.release_second.set()
        release_message_end.set()

        with pytest.raises(OSError, match="session append cutoff") as prompt_failure:
            await prompt
        with pytest.raises(OSError, match="session append cutoff") as idle_failure:
            await idle
        assert idle_failure.value is prompt_failure.value
        assert message_end_started.is_set() is False
        assert [entry.type for entry in manager.getEntries()] == [
            "model_change",
            "thinking_level_change",
            "message",
            "message",
            "message",
            "message",
        ]
        assert [message.role for message in session.messages] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert Path(session_file).read_bytes() == persisted_user_prefix

        monkeypatch.setattr(session_manager_module, "_write_bytes", write_bytes)
        await session.prompt("reuse")
        assert len(transport.requests) == 3
        assert session.messages[-1].role == "assistant"
        await session.dispose()

    asyncio.run(scenario())


def test_compaction_entry_preserves_tree_and_rebuilds_active_context(
    tmp_path: Path,
) -> None:
    manager = SessionManager.create(
        os.fspath(tmp_path / "project"), os.fspath(tmp_path / "sessions")
    )
    manager.appendMessage(UserMessage(content="old", timestamp=1))
    manager.appendMessage(fauxAssistantMessage("old answer"))
    kept_id = manager.appendMessage(UserMessage(content="kept", timestamp=2))
    manager.appendMessage(fauxAssistantMessage("kept answer"))
    before = manager.getEntries()
    manager.appendCompaction("checkpoint", kept_id, 50_000)

    context = manager.buildSessionContext().messages
    assert len(manager.getEntries()) == len(before) + 1
    assert [message.role for message in context] == [
        "user",
        "user",
        "assistant",
    ]
    assert isinstance(context[0], UserMessage)
    assert context[0].content == (
        "The conversation history before this point was compacted into the "
        "following summary:\n<summary>\ncheckpoint\n</summary>"
    )
    assert isinstance(context[1], UserMessage)
    assert context[1].content == "kept"

    session_file = manager.getSessionFile()
    assert session_file is not None
    reopened = SessionManager.open(session_file)
    assert reopened.getEntries() == manager.getEntries()
    assert reopened.buildSessionContext() == manager.buildSessionContext()


def test_manual_compaction_summarizes_prefix_and_rebuilds_agent_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    requests: list[httpx.Request] = []
    summary_sse = _TEXT_SSE.replace(b"Done", b"checkpoint")

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=summary_sse,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )

    _install_transport(monkeypatch, httpx.MockTransport(handle))

    async def scenario() -> None:
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old request", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        kept_id = manager.appendMessage(
            UserMessage(content="x" * 80_000, timestamp=2)
        )
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        entries_before = manager.getEntries()
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)

        result = await session.compact("preserve exact decisions")

        assert result.summary == "checkpoint"
        assert result.firstKeptEntryId == kept_id
        assert result.tokensBefore == 20_010
        assert result.details == {"readFiles": [], "modifiedFiles": []}
        assert len(manager.getEntries()) == len(entries_before) + 1
        assert [event.type for event in events] == [
            "compaction_start",
            "compaction_end",
        ]
        end = events[-1]
        assert isinstance(end, AgentSessionEvent.CompactionEnd)
        assert end.reason == "manual"
        assert end.result is result
        assert end.aborted is False
        assert end.willRetry is False
        assert end.errorMessage is None
        assert session.isCompacting is False
        assert session.messages == manager.buildSessionContext().messages
        assert isinstance(session.messages[0], UserMessage)
        assert "checkpoint" in str(session.messages[0].content)
        kept = session.messages[1]
        assert isinstance(kept, UserMessage)
        assert kept.content == "x" * 80_000

        assert len(requests) == 1
        body = json.loads(requests[0].content)
        assert body["max_completion_tokens"] == 13_107
        assert body["messages"][0]["role"] == "system"
        assert "context summarization assistant" in body["messages"][0]["content"]
        summary_prompt = body["messages"][1]["content"]
        assert "[User]: old request" in summary_prompt
        assert "Additional focus: preserve exact decisions" in summary_prompt
        assert "x" * 100 not in summary_prompt

        with pytest.raises(ValueError, match="too small to compact"):
            await session.compact()
        assert len(requests) == 1
        await session.dispose()

    asyncio.run(scenario())


def test_aborted_compaction_appends_no_result_and_idle_waits_for_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    async def scenario() -> None:
        transport = _FirstRequestBarrier(hold_cancellation=True)
        _install_transport(monkeypatch, transport)
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)
        before = manager.getEntries()

        compaction = asyncio.create_task(session.compact())
        await transport.first_started.wait()
        assert session.isCompacting is True
        assert session.isIdle is False
        idle = asyncio.create_task(session.waitForIdle())
        session.abortCompaction()
        await transport.first_cancelled.wait()

        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        assert compaction.done() is False
        assert idle.done() is False

        transport.release_cancellation.set()
        with pytest.raises(asyncio.CancelledError):
            await compaction
        await idle
        assert manager.getEntries() == before
        assert [event.type for event in events] == [
            "compaction_start",
            "compaction_end",
        ]
        end = events[-1]
        assert isinstance(end, AgentSessionEvent.CompactionEnd)
        assert end.result is None
        assert end.aborted is True
        assert end.willRetry is False
        assert end.errorMessage is None
        assert session.isCompacting is False
        assert session.isIdle is True
        await session.dispose()

    asyncio.run(scenario())


def test_split_turn_compaction_summarizes_prefix_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    requests: list[httpx.Request] = []
    prefix_sse = _TEXT_SSE.replace(b"Done", b"prefix checkpoint")

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=prefix_sse,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )

    _install_transport(monkeypatch, httpx.MockTransport(handle))

    async def scenario() -> None:
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="large turn", timestamp=1))
        kept_id = manager.appendMessage(fauxAssistantMessage("x" * 80_000))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session

        result = await session.compact()

        assert result.firstKeptEntryId == kept_id
        assert result.summary == (
            "No prior history.\n\n---\n\n"
            "**Turn Context (split turn):**\n\n"
            "prefix checkpoint"
        )
        assert len(requests) == 1
        body = json.loads(requests[0].content)
        assert body["max_completion_tokens"] == 8_192
        assert "[User]: large turn" in body["messages"][1]["content"]
        assert "PREFIX of a turn" in body["messages"][1]["content"]
        await session.dispose()

    asyncio.run(scenario())


def test_threshold_compaction_runs_before_agent_settled_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    requests: list[httpx.Request] = []
    summary = _TEXT_SSE.replace(b"Done", b"threshold checkpoint")

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=_HIGH_USAGE_SSE if len(requests) == 1 else summary,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )

    _install_transport(monkeypatch, httpx.MockTransport(handle))

    async def scenario() -> None:
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)

        await session.prompt("trigger threshold")

        assert len(requests) == 2
        compaction_events = [
            event for event in events if event.type.startswith("compaction_")
        ]
        assert [event.type for event in compaction_events] == [
            "compaction_start",
            "compaction_end",
        ]
        start, end = compaction_events
        assert isinstance(start, AgentSessionEvent.CompactionStart)
        assert start.reason == "threshold"
        assert isinstance(end, AgentSessionEvent.CompactionEnd)
        assert end.reason == "threshold"
        assert end.result is not None
        assert end.result.summary == "threshold checkpoint"
        assert end.willRetry is False
        event_types = [event.type for event in events]
        assert event_types.index("compaction_start") < event_types.index(
            "compaction_end"
        )
        assert event_types.index("compaction_end") < event_types.index("agent_end")
        assert events[-1].type == "agent_settled"
        assert sum(event.type == "agent_start" for event in events) == 1
        assert session.messages == manager.buildSessionContext().messages
        await session.dispose()

    asyncio.run(scenario())


def test_over_window_length_compacts_as_threshold_without_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    requests: list[httpx.Request] = []
    summary = _TEXT_SSE.replace(b"Done", b"length checkpoint")

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=_HIGH_LENGTH_SSE if len(requests) == 1 else summary,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )

    _install_transport(monkeypatch, httpx.MockTransport(handle))

    async def scenario() -> None:
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)

        await session.prompt("length is not overflow")

        assert len(requests) == 2
        start = next(
            event
            for event in events
            if isinstance(event, AgentSessionEvent.CompactionStart)
        )
        assert start.reason == "threshold"
        agent_ends = [
            event for event in events if isinstance(event, AgentSessionEvent.AgentEnd)
        ]
        assert [event.willRetry for event in agent_ends] == [False]
        assert events[-1].type == "agent_settled"
        await session.dispose()

    asyncio.run(scenario())


def test_overflow_compaction_continues_same_prompt_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    requests: list[httpx.Request] = []
    summary = _TEXT_SSE.replace(b"Done", b"overflow checkpoint")

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "context_length_exceeded",
                        "message": "SECRET_CONTEXT_LIMIT_CANARY",
                    }
                },
                request=request,
            )
        return httpx.Response(
            200,
            content=summary if len(requests) == 2 else _TEXT_SSE,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )

    _install_transport(monkeypatch, httpx.MockTransport(handle))

    async def scenario() -> None:
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)

        await session.prompt("overflow once")

        assert len(requests) == 3
        assert sum(event.type == "agent_start" for event in events) == 2
        starts = [
            event
            for event in events
            if isinstance(event, AgentSessionEvent.CompactionStart)
        ]
        ends = [
            event
            for event in events
            if isinstance(event, AgentSessionEvent.CompactionEnd)
        ]
        assert [event.reason for event in starts] == ["overflow"]
        assert len(ends) == 1
        assert ends[0].result is not None
        assert ends[0].result.summary == "overflow checkpoint"
        assert ends[0].willRetry is True
        agent_ends = [
            event
            for event in events
            if isinstance(event, AgentSessionEvent.AgentEnd)
        ]
        assert [event.willRetry for event in agent_ends] == [True, False]
        event_types = [event.type for event in events]
        assert event_types.index("compaction_start") < event_types.index(
            "compaction_end"
        )
        assert event_types.index("compaction_end") < event_types.index("agent_end")
        assert event_types.index("agent_end") < event_types.index("agent_start", 1)
        assert events[-1].type == "agent_settled"

        full_assistants = [
            entry.message
            for entry in manager.getEntries()
            if isinstance(entry, SessionMessageEntry)
            and isinstance(entry.message, AssistantMessage)
        ]
        assert any(message.stopReason == "error" for message in full_assistants)
        assert "SECRET_CONTEXT_LIMIT_CANARY" not in repr(full_assistants)
        assert all(
            not (
                isinstance(message, AssistantMessage)
                and message.stopReason == "error"
            )
            for message in session.messages
        )
        assert isinstance(session.messages[-1], AssistantMessage)
        assert session.messages[-1].stopReason == "stop"
        await session.dispose()

    asyncio.run(scenario())


def test_dispose_cancels_and_drains_active_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    async def scenario() -> None:
        transport = _FirstRequestBarrier(hold_cancellation=True)
        _install_transport(monkeypatch, transport)
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        before = manager.getEntries()
        compaction = asyncio.create_task(session.compact())
        await transport.first_started.wait()

        disposal = asyncio.create_task(session.dispose())
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        drive_checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(drive_checkpoint.set)
        await drive_checkpoint.wait()
        disposal_was_done = disposal.done()
        disposal_requested_cancel = transport.first_cancelled.is_set()

        if not transport.first_cancelled.is_set():
            session.abortCompaction()
            await transport.first_cancelled.wait()
        transport.release_cancellation.set()
        with pytest.raises(asyncio.CancelledError):
            await compaction
        await disposal

        assert disposal_was_done is False
        assert disposal_requested_cancel is True
        assert manager.getEntries() == before
        assert session.isCompacting is False
        assert session.isIdle is True
        with pytest.raises(LifecycleError) as disposed:
            await session.prompt("closed")
        assert disposed.value.code == "disposed"

    asyncio.run(scenario())


def test_abort_during_session_listener_finishes_snapshot_then_cancels_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=_TEXT_SSE,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )

    _install_transport(monkeypatch, httpx.MockTransport(handle))

    async def scenario() -> None:
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    sessionManager=SessionManager.inMemory(os.fspath(tmp_path))
                )
            )
        ).session
        first_started = asyncio.Event()
        first_cancelled = asyncio.Event()
        snapshot: list[str] = []

        async def first(event: AgentSessionEvent) -> None:
            if isinstance(event, AgentSessionEvent.AgentStart):
                snapshot.append("first")
                first_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    first_cancelled.set()
                    raise

        def second(event: AgentSessionEvent) -> None:
            if isinstance(event, AgentSessionEvent.AgentStart):
                snapshot.append("second")

        session.subscribe(first)
        session.subscribe(second)
        prompt = asyncio.create_task(session.prompt("listener cutoff"))
        await first_started.wait()
        abort = asyncio.create_task(session.abort())
        await asyncio.gather(prompt, abort)

        assert first_cancelled.is_set()
        assert snapshot == ["first", "second"]
        assert requests == []
        assert isinstance(session.messages[-1], AssistantMessage)
        assert session.messages[-1].stopReason == "aborted"
        assert session.isIdle is True
        await session.dispose()

    asyncio.run(scenario())


def test_listener_owner_task_cancellation_aborts_before_model_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=_TEXT_SSE, request=request)

    _install_transport(
        monkeypatch,
        httpx.MockTransport(handle),
    )

    async def scenario() -> None:
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    sessionManager=SessionManager.inMemory(os.fspath(tmp_path))
                )
            )
        ).session
        snapshot: list[str] = []

        async def cancelling(event: AgentSessionEvent) -> None:
            if isinstance(event, AgentSessionEvent.AgentStart):
                snapshot.append("cancelling")
                owner = asyncio.current_task()
                assert owner is not None
                owner.cancel()
                await asyncio.Event().wait()

        def later(event: AgentSessionEvent) -> None:
            if isinstance(event, AgentSessionEvent.AgentStart):
                snapshot.append("later")

        session.subscribe(cancelling)
        session.subscribe(later)
        await session.prompt("cancel from listener")

        assert snapshot == ["cancelling", "later"]
        assert requests == []
        assert isinstance(session.messages[-1], AssistantMessage)
        assert session.messages[-1].stopReason == "aborted"
        await session.dispose()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


def test_large_trailing_tool_result_reports_uncompactable_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    async def scenario() -> None:
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="one turn", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("tool call completed"))
        manager.appendMessage(
            ToolResultMessage(
                toolCallId="call",
                toolName="read",
                content=(fauxText("x" * 80_004),),
                details={},
                isError=False,
                timestamp=2,
            )
        )
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session

        with pytest.raises(ValueError, match="too small to compact"):
            await session.compact()
        await session.dispose()

    asyncio.run(scenario())


def test_failed_overflow_compaction_reports_no_agent_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                400,
                json={"error": {"code": "context_length_exceeded"}},
                request=request,
            )
        return httpx.Response(500, json={"error": "summary failed"}, request=request)

    _install_transport(monkeypatch, httpx.MockTransport(handle))

    async def scenario() -> None:
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)

        await session.prompt("overflow with failed summary")

        agent_ends = [
            event for event in events if isinstance(event, AgentSessionEvent.AgentEnd)
        ]
        compaction_ends = [
            event
            for event in events
            if isinstance(event, AgentSessionEvent.CompactionEnd)
        ]
        assert [event.willRetry for event in agent_ends] == [False]
        assert len(compaction_ends) == 1
        assert compaction_ends[0].result is None
        assert compaction_ends[0].willRetry is False
        assert events[-1].type == "agent_settled"
        assert all(entry.type != "compaction" for entry in manager.getEntries())
        await session.dispose()

    asyncio.run(scenario())


def test_recovered_oversized_context_compacts_before_new_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    requests: list[httpx.Request] = []
    summary = _TEXT_SSE.replace(b"Done", b"pre-prompt checkpoint")

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=summary if len(requests) == 1 else _TEXT_SSE,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )

    _install_transport(monkeypatch, httpx.MockTransport(handle))

    async def scenario() -> None:
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="x" * 400_000, timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="y" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)

        await session.prompt("new work")

        assert len(requests) == 2
        first_body = json.loads(requests[0].content)
        second_body = json.loads(requests[1].content)
        assert "context summarization assistant" in first_body["messages"][0]["content"]
        assert second_body["messages"][-1]["content"] == "new work"
        assert [event.type for event in events[:2]] == [
            "compaction_start",
            "compaction_end",
        ]
        assert events[-1].type == "agent_settled"
        await session.dispose()

    asyncio.run(scenario())


def test_disposal_cleanup_failure_retries_and_context_keeps_body_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    async def scenario() -> None:
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    sessionManager=SessionManager.inMemory(os.fspath(tmp_path))
                )
            )
        ).session
        original_unsubscribe = session._agent_unsubscribe
        assert original_unsubscribe is not None
        attempts = 0

        def flaky_unsubscribe() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("unsubscribe failed")
            original_unsubscribe()

        session._agent_unsubscribe = flaky_unsubscribe
        body_failure = ValueError("body failed")
        with pytest.raises(ValueError) as caught:
            async with session:
                raise body_failure

        assert caught.value is body_failure
        disposal_failure = caught.value.__context__
        assert isinstance(disposal_failure, LifecycleError)
        assert disposal_failure.code == "disposal"
        assert isinstance(disposal_failure.causes[0], RuntimeError)
        assert session.isIdle is False
        with pytest.raises(LifecycleError) as closing:
            await session.prompt("closed")
        assert closing.value.code == "closing"

        await session.dispose()
        assert attempts == 2
        assert session.isIdle is True

    asyncio.run(scenario())


def test_second_overflow_settles_without_another_compaction_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    requests: list[httpx.Request] = []
    summary = _TEXT_SSE.replace(b"Done", b"retry checkpoint")

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) in (1, 3):
            return httpx.Response(
                400,
                json={"error": {"code": "context_length_exceeded"}},
                request=request,
            )
        return httpx.Response(
            200,
            content=summary,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )

    _install_transport(monkeypatch, httpx.MockTransport(handle))

    async def scenario() -> None:
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)

        await session.prompt("overflow twice")

        assert len(requests) == 3
        agent_ends = [
            event for event in events if isinstance(event, AgentSessionEvent.AgentEnd)
        ]
        compaction_ends = [
            event
            for event in events
            if isinstance(event, AgentSessionEvent.CompactionEnd)
        ]
        assert [event.willRetry for event in agent_ends] == [True, False]
        assert [event.willRetry for event in compaction_ends] == [True, False]
        assert compaction_ends[1].result is None
        assert "recovery failed" in (compaction_ends[1].errorMessage or "")
        assert events[-1].type == "agent_settled"
        await session.dispose()

    asyncio.run(scenario())


def test_automatic_compaction_cancellation_appends_no_result_and_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    class Transport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.requests: list[httpx.Request] = []
            self.compaction_started = asyncio.Event()
            self.compaction_cancelled = asyncio.Event()

        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            self.requests.append(request)
            if len(self.requests) == 1:
                return httpx.Response(
                    200,
                    content=_HIGH_USAGE_SSE,
                    headers={"Content-Type": "text/event-stream"},
                    request=request,
                )
            self.compaction_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.compaction_cancelled.set()
                raise
            raise AssertionError("unreachable compaction barrier release")

    async def scenario() -> None:
        transport = Transport()
        _install_transport(monkeypatch, transport)
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        before = manager.getEntries()
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)

        prompt = asyncio.create_task(session.prompt("cancel automatic compaction"))
        await transport.compaction_started.wait()
        session.abortCompaction()
        await prompt

        assert transport.compaction_cancelled.is_set()
        assert all(entry.type != "compaction" for entry in manager.getEntries())
        assert len(manager.getEntries()) > len(before)
        end = next(
            event
            for event in events
            if isinstance(event, AgentSessionEvent.CompactionEnd)
        )
        assert end.aborted is True
        assert end.result is None
        agent_end = next(
            event
            for event in events
            if isinstance(event, AgentSessionEvent.AgentEnd)
        )
        assert agent_end.willRetry is False
        assert events[-1].type == "agent_settled"
        await session.dispose()

    asyncio.run(scenario())


def test_overflow_compaction_abort_settles_normally_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    class Transport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.requests: list[httpx.Request] = []
            self.compaction_started = asyncio.Event()
            self.compaction_cancelled = asyncio.Event()

        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            self.requests.append(request)
            if len(self.requests) == 1:
                return httpx.Response(
                    400,
                    json={"error": {"code": "context_length_exceeded"}},
                    request=request,
                )
            self.compaction_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.compaction_cancelled.set()
                raise
            raise AssertionError("unreachable overflow summary release")

    async def scenario() -> None:
        transport = Transport()
        _install_transport(monkeypatch, transport)
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)

        prompt = asyncio.create_task(session.prompt("abort overflow recovery"))
        await transport.compaction_started.wait()
        session.abortCompaction()
        await transport.compaction_cancelled.wait()
        await prompt

        assert [event.type for event in events[-3:]] == [
            "compaction_end",
            "agent_end",
            "agent_settled",
        ]
        end = events[-3]
        assert isinstance(end, AgentSessionEvent.CompactionEnd)
        assert end.aborted is True
        assert end.result is None
        agent_end = events[-2]
        assert isinstance(agent_end, AgentSessionEvent.AgentEnd)
        assert agent_end.willRetry is False
        assert all(entry.type != "compaction" for entry in manager.getEntries())
        assert session.isIdle is True
        await session.dispose()

    asyncio.run(scenario())


def test_automatic_compaction_start_listener_precedes_effect_and_can_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=_HIGH_USAGE_SSE,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )

    _install_transport(monkeypatch, httpx.MockTransport(handle))

    async def scenario() -> None:
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        compacting_observations: list[bool] = []
        events: list[AgentSessionEvent] = []

        async def listener(event: AgentSessionEvent) -> None:
            events.append(event)
            if isinstance(event, AgentSessionEvent.CompactionStart):
                compacting_observations.append(session.isCompacting)
                session.abortCompaction()
                checkpoint = asyncio.Event()
                asyncio.get_running_loop().call_soon(checkpoint.set)
                await checkpoint.wait()

        session.subscribe(listener)
        await session.prompt("cancel from compaction start")

        assert compacting_observations == [True]
        assert len(requests) == 1
        assert all(entry.type != "compaction" for entry in manager.getEntries())
        end = next(
            event
            for event in events
            if isinstance(event, AgentSessionEvent.CompactionEnd)
        )
        assert end.aborted is True
        assert end.result is None
        await session.dispose()

    asyncio.run(scenario())


def test_prompt_cancellation_during_automatic_compaction_settles_before_reraise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    class Transport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.requests: list[httpx.Request] = []
            self.compaction_started = asyncio.Event()
            self.compaction_cancelled = asyncio.Event()
            self.release_cancellation = asyncio.Event()

        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            self.requests.append(request)
            if len(self.requests) == 1:
                return httpx.Response(
                    200,
                    content=_HIGH_USAGE_SSE,
                    headers={"Content-Type": "text/event-stream"},
                    request=request,
                )
            if len(self.requests) == 2:
                self.compaction_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.compaction_cancelled.set()
                    await self.release_cancellation.wait()
                    raise
            return httpx.Response(
                200,
                content=_TEXT_SSE,
                headers={"Content-Type": "text/event-stream"},
                request=request,
            )

    async def scenario() -> None:
        transport = Transport()
        _install_transport(monkeypatch, transport)
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)

        prompt = asyncio.create_task(session.prompt("cancel automatic owner"))
        await transport.compaction_started.wait()
        prompt.cancel()
        await transport.compaction_cancelled.wait()

        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        assert prompt.done() is False

        transport.release_cancellation.set()
        with pytest.raises(asyncio.CancelledError):
            await prompt

        assert [event.type for event in events[-3:]] == [
            "compaction_end",
            "agent_end",
            "agent_settled",
        ]
        assert session.isIdle is True
        await session.prompt("reuse after automatic owner cancellation")
        assert isinstance(session.messages[-1], AssistantMessage)
        assert session.messages[-1].stopReason == "stop"
        await session.dispose()

    asyncio.run(scenario())


def test_wait_for_idle_during_automatic_compaction_captures_prompt_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    summary = _TEXT_SSE.replace(b"Done", b"idle checkpoint")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=_HIGH_USAGE_SSE if len(requests) == 1 else summary,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )

    _install_transport(monkeypatch, httpx.MockTransport(handle))

    async def scenario() -> None:
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        compaction_started = asyncio.Event()
        release_compaction_start = asyncio.Event()
        agent_end_started = asyncio.Event()
        release_agent_end = asyncio.Event()

        async def listener(event: AgentSessionEvent) -> None:
            if isinstance(event, AgentSessionEvent.CompactionStart):
                compaction_started.set()
                await release_compaction_start.wait()
            if isinstance(event, AgentSessionEvent.AgentEnd):
                agent_end_started.set()
                await release_agent_end.wait()

        session.subscribe(listener)
        prompt = asyncio.create_task(session.prompt("wait through automatic"))
        await compaction_started.wait()
        idle = asyncio.create_task(session.waitForIdle())
        release_compaction_start.set()
        await agent_end_started.wait()

        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        assert idle.done() is False
        assert prompt.done() is False
        assert session.isIdle is False

        release_agent_end.set()
        await asyncio.gather(prompt, idle)
        assert session.isIdle is True
        await session.dispose()

    asyncio.run(scenario())


def test_abort_during_automatic_compaction_cancels_shared_prompt_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    class Transport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.requests: list[httpx.Request] = []
            self.compaction_started = asyncio.Event()
            self.compaction_cancelled = asyncio.Event()

        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            self.requests.append(request)
            if len(self.requests) == 1:
                return httpx.Response(
                    200,
                    content=_HIGH_USAGE_SSE,
                    headers={"Content-Type": "text/event-stream"},
                    request=request,
                )
            self.compaction_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.compaction_cancelled.set()
                raise
            raise AssertionError("unreachable automatic compaction release")

    async def scenario() -> None:
        transport = Transport()
        _install_transport(monkeypatch, transport)
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session

        prompt = asyncio.create_task(session.prompt("abort automatic work"))
        await transport.compaction_started.wait()
        abort = asyncio.create_task(session.abort())
        await transport.compaction_cancelled.wait()
        await asyncio.gather(prompt, abort)

        assert all(entry.type != "compaction" for entry in manager.getEntries())
        assert session.isIdle is True
        await session.dispose()

    asyncio.run(scenario())


def test_automatic_compaction_listener_failure_truncates_and_reuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=_HIGH_USAGE_SSE if len(requests) == 1 else _TEXT_SSE,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )

    _install_transport(monkeypatch, httpx.MockTransport(handle))

    async def scenario() -> None:
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        event_types: list[str] = []

        def listener(event: AgentSessionEvent) -> None:
            event_types.append(event.type)
            if isinstance(event, AgentSessionEvent.CompactionStart):
                raise RuntimeError("automatic start listener failed")

        unsubscribe = session.subscribe(listener)
        with pytest.raises(LifecycleError) as failure:
            await session.prompt("fail automatic listener")

        assert failure.value.code == "listener"
        assert event_types[-1] == "compaction_start"
        assert "agent_end" not in event_types
        assert "agent_settled" not in event_types
        assert len(requests) == 1

        unsubscribe()
        await session.prompt("reuse after automatic listener failure")
        assert len(requests) == 3
        assert isinstance(session.messages[-1], AssistantMessage)
        assert session.messages[-1].stopReason == "stop"
        await session.dispose()

    asyncio.run(scenario())


def test_summary_failure_compaction_end_listener_failure_is_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                content=_HIGH_USAGE_SSE,
                headers={"Content-Type": "text/event-stream"},
                request=request,
            )
        if len(requests) == 2:
            return httpx.Response(500, json={"error": "summary failed"}, request=request)
        return httpx.Response(
            200,
            content=_TEXT_SSE,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )

    _install_transport(monkeypatch, httpx.MockTransport(handle))

    async def scenario() -> None:
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        event_types: list[str] = []

        def listener(event: AgentSessionEvent) -> None:
            event_types.append(event.type)
            if isinstance(event, AgentSessionEvent.CompactionEnd):
                raise RuntimeError("summary end listener failed")

        unsubscribe = session.subscribe(listener)
        with pytest.raises(LifecycleError) as failure:
            await session.prompt("fail summary and listener")

        assert failure.value.code == "listener"
        assert isinstance(failure.value.__cause__, ModelsError)
        assert event_types[-1] == "compaction_end"
        assert "agent_end" not in event_types
        assert "agent_settled" not in event_types

        unsubscribe()
        await session.prompt("reuse after summary listener failure")
        assert len(requests) == 4
        assert isinstance(session.messages[-1], AssistantMessage)
        assert session.messages[-1].stopReason == "stop"
        await session.dispose()

    asyncio.run(scenario())


def test_automatic_compaction_listener_owner_cancellation_reraises_after_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=_HIGH_USAGE_SSE if len(requests) == 1 else _TEXT_SSE,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )

    _install_transport(monkeypatch, httpx.MockTransport(handle))

    async def scenario() -> None:
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        events: list[AgentSessionEvent] = []

        async def listener(event: AgentSessionEvent) -> None:
            events.append(event)
            if isinstance(event, AgentSessionEvent.CompactionStart):
                owner = asyncio.current_task()
                assert owner is not None
                owner.cancel()
                await asyncio.Event().wait()

        unsubscribe = session.subscribe(listener)
        with pytest.raises(asyncio.CancelledError):
            await session.prompt("cancel compaction callback owner")

        assert [event.type for event in events[-3:]] == [
            "compaction_end",
            "agent_end",
            "agent_settled",
        ]
        end = events[-3]
        assert isinstance(end, AgentSessionEvent.CompactionEnd)
        assert end.aborted is True
        assert len(requests) == 1
        assert session.isIdle is True

        unsubscribe()
        await session.prompt("reuse after callback owner cancellation")
        assert len(requests) == 3
        assert isinstance(session.messages[-1], AssistantMessage)
        assert session.messages[-1].stopReason == "stop"
        await session.dispose()

    asyncio.run(scenario())


def test_automatic_compaction_append_failure_propagates_raw_and_memory_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    summary = _TEXT_SSE.replace(b"Done", b"durable checkpoint")
    _install_transport(
        monkeypatch,
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=summary,
                headers={"Content-Type": "text/event-stream"},
                request=request,
            )
        ),
    )

    async def scenario() -> None:
        manager = SessionManager.create(
            os.fspath(tmp_path / "project"), os.fspath(tmp_path / "sessions")
        )
        manager.appendMessage(UserMessage(content="x" * 400_000, timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="y" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)
        write_bytes = session_manager_module._write_bytes
        append_failure = OSError("compaction append cutoff")

        def fail_append(_output: BinaryIO, _data: bytes) -> None:
            raise append_failure

        monkeypatch.setattr(session_manager_module, "_write_bytes", fail_append)
        with pytest.raises(OSError) as caught:
            await session.prompt("must not reach provider prompt")

        assert caught.value is append_failure
        assert manager.getEntries()[-1].type == "compaction"
        assert [event.type for event in events] == [
            "compaction_start",
            "compaction_end",
        ]
        end = events[-1]
        assert isinstance(end, AgentSessionEvent.CompactionEnd)
        assert end.result is None
        assert end.willRetry is False
        monkeypatch.setattr(session_manager_module, "_write_bytes", write_bytes)

        assert session.messages == manager.buildSessionContext().messages
        await session.prompt("reuse after pre-prompt append failure")
        assert session.messages == manager.buildSessionContext().messages
        assert isinstance(session.messages[-1], AssistantMessage)
        assert session.messages[-1].stopReason == "stop"
        await session.dispose()

    asyncio.run(scenario())


def test_post_run_compaction_append_failure_rebuilds_context_and_reuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    requests: list[httpx.Request] = []
    summary = _TEXT_SSE.replace(b"Done", b"memory-first checkpoint")
    write_bytes = session_manager_module._write_bytes
    append_failure = OSError("post-run compaction append cutoff")

    def fail_append(_output: BinaryIO, _data: bytes) -> None:
        raise append_failure

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 2:
            monkeypatch.setattr(session_manager_module, "_write_bytes", fail_append)
        if len(requests) == 1:
            content = _HIGH_USAGE_SSE
        elif len(requests) == 2:
            content = summary
        else:
            content = _TEXT_SSE
        return httpx.Response(
            200,
            content=content,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )

    _install_transport(monkeypatch, httpx.MockTransport(handle))

    async def scenario() -> None:
        manager = SessionManager.create(
            os.fspath(tmp_path / "project"), os.fspath(tmp_path / "sessions")
        )
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session

        with pytest.raises(OSError) as caught:
            await session.prompt("fail post-run compaction append")

        assert caught.value is append_failure
        assert manager.getEntries()[-1].type == "compaction"
        assert session.messages == manager.buildSessionContext().messages
        monkeypatch.setattr(session_manager_module, "_write_bytes", write_bytes)

        await session.prompt("reuse after raw append failure")
        assert isinstance(session.messages[-1], AssistantMessage)
        assert session.messages[-1].stopReason == "stop"
        assert len(requests) == 3
        await session.dispose()

    asyncio.run(scenario())


def test_disposal_during_pre_prompt_compaction_settles_prompt_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    async def scenario() -> None:
        transport = _FirstRequestBarrier()
        _install_transport(monkeypatch, transport)
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="x" * 400_000, timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="y" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session

        prompt = asyncio.create_task(session.prompt("must not start"))
        await transport.first_started.wait()
        disposal = asyncio.create_task(session.dispose())
        await transport.first_cancelled.wait()
        await asyncio.gather(prompt, disposal)

        assert len(transport.requests) == 1
        assert all(entry.type != "compaction" for entry in manager.getEntries())
        assert session.isIdle is True
        assert session.isStreaming is False

    asyncio.run(scenario())


def test_abort_during_pre_prompt_compaction_settles_without_model_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    async def scenario() -> None:
        transport = _FirstRequestBarrier()
        _install_transport(monkeypatch, transport)
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="x" * 400_000, timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="y" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        before = manager.getEntries()
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session

        prompt = asyncio.create_task(session.prompt("must not start after abort"))
        await transport.first_started.wait()
        abort = asyncio.create_task(session.abort())
        await transport.first_cancelled.wait()
        await asyncio.gather(prompt, abort)

        assert len(transport.requests) == 1
        assert manager.getEntries() == before
        assert session.isIdle is True
        assert session.isStreaming is False
        await session.dispose()

    asyncio.run(scenario())


def test_disposal_reports_compaction_listener_failure_then_retries_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    async def scenario() -> None:
        transport = _FirstRequestBarrier()
        _install_transport(monkeypatch, transport)
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session

        def fail_end(event: AgentSessionEvent) -> None:
            if isinstance(event, AgentSessionEvent.CompactionEnd):
                raise RuntimeError("compaction end listener failed")

        unsubscribe = session.subscribe(fail_end)
        compaction = asyncio.create_task(session.compact())
        await transport.first_started.wait()

        with pytest.raises(LifecycleError) as disposal:
            await session.dispose()
        with pytest.raises(LifecycleError) as compact_failure:
            await compaction

        assert disposal.value.code == "disposal"
        assert isinstance(disposal.value.causes[0], LifecycleError)
        assert compact_failure.value.code == "listener"
        assert session.isIdle is False
        unsubscribe()
        await session.dispose()
        assert session.isIdle is True

    asyncio.run(scenario())


def test_abort_after_compaction_result_commit_cannot_rewrite_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    summary = _TEXT_SSE.replace(b"Done", b"committed checkpoint")
    _install_transport(
        monkeypatch,
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=summary,
                headers={"Content-Type": "text/event-stream"},
                request=request,
            )
        ),
    )

    async def scenario() -> None:
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        end_started = asyncio.Event()
        release_end = asyncio.Event()

        async def listener(event: AgentSessionEvent) -> None:
            if (
                isinstance(event, AgentSessionEvent.CompactionEnd)
                and event.result is not None
            ):
                end_started.set()
                await release_end.wait()

        session.subscribe(listener)
        compaction = asyncio.create_task(session.compact())
        await end_started.wait()

        assert manager.getEntries()[-1].type == "compaction"
        assert session.isCompacting is True
        session.abortCompaction()
        release_end.set()

        result = await compaction
        assert result.summary == "committed checkpoint"
        assert session.messages == manager.buildSessionContext().messages
        await session.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("trigger", "expected_will_retry"),
    [("overflow", True), ("threshold", False)],
)
def test_prompt_cancellation_after_automatic_compaction_commit_truncates_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
    expected_will_retry: bool,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    requests: list[httpx.Request] = []
    summary = _TEXT_SSE.replace(b"Done", b"authorized checkpoint")

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1 and trigger == "overflow":
            return httpx.Response(
                400,
                json={"error": {"code": "context_length_exceeded"}},
                request=request,
            )
        return httpx.Response(
            200,
            content=_HIGH_USAGE_SSE if len(requests) == 1 else summary,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )

    _install_transport(monkeypatch, httpx.MockTransport(handle))

    async def scenario() -> None:
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        events: list[AgentSessionEvent] = []
        end_started = asyncio.Event()
        release_end = asyncio.Event()

        async def listener(event: AgentSessionEvent) -> None:
            events.append(event)
            if isinstance(event, AgentSessionEvent.CompactionEnd):
                end_started.set()
                await release_end.wait()

        session.subscribe(listener)
        prompt = asyncio.create_task(session.prompt("cancel committed recovery"))
        await end_started.wait()
        prompt.cancel()
        release_end.set()

        with pytest.raises(asyncio.CancelledError):
            await prompt

        end = next(
            event
            for event in events
            if isinstance(event, AgentSessionEvent.CompactionEnd)
        )
        assert end.result is not None
        assert end.willRetry is expected_will_retry
        assert "agent_end" not in [event.type for event in events]
        assert "agent_settled" not in [event.type for event in events]
        assert len(requests) == 2
        assert manager.getEntries()[-1].type == "compaction"
        assert session.isIdle is True
        await session.dispose()

    asyncio.run(scenario())


def test_session_abort_after_overflow_compaction_commit_blocks_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    requests: list[httpx.Request] = []
    summary = _TEXT_SSE.replace(b"Done", b"abort cutoff checkpoint")

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                400,
                json={"error": {"code": "context_length_exceeded"}},
                request=request,
            )
        if len(requests) == 2:
            return httpx.Response(
                200,
                content=summary,
                headers={"Content-Type": "text/event-stream"},
                request=request,
            )
        raise AssertionError("overflow continuation started after Session abort")

    _install_transport(monkeypatch, httpx.MockTransport(handle))

    async def scenario() -> None:
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(fauxAssistantMessage("recent answer"))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session
        events: list[AgentSessionEvent] = []
        end_started = asyncio.Event()
        release_end = asyncio.Event()

        async def listener(event: AgentSessionEvent) -> None:
            events.append(event)
            if isinstance(event, AgentSessionEvent.CompactionEnd):
                end_started.set()
                await release_end.wait()

        session.subscribe(listener)
        prompt = asyncio.create_task(session.prompt("abort committed recovery"))
        await end_started.wait()
        abort = asyncio.create_task(session.abort())
        release_end.set()
        await asyncio.gather(prompt, abort)

        assert len(requests) == 2
        assert "agent_end" not in [event.type for event in events]
        assert "agent_settled" not in [event.type for event in events]
        assert session.isIdle is True
        await session.dispose()

    asyncio.run(scenario())


def test_zero_total_usage_falls_back_to_usage_components_for_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    summary = _TEXT_SSE.replace(b"Done", b"usage checkpoint")
    _install_transport(
        monkeypatch,
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=summary,
                headers={"Content-Type": "text/event-stream"},
                request=request,
            )
        ),
    )

    async def scenario() -> None:
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        manager.appendMessage(UserMessage(content="old", timestamp=1))
        manager.appendMessage(fauxAssistantMessage("old answer"))
        manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
        manager.appendMessage(
            AssistantMessage(
                content=(fauxText("recent answer"),),
                api="faux",
                provider="faux",
                model="faux-1",
                usage=Usage(
                    input=30_000,
                    output=20_000,
                    cacheRead=10_000,
                    cacheWrite=5_000,
                    totalTokens=0,
                    cost=UsageCost(
                        input=0.0,
                        output=0.0,
                        cacheRead=0.0,
                        cacheWrite=0.0,
                        total=0.0,
                    ),
                ),
                stopReason="stop",
                timestamp=3,
            )
        )
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session

        result = await session.compact()

        assert result.tokensBefore == 65_000
        await session.dispose()

    asyncio.run(scenario())


def test_ticket_15_obligations_link_corpus_and_executable_evidence() -> None:
    matrix = json.loads((ROOT / "conformance/obligation-matrix.json").read_text())
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    obligations = {row["id"]: row for row in matrix["obligations"]}
    cases = {case["id"]: case for case in corpus["cases"]}
    links = {
        "omh-v0.session-cancel-error-reuse": (
            "reference.session-cancel-error-reuse",
            [
                "ticket-08-agent-cancellation",
                "ticket-15-session",
                "ticket-15-installed",
            ],
        ),
        "omh-v0.session-managed-disposal": (
            "reference.session-managed-disposal",
            ["ticket-15-session", "ticket-15-installed"],
        ),
        "omh-v0.session-compaction": (
            "reference.session-compaction",
            ["ticket-15-session", "ticket-15-installed"],
        ),
    }

    for obligation_id, (corpus_id, executable_cases) in links.items():
        row = obligations[obligation_id]
        case = cases[corpus_id]
        assert row["corpusCase"] == corpus_id
        assert row["executableRunners"] == executable_cases
        assert case["obligation"] == obligation_id
        assert case["referenceCitations"]
