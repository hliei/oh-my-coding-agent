from __future__ import annotations

import asyncio
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
from oh_my_llm import AssistantMessage, UserMessage
from oh_my_llm import LifecycleError


ROOT = Path(__file__).parents[2]

_SESSION_EVENT_VARIANTS = {
    "AgentEnd",
    "AgentSettled",
    "AgentStart",
    "CompactionEnd",
    "CompactionStart",
    "MessageEnd",
    "MessageStart",
    "MessageUpdate",
    "ToolExecutionEnd",
    "ToolExecutionStart",
    "ToolExecutionUpdate",
    "TurnEnd",
    "TurnStart",
}


_TEXT_SSE = (
    b'data: {"id":"chatcmpl-session","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"Done"},"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-session","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4,"prompt_cache_hit_tokens":0}}\n\n'
    b"data: [DONE]\n\n"
)


class _HttpSpy:
    def __init__(self, body: bytes = _TEXT_SSE) -> None:
        self.body = body
        self.requests: list[httpx.Request] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = self

        class FakeClient(httpx.AsyncClient):
            def __init__(self, **kwargs: Any) -> None:
                kwargs = dict(kwargs)
                kwargs["transport"] = httpx.MockTransport(spy._handle)
                super().__init__(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            content=self.body,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )


class _BlockingTransport(httpx.AsyncBaseTransport):
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


def test_session_event_surface_is_closed() -> None:
    assert {
        name for name in dir(AgentSessionEvent) if not name.startswith("_")
    } == _SESSION_EVENT_VARIANTS
    with pytest.raises(TypeError, match="sealed event base"):
        AgentSessionEvent()


def test_prompt_lazily_flushes_one_no_tool_run_and_reopens_active_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    spy = _HttpSpy()
    spy.install(monkeypatch)
    manager = SessionManager.create(
        os.fspath(tmp_path / "project"),
        os.fspath(tmp_path / "sessions"),
        NewSessionOptions(id="prompt-one"),
    )
    session = asyncio.run(
        createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
    ).session
    session_file = manager.getSessionFile()
    assert session_file is not None
    observations: list[tuple[str, bool, bool]] = []
    events: list[AgentSessionEvent] = []

    def listener(event: AgentSessionEvent) -> None:
        events.append(event)
        observations.append((event.type, session.isStreaming, session.isIdle))

    session.subscribe(listener)
    operation = session.prompt("Hello")
    assert not Path(session_file).exists()
    assert manager.getEntries() == ()
    assert spy.requests == []

    assert asyncio.run(operation) is None

    assert len(spy.requests) == 1
    entries = manager.getEntries()
    assert [entry.type for entry in entries] == [
        "model_change",
        "thinking_level_change",
        "message",
        "message",
    ]
    user = entries[2].message  # type: ignore[union-attr]
    assistant = entries[3].message  # type: ignore[union-attr]
    assert isinstance(user, UserMessage)
    assert user.content == "Hello"
    assert isinstance(assistant, AssistantMessage)
    assert manager.buildSessionContext().messages == (user, assistant)
    assert session.messages == (user, assistant)

    agent_end = cast(
        Any,
        next(
            event for event in events if isinstance(event, AgentSessionEvent.AgentEnd)
        ),
    )
    assert agent_end.messages == (user, assistant)
    assert agent_end.messages[0] is user
    assert agent_end.messages[1] is assistant
    assert agent_end.willRetry is False
    assert isinstance(events[-1], AgentSessionEvent.AgentSettled)
    assert next(item for item in observations if item[0] == "agent_end") == (
        "agent_end",
        True,
        False,
    )
    assert observations[-1] == ("agent_settled", False, True)

    physical = Path(session_file).read_text().splitlines()
    assert len(physical) == 5
    assert [json.loads(line)["type"] for line in physical] == [
        "session",
        "model_change",
        "thinking_level_change",
        "message",
        "message",
    ]

    reopened = SessionManager.open(session_file)
    assert reopened.getEntries() == entries
    assert reopened.getLeafId() == entries[-1].id
    assert reopened.getBranch() == entries
    assert reopened.getTree() == manager.getTree()
    assert reopened.getEntry(entries[-1].id) == entries[-1]
    assert reopened.getChildren(entries[-2].id) == (entries[-1],)
    assert reopened.buildSessionContext() == manager.buildSessionContext()
    recovered = asyncio.run(
        createAgentSession(CreateAgentSessionOptions(sessionManager=reopened))
    ).session
    assert recovered.messages == (user, assistant)
    assert len(spy.requests) == 1

    asyncio.run(recovered.dispose())
    asyncio.run(session.dispose())


def test_busy_and_carrier_misuse_precede_append_and_model_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    async def scenario() -> None:
        transport = _BlockingTransport()
        _install_transport(monkeypatch, transport)
        manager = SessionManager.inMemory(os.fspath(tmp_path))
        session = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        ).session

        with pytest.raises(TypeError):
            await session.prompt(1)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            await session.prompt("bad options", object())  # type: ignore[arg-type]
        assert manager.getEntries() == ()

        await session.prompt("first")
        active = asyncio.create_task(session.prompt("blocked"))
        await transport.second_started.wait()
        before = manager.getEntries()
        with pytest.raises(LifecycleError) as busy:
            await session.prompt("rejected")
        assert busy.value.code == "busy"
        assert manager.getEntries() == before
        assert len(transport.requests) == 2

        transport.release_second.set()
        await active
        await session.dispose()
        with pytest.raises(LifecycleError) as disposed:
            await session.prompt("after disposal")
        assert disposed.value.code == "disposed"
        assert len(transport.requests) == 2

    asyncio.run(scenario())


def test_subscriptions_are_independent_ordered_snapshot_barriers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    _HttpSpy().install(monkeypatch)
    manager = SessionManager.inMemory(os.fspath(tmp_path))
    session = asyncio.run(
        createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
    ).session
    observed: list[tuple[str, str]] = []

    def duplicate(event: AgentSessionEvent) -> None:
        observed.append(("duplicate", event.type))

    remove_first = session.subscribe(duplicate)
    session.subscribe(duplicate)
    remove_first()
    remove_first()

    def later(event: AgentSessionEvent) -> None:
        observed.append(("later", event.type))

    remove_second: Any

    def mutating(event: AgentSessionEvent) -> None:
        observed.append(("mutating", event.type))
        if event.type == "agent_start":
            remove_second()
            session.subscribe(later)

    session.subscribe(mutating)
    remove_second = session.subscribe(
        lambda event: observed.append(("removed", event.type))
    )

    asyncio.run(session.prompt("snapshot"))

    starts = [name for name, event_type in observed if event_type == "agent_start"]
    assert starts == ["duplicate", "mutating", "removed"]
    turns = [name for name, event_type in observed if event_type == "turn_start"]
    assert turns == ["duplicate", "mutating", "later"]
    assert [event_type for name, event_type in observed if name == "duplicate"] == [
        event_type for name, event_type in observed if name == "mutating"
    ]
    asyncio.run(session.dispose())


def test_reopen_retains_persisted_incomplete_suffix_without_redispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    async def scenario() -> None:
        transport = _BlockingTransport()
        _install_transport(monkeypatch, transport)
        manager = SessionManager.create(
            os.fspath(tmp_path / "project"),
            os.fspath(tmp_path / "sessions"),
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
        persisted_before_reopen = Path(session_file).read_bytes()
        assert json.loads(persisted_before_reopen.splitlines()[-1])["message"][
            "content"
        ] == "interrupted"

        reopened = SessionManager.open(session_file)
        recovered = (
            await createAgentSession(CreateAgentSessionOptions(sessionManager=reopened))
        ).session
        assert recovered.isIdle is True
        assert recovered.isStreaming is False
        assert [
            message.content
            for message in recovered.messages
            if isinstance(message, UserMessage)
        ] == ["complete", "interrupted"]
        assert isinstance(recovered.messages[-1], UserMessage)
        assert len(transport.requests) == 2
        assert Path(session_file).read_bytes() == persisted_before_reopen

        await recovered.dispose()
        transport.release_second.set()
        await interrupted
        await session.dispose()

    asyncio.run(scenario())


def test_wait_for_idle_includes_the_agent_settled_listener_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    _HttpSpy().install(monkeypatch)

    async def scenario() -> None:
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    sessionManager=SessionManager.inMemory(os.fspath(tmp_path))
                )
            )
        ).session
        settled_started = asyncio.Event()
        release_settled = asyncio.Event()

        async def listener(event: AgentSessionEvent) -> None:
            if isinstance(event, AgentSessionEvent.AgentSettled):
                assert session.isStreaming is False
                assert session.isIdle is True
                settled_started.set()
                await release_settled.wait()

        session.subscribe(listener)
        prompt = asyncio.create_task(session.prompt("barrier"))
        await settled_started.wait()
        idle = asyncio.create_task(session.waitForIdle())
        await asyncio.sleep(0)
        assert idle.done() is False
        assert prompt.done() is False

        release_settled.set()
        await prompt
        await idle
        await session.dispose()

    asyncio.run(scenario())


def test_ticket_13_matrix_records_prompt_and_incomplete_recovery() -> None:
    matrix = json.loads((ROOT / "conformance/obligation-matrix.json").read_text())
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    required = {
        "omh-v0.session-prompt-lazy-flush": "reference.session-prompt-lazy-flush",
        "omh-v0.session-incomplete-recovery": (
            "reference.session-incomplete-recovery"
        ),
    }
    rows = {row["id"]: row for row in matrix["obligations"]}
    cases = {case["id"]: case for case in corpus["cases"]}
    assert required.keys() <= rows.keys()
    assert set(required.values()) <= cases.keys()
    for obligation, corpus_case in required.items():
        assert rows[obligation]["corpusCase"] == corpus_case
        assert rows[obligation]["executableCases"] == [
            "ticket-13-session",
            "ticket-13-installed",
        ]
        assert cases[corpus_case]["obligation"] == obligation
