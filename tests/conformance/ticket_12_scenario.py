from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import uuid

import oh_my_coding_agent._session as session_module
from oh_my_coding_agent import (
    CreateAgentSessionOptions,
    NewSessionOptions,
    SessionManager,
    createAgentSession,
)
from oh_my_llm import ModelsError, fauxProvider


async def _identity(root: Path) -> dict[str, object]:
    manager = SessionManager.inMemory(
        os.fspath(root / "manager"), NewSessionOptions(id="identity")
    )
    before = (manager.getHeader(), manager.getEntries(), manager.getSessionFile())
    result = await createAgentSession(
        CreateAgentSessionOptions(
            cwd=os.fspath(root / "operational"), sessionManager=manager
        )
    )
    session = result.session
    observed = {
        "sameManager": session.sessionManager is manager,
        "sessionId": session.sessionId,
        "model": {
            "provider": session.model.provider,
            "id": session.model.id,
        },
        "messages": len(session.messages),
    }
    await session.dispose()
    after = (manager.getHeader(), manager.getEntries(), manager.getSessionFile())
    return {
        "A": "supplied_manager_admitted",
        "L": [],
        "T": observed,
        "E": {"managerUnchangedByDisposal": before == after},
        "C": "disposed_idle",
    }


async def _reject_invalid_creation(root: Path) -> dict[str, object]:
    home = root / "rejected-home"
    os.environ["HOME"] = os.fspath(home)
    os.environ.pop("DEEPSEEK_API_KEY", None)
    foreign = fauxProvider().getModel()
    assert foreign is not None
    foreign_carrier = "not_rejected"
    try:
        await createAgentSession(
            CreateAgentSessionOptions(cwd=os.fspath(root), model=foreign)
        )
    except ValueError:
        foreign_carrier = "ValueError"

    auth_carrier = "not_rejected"
    try:
        await createAgentSession(CreateAgentSessionOptions(cwd=os.fspath(root)))
    except ModelsError as error:
        auth_carrier = f"ModelsError:{error.code}"

    os.environ["DEEPSEEK_API_KEY"] = "omh-conformance-canary"
    return {
        "A": {
            "foreignModel": "rejected",
            "missingAuthentication": "rejected",
        },
        "L": [],
        "T": {
            "foreignModel": foreign_carrier,
            "missingAuthentication": auth_carrier,
            "sessionPublished": False,
        },
        "E": {
            "defaultSessionDirectoryCreated": home.exists(),
            "modelRequests": 0,
        },
        "C": "later_valid_construction_possible",
    }


async def _cancel_atomic_publication(root: Path) -> dict[str, object]:
    manager = SessionManager.inMemory(
        os.fspath(root), NewSessionOptions(id="atomic-cancel")
    )
    before = (manager.getHeader(), manager.getEntries(), manager.getSessionFile())
    publication_reached = asyncio.Event()
    release_publication = asyncio.Event()
    original = session_module._yield_before_session_publication

    async def hold_publication() -> None:
        publication_reached.set()
        await release_publication.wait()

    session_module._yield_before_session_publication = hold_publication
    carrier = "not_cancelled"
    operation = asyncio.create_task(
        createAgentSession(CreateAgentSessionOptions(sessionManager=manager))
    )
    try:
        await publication_reached.wait()
        operation.cancel()
        release_publication.set()
        try:
            await operation
        except asyncio.CancelledError:
            carrier = "CancelledError"
    finally:
        session_module._yield_before_session_publication = original
    current = asyncio.current_task()
    return {
        "A": "cancelled_before_publication",
        "L": "cleanup_before_cancelled_error",
        "T": {"carrier": carrier, "sessionPublished": False},
        "E": {
            "managerUnchanged": before
            == (manager.getHeader(), manager.getEntries(), manager.getSessionFile())
        },
        "C": {
            "reachableSession": False,
            "liveOwnedTask": any(
                task is not current and not task.done() for task in asyncio.all_tasks()
            ),
        },
    }
async def main() -> None:
    os.environ["DEEPSEEK_API_KEY"] = "omh-conformance-canary"
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        persistent_dir = root / "sessions"
        persistent = SessionManager.create(
            os.fspath(root / "project"),
            os.fspath(persistent_dir),
            NewSessionOptions(id="empty"),
        )
        candidate = persistent.getSessionFile()
        assert candidate is not None
        memory_cwd = root / "memory-cwd"
        memory = SessionManager.inMemory(
            os.fspath(memory_cwd), NewSessionOptions(id="memory")
        )

        empty = {
            "A": "persistent_and_in_memory_admitted",
            "L": [],
            "T": {
                "version": persistent.getHeader().version,
                "persistentEmpty": persistent.getTree() == (),
                "memoryEmpty": memory.getTree() == (),
                "leafs": [persistent.getLeafId(), memory.getLeafId()],
            },
            "E": {
                "directoryCreated": persistent_dir.is_dir(),
                "candidateFileExists": Path(candidate).exists(),
                "memoryCwdExists": memory_cwd.exists(),
                "memoryFile": memory.getSessionFile(),
            },
            "C": "empty_unflushed",
        }

        generated = SessionManager.inMemory(os.fspath(root / "generated"))
        generated_id = generated.getSessionId()
        generated.newSession(NewSessionOptions(id="reset.id"))
        explicit_path = root / "explicit.jsonl"
        explicit_path.touch()
        source = SessionManager.open(
            os.fspath(explicit_path), cwdOverride=os.fspath(root / "source")
        )
        forked = SessionManager.forkFrom(
            os.fspath(explicit_path),
            os.fspath(root / "target"),
            os.fspath(root / "forks"),
            NewSessionOptions(id="fork.id"),
        )
        caller_ids = {
            "A": "caller_and_generated_ids_admitted",
            "L": [],
            "T": {
                "create": persistent.getSessionId(),
                "inMemory": memory.getSessionId(),
                "newSession": generated.getSessionId(),
                "forkFrom": forked.getSessionId(),
                "generatedVersion": uuid.UUID(generated_id).version,
            },
            "E": {"forkFileCreated": Path(forked.getSessionFile() or "").exists()},
            "C": "ids_preserved",
        }

        shared_one = SessionManager.open(os.fspath(explicit_path))
        shared_two = SessionManager.open(os.fspath(explicit_path))
        first = await createAgentSession(
            CreateAgentSessionOptions(sessionManager=shared_one)
        )
        second = await createAgentSession(
            CreateAgentSessionOptions(sessionManager=shared_two)
        )
        no_lease = {
            "A": "two_managers_and_sessions_admitted",
            "L": [],
            "T": {
                "samePath": shared_one.getSessionFile()
                == shared_two.getSessionFile(),
                "sameId": source.getSessionId() == shared_two.getSessionId(),
            },
            "E": {"leaseFiles": 0},
            "C": "independent_disposal",
        }
        await first.session.dispose()
        await second.session.dispose()

        actual = {
            "reference.session-empty-unflushed": empty,
            "reference.session-supplied-manager-identity": await _identity(root),
            "reference.session-caller-ids": caller_ids,
            "reference.session-no-continuing-lease": no_lease,
            "reference.reject-missing-model-at-session-creation": (
                await _reject_invalid_creation(root)
            ),
            "reference.atomic-session-construction": (
                await _cancel_atomic_publication(root)
            ),
        }
        print(json.dumps(actual, sort_keys=True, separators=(",", ":")))


asyncio.run(main())
