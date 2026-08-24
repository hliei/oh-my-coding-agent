from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path
import uuid

import pytest

import oh_my_coding_agent
from oh_my_coding_agent import (
    AgentSession,
    CURRENT_SESSION_VERSION,
    CreateAgentSessionOptions,
    NewSessionOptions,
    SessionHeader,
    SessionManager,
    createAgentSession,
)
from oh_my_llm import ModelsError, createModels, fauxProvider
from oh_my_llm.providers.deepseek import deepseekProvider


ROOT = Path(__file__).parents[2]


_SESSION_MANAGER_METHODS = {
    "appendCompaction",
    "appendCustomEntry",
    "appendCustomMessageEntry",
    "appendLabelChange",
    "appendMessage",
    "appendModelChange",
    "appendSessionInfo",
    "appendThinkingLevelChange",
    "branch",
    "branchWithSummary",
    "buildContextEntries",
    "buildSessionContext",
    "continueRecent",
    "create",
    "createBranchedSession",
    "forkFrom",
    "getBranch",
    "getChildren",
    "getCwd",
    "getEntries",
    "getEntry",
    "getHeader",
    "getLabel",
    "getLeafEntry",
    "getLeafId",
    "getSessionDir",
    "getSessionFile",
    "getSessionId",
    "getSessionName",
    "getTree",
    "inMemory",
    "isPersisted",
    "list",
    "listAll",
    "newSession",
    "open",
    "resetLeaf",
    "setSessionFile",
    "usesDefaultSessionDir",
}

_AGENT_SESSION_MEMBERS = {
    "abort",
    "abortBranchSummary",
    "abortCompaction",
    "autoCompactionEnabled",
    "compact",
    "dispose",
    "getUserMessagesForForking",
    "isCompacting",
    "isIdle",
    "isStreaming",
    "messages",
    "model",
    "navigateTree",
    "prompt",
    "sessionFile",
    "sessionId",
    "sessionManager",
    "sessionName",
    "setAutoCompactionEnabled",
    "setSessionName",
    "subscribe",
    "systemPrompt",
    "waitForIdle",
}


def test_session_manager_public_surface_is_closed() -> None:
    expected_roots = {
        "AgentSession",
        "AgentSessionEvent",
        "AgentSessionEventListener",
        "BranchSummaryEntry",
        "CompactionEntry",
        "CompactionResult",
        "CreateAgentSessionOptions",
        "CreateAgentSessionResult",
        "CustomEntry",
        "CustomMessageEntry",
        "FileEntry",
        "LabelEntry",
        "ModelChangeEntry",
        "NewSessionOptions",
        "PromptOptions",
        "SessionContext",
        "SessionEntry",
        "SessionEntryBase",
        "SessionHeader",
        "SessionInfo",
        "SessionInfoEntry",
        "SessionManager",
        "SessionMessageEntry",
        "SessionTreeNode",
        "ThinkingLevelChangeEntry",
        "CURRENT_SESSION_VERSION",
        "createAgentSession",
    }
    assert set(oh_my_coding_agent.__all__) == expected_roots
    assert CURRENT_SESSION_VERSION == 3
    assert {
        name for name in dir(SessionManager) if not name.startswith("_")
    } == _SESSION_MANAGER_METHODS
    assert not hasattr(oh_my_coding_agent, "ReadonlySessionManager")
    assert not hasattr(oh_my_coding_agent, "SessionPath")
    assert not hasattr(oh_my_coding_agent, "SessionId")
    assert not hasattr(SessionManager, "get_cwd")
    assert tuple(inspect.signature(NewSessionOptions).parameters) == (
        "id",
        "parentSession",
    )


def test_new_persistent_manager_is_empty_and_unflushed(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "project" / ".." / "project"
    session_dir = tmp_path / "sessions"
    manager = SessionManager.create(
        os.fspath(cwd),
        os.fspath(session_dir),
        NewSessionOptions(id="caller.id-1"),
    )

    assert manager.isPersisted() is True
    assert manager.getCwd() == os.path.abspath(os.fspath(cwd))
    assert manager.getSessionDir() == os.fspath(session_dir)
    assert manager.usesDefaultSessionDir() is False
    assert manager.getSessionId() == "caller.id-1"
    session_file = manager.getSessionFile()
    assert session_file is not None
    assert session_file.endswith("_caller.id-1.jsonl")
    assert session_dir.is_dir()
    assert not Path(session_file).exists()
    assert manager.getHeader() == SessionHeader(
        id="caller.id-1",
        timestamp=manager.getHeader().timestamp,
        cwd=os.path.abspath(os.fspath(cwd)),
    )
    assert manager.getEntries() == ()
    assert manager.getTree() == ()
    assert manager.getBranch() == ()
    assert manager.getLeafId() is None
    assert manager.getLeafEntry() is None
    assert manager.buildContextEntries() == ()
    assert manager.buildSessionContext().messages == ()


def test_in_memory_manager_has_no_filesystem_effects(tmp_path: Path) -> None:
    absent_cwd = tmp_path / "not-created"
    manager = SessionManager.inMemory(
        os.fspath(absent_cwd), NewSessionOptions(id="memory_1")
    )

    assert manager.isPersisted() is False
    assert manager.getSessionDir() == ""
    assert manager.getSessionFile() is None
    assert manager.getSessionId() == "memory_1"
    assert manager.getHeader().cwd == os.fspath(absent_cwd)
    assert manager.getTree() == ()
    assert manager.getLeafId() is None
    assert not absent_cwd.exists()


def test_session_ids_validate_before_effect_and_new_session_preserves_mode(
    tmp_path: Path,
) -> None:
    rejected_dir = tmp_path / "rejected"
    for invalid in ("", "-leading", "trailing-", "not space", "é"):
        with pytest.raises(ValueError):
            SessionManager.create(
                os.fspath(tmp_path),
                os.fspath(rejected_dir),
                NewSessionOptions(id=invalid),
            )
        assert not rejected_dir.exists()

    generated = SessionManager.inMemory(os.fspath(tmp_path))
    assert uuid.UUID(generated.getSessionId()).version == 7
    old_header = generated.getHeader()
    assert generated.newSession(NewSessionOptions(id="next.session")) is None
    assert generated.getSessionId() == "next.session"
    assert generated.getHeader() is not old_header
    assert generated.getCwd() == os.fspath(tmp_path)
    assert generated.isPersisted() is False
    assert generated.getEntries() == ()


def test_default_path_open_fork_discovery_and_no_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    monkeypatch.setenv("HOME", os.fspath(home))

    fresh = SessionManager.create(os.fspath(project))
    assert fresh.usesDefaultSessionDir() is True
    assert fresh.getSessionDir().startswith(
        os.fspath(home / ".omh" / "agent" / "sessions")
    )
    assert "--" + os.fspath(project).lstrip("/").replace("/", "-") + "--" in fresh.getSessionDir()
    assert fresh.getSessionFile() is not None
    fresh_file = fresh.getSessionFile()
    assert fresh_file is not None
    assert not Path(fresh_file).exists()
    assert asyncio.run(SessionManager.list(os.fspath(project))) == ()

    explicit = tmp_path / "explicit.jsonl"
    explicit.touch()
    opened = SessionManager.open(os.fspath(explicit), cwdOverride=os.fspath(project))
    assert explicit.exists()
    assert explicit.stat().st_size > 0
    assert opened.getEntries() == ()
    reopened = SessionManager.open(os.fspath(explicit))
    assert reopened.getSessionId() == opened.getSessionId()
    assert reopened.getSessionFile() == opened.getSessionFile()

    rows = asyncio.run(SessionManager.list(os.fspath(project), os.fspath(tmp_path)))
    assert [(row.path, row.id, row.messageCount) for row in rows] == [
        (os.fspath(explicit), opened.getSessionId(), 0)
    ]

    fork_dir = tmp_path / "forks"
    forked = SessionManager.forkFrom(
        os.fspath(explicit),
        os.fspath(tmp_path / "target"),
        os.fspath(fork_dir),
        NewSessionOptions(id="fork.1"),
    )
    assert forked.getSessionId() == "fork.1"
    assert forked.getHeader().parentSession == os.fspath(explicit)
    forked_file = forked.getSessionFile()
    assert forked_file is not None
    assert Path(forked_file).exists()
    assert SessionManager.open(forked_file).getSessionId() == "fork.1"


def test_create_agent_session_is_lazy_identity_preserving_and_disposable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    manager = SessionManager.inMemory(
        os.fspath(tmp_path / "manager-cwd"), NewSessionOptions(id="owned")
    )
    models = createModels()
    models.setProvider(deepseekProvider())
    model = models.getModel("deepseek", "deepseek-v4-flash")
    assert model is not None

    operation = createAgentSession(
        CreateAgentSessionOptions(
            cwd=os.fspath(tmp_path / "different-operational-cwd"),
            model=model,
            sessionManager=manager,
        )
    )
    assert inspect.iscoroutine(operation)
    assert manager.getHeader().cwd == os.fspath(tmp_path / "manager-cwd")

    result = asyncio.run(operation)
    session = result.session
    assert type(session) is AgentSession
    assert session.sessionManager is manager
    assert session.model is model
    assert session.sessionId == "owned"
    assert session.sessionFile is None
    assert session.sessionName is None
    assert session.messages == ()
    assert session.systemPrompt == (
        "Read file contents\n"
        "Execute bash commands (ls, grep, find, etc.)\n"
        "Make precise file edits with exact text replacement, including multiple disjoint edits in one call\n"
        "Create or overwrite files"
    )
    assert session.isStreaming is False
    assert session.isIdle is True
    assert {name for name in dir(AgentSession) if not name.startswith("_")} == _AGENT_SESSION_MEMBERS
    with pytest.raises(TypeError):
        AgentSession()  # type: ignore[call-arg]

    before = (manager.getHeader(), manager.getEntries(), manager.getSessionFile())
    asyncio.run(session.dispose())
    asyncio.run(session.dispose())
    assert session.sessionManager is manager
    assert (manager.getHeader(), manager.getEntries(), manager.getSessionFile()) == before
    assert session.isIdle is True


def test_creation_rejects_invalid_model_and_missing_auth_before_default_manager_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "project"
    monkeypatch.setenv("HOME", os.fspath(home))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    faux = fauxProvider().getModel()
    assert faux is not None
    with pytest.raises(ValueError):
        asyncio.run(
            createAgentSession(CreateAgentSessionOptions(cwd=os.fspath(cwd), model=faux))
        )
    assert not home.exists()

    with pytest.raises(ModelsError) as missing:
        asyncio.run(createAgentSession(CreateAgentSessionOptions(cwd=os.fspath(cwd))))
    assert missing.value.code == "auth"
    assert not home.exists()


def test_omitted_manager_is_created_only_on_first_await(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "project"
    monkeypatch.setenv("HOME", os.fspath(home))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

    operation = createAgentSession(CreateAgentSessionOptions(cwd=os.fspath(cwd)))
    assert not home.exists()
    result = asyncio.run(operation)

    manager = result.session.sessionManager
    assert manager.isPersisted() is True
    assert manager.getCwd() == os.fspath(cwd)
    session_file = manager.getSessionFile()
    assert session_file is not None
    assert not Path(session_file).exists()
    asyncio.run(result.session.dispose())
    assert not Path(session_file).exists()


def test_creation_cancellation_publishes_no_session_or_manager_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    manager = SessionManager.inMemory(
        os.fspath(tmp_path), NewSessionOptions(id="cancelled")
    )
    before = (manager.getHeader(), manager.getEntries(), manager.getSessionFile())

    async def cancel_at_publication_boundary() -> None:
        operation = asyncio.create_task(
            createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
        )
        await asyncio.sleep(0)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation

    asyncio.run(cancel_at_publication_boundary())
    assert (manager.getHeader(), manager.getEntries(), manager.getSessionFile()) == before


def test_ticket_12_matrix_records_empty_identity_id_and_no_lease_obligations() -> None:
    matrix = json.loads((ROOT / "conformance/obligation-matrix.json").read_text())
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    required = {
        "omh-v0.session-empty-unflushed": "reference.session-empty-unflushed",
        "omh-v0.session-supplied-manager-identity": (
            "reference.session-supplied-manager-identity"
        ),
        "omh-v0.session-caller-ids": "reference.session-caller-ids",
        "omh-v0.session-no-continuing-lease": (
            "reference.session-no-continuing-lease"
        ),
    }
    rows = {row["id"]: row for row in matrix["obligations"]}
    cases = {case["id"]: case for case in corpus["cases"]}
    assert required.keys() <= rows.keys()
    assert set(required.values()) <= cases.keys()
    for obligation, corpus_case in required.items():
        assert rows[obligation]["corpusCase"] == corpus_case
        assert rows[obligation]["executableCases"] == [
            "ticket-12-session",
            "ticket-12-installed",
        ]
        assert cases[corpus_case]["obligation"] == obligation
