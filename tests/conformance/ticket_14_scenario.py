from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
from typing import Any, BinaryIO

import oh_my_coding_agent._session_manager as session_manager_module
from oh_my_coding_agent import LabelEntry, ModelChangeEntry, NewSessionOptions, SessionManager
from oh_my_llm import UserMessage, fauxAssistantMessage


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _process_append(
    session_file: str, model_id: str, ready: Any, release: Any
) -> None:
    manager = SessionManager.open(session_file)
    ready.set()
    release.wait()
    manager.appendModelChange("deepseek", model_id)


def _fault_continuity(root: Path) -> dict[str, object]:
    first = SessionManager.create(
        os.fspath(root / "project"),
        os.fspath(root / "first"),
        NewSessionOptions(id="first-fault"),
    )
    first.appendMessage(UserMessage(content="memory", timestamp=1))
    first_file = first.getSessionFile()
    assert first_file is not None
    original_write = session_manager_module._write_bytes

    def fail_first(output: BinaryIO, data: bytes) -> None:
        output.write(data[:17])
        raise OSError("first cutoff")

    session_manager_module._write_bytes = fail_first
    first_error = ""
    try:
        first.appendMessage(fauxAssistantMessage("assistant"))
    except OSError as error:
        first_error = type(error).__name__
    finally:
        session_manager_module._write_bytes = original_write

    append = SessionManager.create(
        os.fspath(root / "project"),
        os.fspath(root / "append"),
        NewSessionOptions(id="append-fault"),
    )
    assistant_id = append.appendMessage(fauxAssistantMessage("durable"))
    append_file = append.getSessionFile()
    assert append_file is not None
    durable = Path(append_file).read_bytes()

    def fail_append(output: BinaryIO, data: bytes) -> None:
        del output, data
        raise OSError("append cutoff")

    session_manager_module._write_bytes = fail_append
    append_error = ""
    try:
        append.appendLabelChange(assistant_id, "memory-only")
    except OSError as error:
        append_error = type(error).__name__
    finally:
        session_manager_module._write_bytes = original_write
    failed_label = append.getEntries()[-1]
    assert isinstance(failed_label, LabelEntry)
    later_id = append.appendModelChange("deepseek", "continued")
    reopened = SessionManager.open(append_file)
    later = reopened.getEntry(later_id)
    assert isinstance(later, ModelChangeEntry)

    rewrite = root / "rewrite.jsonl"
    rewrite.touch()
    rewrite_cutoff = 19

    def fail_rewrite(output: BinaryIO, data: bytes) -> None:
        output.write(data[:rewrite_cutoff])
        raise OSError("rewrite cutoff")

    session_manager_module._write_bytes = fail_rewrite
    rewrite_error = ""
    try:
        SessionManager.open(os.fspath(rewrite), cwdOverride=os.fspath(root))
    except OSError as error:
        rewrite_error = type(error).__name__
    finally:
        session_manager_module._write_bytes = original_write

    read_target = root / "read.jsonl"
    read_target.write_bytes(
        _json_bytes(
            {
                "type": "session",
                "version": 3,
                "id": "read-cutoff",
                "timestamp": "2026-08-24T00:00:00.000Z",
                "cwd": os.fspath(root),
            }
        )
        + b"\n"
    )
    read_target_bytes = read_target.read_bytes()
    original_read = session_manager_module._read_bytes

    def fail_read(path: str) -> bytes:
        del path
        raise OSError("read cutoff")

    session_manager_module._read_bytes = fail_read
    read_error = ""
    try:
        SessionManager.open(os.fspath(read_target))
    except OSError as error:
        read_error = type(error).__name__
    finally:
        session_manager_module._read_bytes = original_read

    return {
        "A": "raw_file_faults_propagated",
        "L": [],
        "T": {
            "errors": [first_error, append_error],
            "firstMemoryTypes": [entry.type for entry in first.getEntries()],
            "installedCutoffErrors": [rewrite_error, read_error],
            "labelRetainedInMemory": append.getLabel(assistant_id) == "memory-only",
        },
        "E": {
            "firstPartialBytes": Path(first_file).stat().st_size,
            "appendPrefixUnchanged": durable == Path(append_file).read_bytes()[: len(durable)],
            "readTargetUnchanged": read_target.read_bytes() == read_target_bytes,
            "rewritePartialBytes": rewrite.stat().st_size,
            "settledMarkers": 0,
        },
        "C": {
            "continuedFromMissingParent": later.parentId == failed_label.id,
            "reopenedLostFailedLabel": reopened.getEntry(failed_label.id) is None,
            "reopenedBranchTypes": [entry.type for entry in reopened.getBranch()],
        },
    }


async def _best_effort_recovery(root: Path) -> dict[str, object]:
    root.mkdir(parents=True)
    recovery = root / "recovery.jsonl"
    recovery_bytes = b"\n".join(
        (
            b"",
            b'{"broken":',
            _json_bytes(
                {
                    "type": "session",
                    "version": 3,
                    "id": "recovery",
                    "timestamp": "2026-08-24T00:00:00.000Z",
                    "cwd": os.fspath(root),
                }
            ),
            _json_bytes(
                {
                    "type": "model_change",
                    "id": "model",
                    "parentId": None,
                    "timestamp": "2026-08-24T00:00:01.000Z",
                    "provider": "deepseek",
                    "modelId": "recovered",
                }
            ),
            b'{"type":"message"',
            _json_bytes(
                {
                    "type": "label",
                    "id": "label",
                    "parentId": "model",
                    "timestamp": "2026-08-24T00:00:02.000Z",
                    "targetId": "model",
                    "label": "kept",
                }
            ),
        )
    )
    recovery.write_bytes(recovery_bytes)
    recovered = SessionManager.open(os.fspath(recovery))

    legacy = root / "legacy.jsonl"
    legacy.write_bytes(
        _json_bytes(
            {
                "type": "session",
                "id": "legacy",
                "timestamp": "2026-08-24T00:00:00.000Z",
                "cwd": os.fspath(root),
            }
        )
        + b"\n"
        + _json_bytes(
            {
                "type": "model_change",
                "timestamp": "2026-08-24T00:00:01.000Z",
                "provider": "deepseek",
                "modelId": "legacy",
            }
        )
        + b"\n"
    )
    migrated = SessionManager.open(os.fspath(legacy))

    sessions = root / "sessions"
    sessions.mkdir()
    strict = sessions / "strict.jsonl"
    strict.write_bytes(
        _json_bytes(
            {
                "type": "session",
                "version": 3,
                "id": "strict",
                "timestamp": "2026-08-24T00:00:00.000Z",
                "cwd": os.fspath(root),
            }
        )
        + b"\n"
    )
    tolerant = sessions / "tolerant.jsonl"
    tolerant.write_bytes(
        b"\n"
        + _json_bytes(
            {
                "type": "session",
                "version": 3,
                "id": "tolerant",
                "timestamp": "2026-08-24T00:00:01.000Z",
                "cwd": os.fspath(root),
            }
        )
    )
    os.utime(strict, ns=(100, 100))
    os.utime(tolerant, ns=(200, 200))
    recent = SessionManager.continueRecent(os.fspath(root), os.fspath(sessions))
    listed = await SessionManager.list(os.fspath(root), os.fspath(sessions))
    listed_all = await SessionManager.listAll(os.fspath(sessions))

    invalid = root / "invalid.jsonl"
    invalid_bytes = b'{"type":"not-session","id":"invalid"}\n'
    invalid.write_bytes(invalid_bytes)
    invalid_error = ""
    try:
        SessionManager.open(os.fspath(invalid))
    except ValueError as error:
        invalid_error = type(error).__name__
    absent = root / "absent.jsonl"
    absent_manager = SessionManager.open(os.fspath(absent))
    empty = root / "empty.jsonl"
    empty.touch()
    empty_manager = SessionManager.open(os.fspath(empty), cwdOverride=os.fspath(root))

    memory = SessionManager.inMemory(os.fspath(root / "memory"))
    memory.setSessionFile(os.fspath(strict))
    strict_before = strict.read_bytes()
    memory.appendModelChange("deepseek", "memory-only")

    return {
        "A": "operation_specific_file_admission",
        "L": [],
        "T": {
            "recoveredTypes": [entry.type for entry in recovered.getEntries()],
            "label": recovered.getLabel("model"),
            "migratedVersion": migrated.getHeader().version,
            "recent": recent.getSessionId(),
            "listed": [row.id for row in listed],
            "listedAll": [row.id for row in listed_all],
        },
        "E": {
            "recoveryUnchanged": recovery.read_bytes() == recovery_bytes,
            "invalid": invalid_error,
            "invalidUnchanged": invalid.read_bytes() == invalid_bytes,
            "absentUnflushed": not absent.exists() and absent_manager.getEntries() == (),
            "emptyInitialized": empty.stat().st_size > 0 and empty_manager.getEntries() == (),
            "inMemoryDidNotAppend": strict.read_bytes() == strict_before,
        },
        "C": {
            "leaf": recovered.getLeafId(),
            "migrationEntryCount": len(migrated.getEntries()),
            "memoryModeRetained": not memory.isPersisted(),
        },
    }


def _multi_manager_append(root: Path) -> dict[str, object]:
    seed = SessionManager.create(
        os.fspath(root / "project"),
        os.fspath(root / "sessions"),
        NewSessionOptions(id="shared"),
    )
    parent_id = seed.appendMessage(fauxAssistantMessage("seed"))
    session_file = seed.getSessionFile()
    assert session_file is not None
    context = multiprocessing.get_context("spawn")
    first_ready = context.Event()
    second_ready = context.Event()
    release = context.Event()
    first = context.Process(
        target=_process_append,
        args=(session_file, "one", first_ready, release),
    )
    second = context.Process(
        target=_process_append,
        args=(session_file, "two", second_ready, release),
    )
    first.start()
    second.start()
    assert first_ready.wait(10)
    assert second_ready.wait(10)
    stale_count = len(seed.getEntries())
    release.set()
    first.join(10)
    second.join(10)
    reopened = SessionManager.open(session_file)
    appended = reopened.getEntries()[1:]
    models = sorted(
        entry.modelId for entry in appended if isinstance(entry, ModelChangeEntry)
    )
    return {
        "A": "two_process_snapshots_admitted",
        "L": [],
        "T": {
            "models": models,
            "sharedParent": all(entry.parentId == parent_id for entry in appended),
            "physicalEntryCount": len(reopened.getEntries()),
        },
        "E": {
            "exitCodes": [first.exitcode, second.exitcode],
            "leaseFiles": 0,
        },
        "C": {
            "staleSnapshotCount": stale_count,
            "recoveredLeafInPhysicalSuffix": reopened.getLeafId() == appended[-1].id,
        },
    }


async def main() -> None:
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        actual = {
            "reference.session-persistence-fault-continuity": _fault_continuity(
                root / "faults"
            ),
            "reference.session-best-effort-recovery": await _best_effort_recovery(
                root / "recovery"
            ),
            "reference.session-stale-multi-manager-append": _multi_manager_append(
                root / "concurrency"
            ),
        }
        print(json.dumps(actual, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    asyncio.run(main())
