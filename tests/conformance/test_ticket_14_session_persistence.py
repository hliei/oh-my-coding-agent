from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
from pathlib import Path
from typing import Any, BinaryIO

import pytest

import oh_my_coding_agent._session_manager as session_manager_module
from oh_my_coding_agent import (
    BranchSummaryEntry,
    CompactionEntry,
    CustomEntry,
    CustomMessageEntry,
    LabelEntry,
    ModelChangeEntry,
    SessionManager,
    SessionMessageEntry,
)
from oh_my_llm import TextContent, UserMessage, fauxAssistantMessage


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _append_from_process(
    session_file: str,
    model_id: str,
    ready: Any,
    release: Any,
) -> None:
    manager = SessionManager.open(session_file)
    ready.set()
    release.wait()
    manager.appendModelChange("deepseek", model_id)


def test_open_recovers_best_effort_physical_lines_without_rewrite(
    tmp_path: Path,
) -> None:
    session_file = tmp_path / "recovery.jsonl"
    original = b"\n".join(
        (
            b"",
            b'{"broken":',
            _json_bytes(
                {"type": "session", "version": 3, "id": "parsed-prefix"}
            ),
            b"not json",
            _json_bytes(
                {
                    "type": "model_change",
                    "id": "model",
                    "parentId": None,
                    "timestamp": "2026-08-24T00:00:00.000Z",
                    "provider": "deepseek",
                    "modelId": "deepseek-v4-flash",
                }
            ),
            b'{"type":"message"',
            _json_bytes(
                {
                    "type": "label",
                    "id": "label",
                    "parentId": "model",
                    "timestamp": "2026-08-24T00:00:01.000Z",
                    "targetId": "model",
                    "label": "recovered",
                }
            ),
        )
    )
    session_file.write_bytes(original)

    manager = SessionManager.open(
        os.fspath(session_file), cwdOverride=os.fspath(tmp_path / "project")
    )

    assert manager.getSessionId() == "parsed-prefix"
    assert isinstance(manager.getEntries()[0], ModelChangeEntry)
    assert isinstance(manager.getEntries()[1], LabelEntry)
    assert manager.getLeafId() == "label"
    assert manager.getLabel("model") == "recovered"
    assert manager.getTree()[0].label == "recovered"
    assert session_file.read_bytes() == original


def test_lazy_first_flush_exposes_exact_partial_bytes_and_keeps_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = SessionManager.create(os.fspath(tmp_path), os.fspath(tmp_path))
    manager.appendMessage(UserMessage(content="before assistant", timestamp=1))
    session_file = manager.getSessionFile()
    assert session_file is not None
    cutoff = 17

    def fail_first_write(output: BinaryIO, data: bytes) -> None:
        output.write(data[:cutoff])
        raise OSError("first flush cutoff")

    monkeypatch.setattr(
        session_manager_module, "_write_bytes", fail_first_write, raising=False
    )

    with pytest.raises(OSError, match="first flush cutoff"):
        manager.appendMessage(fauxAssistantMessage("answer"))

    header = manager.getHeader()
    expected_header = _json_bytes(
        {
            "type": "session",
            "version": 3,
            "id": header.id,
            "timestamp": header.timestamp,
            "cwd": header.cwd,
        }
    ) + b"\n"
    assert Path(session_file).read_bytes() == expected_header[:cutoff]
    assert [entry.type for entry in manager.getEntries()] == ["message", "message"]
    assert manager.getLeafId() == manager.getEntries()[-1].id
    assert all(entry.type != "session_info" for entry in manager.getEntries())


def test_append_failure_retains_indexes_labels_and_divergent_continuity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = SessionManager.create(os.fspath(tmp_path), os.fspath(tmp_path))
    assistant_id = manager.appendMessage(fauxAssistantMessage("durable"))
    session_file = manager.getSessionFile()
    assert session_file is not None
    durable_prefix = Path(session_file).read_bytes()
    write_bytes = session_manager_module._write_bytes

    def fail_append(_output: BinaryIO, _data: bytes) -> None:
        raise OSError("append cutoff")

    monkeypatch.setattr(session_manager_module, "_write_bytes", fail_append)
    with pytest.raises(OSError, match="append cutoff"):
        manager.appendLabelChange(assistant_id, "memory only")

    failed_label = manager.getEntries()[-1]
    assert isinstance(failed_label, LabelEntry)
    assert manager.getEntry(failed_label.id) is failed_label
    assert manager.getLeafEntry() is failed_label
    assert manager.getLabel(assistant_id) == "memory only"
    assert manager.getTree()[0].label == "memory only"
    assert Path(session_file).read_bytes() == durable_prefix

    monkeypatch.setattr(session_manager_module, "_write_bytes", write_bytes)
    later_id = manager.appendModelChange("deepseek", "after-failure")
    later = manager.getEntry(later_id)
    assert isinstance(later, ModelChangeEntry)
    assert later.parentId == failed_label.id

    reopened = SessionManager.open(session_file)
    assert reopened.getEntry(failed_label.id) is None
    assert reopened.getLabel(assistant_id) is None
    assert reopened.getEntry(later_id) == later
    assert reopened.getBranch() == (later,)


def test_open_directly_migrates_registered_v1_and_v2_formats(
    tmp_path: Path,
) -> None:
    v1_file = tmp_path / "v1.jsonl"
    v1_file.write_bytes(
        b"\n".join(
            _json_bytes(value)
            for value in (
                {
                    "type": "session",
                    "id": "legacy-v1",
                    "timestamp": "2026-08-24T00:00:00.000Z",
                    "cwd": os.fspath(tmp_path),
                },
                {
                    "type": "model_change",
                    "timestamp": "2026-08-24T00:00:01.000Z",
                    "provider": "deepseek",
                    "modelId": "legacy",
                },
                {
                    "type": "compaction",
                    "timestamp": "2026-08-24T00:00:02.000Z",
                    "summary": "legacy summary",
                    "firstKeptEntryIndex": 1,
                    "tokensBefore": 9,
                },
            )
        )
        + b"\n"
    )

    migrated = SessionManager.open(os.fspath(v1_file))
    model, compaction = migrated.getEntries()
    assert isinstance(model, ModelChangeEntry)
    assert isinstance(compaction, CompactionEntry)
    assert model.parentId is None
    assert compaction.parentId == model.id
    assert compaction.firstKeptEntryId == model.id
    migrated_values = [json.loads(line) for line in v1_file.read_bytes().splitlines()]
    assert migrated_values[0]["version"] == 3
    assert migrated_values[1]["id"] == model.id
    assert migrated_values[2]["parentId"] == model.id
    assert migrated_values[2]["firstKeptEntryId"] == model.id
    assert "firstKeptEntryIndex" not in migrated_values[2]

    v2_file = tmp_path / "v2.jsonl"
    v2_file.write_bytes(
        b"\n".join(
            _json_bytes(value)
            for value in (
                {
                    "type": "session",
                    "version": 2,
                    "id": "legacy-v2",
                    "timestamp": "2026-08-24T00:00:00.000Z",
                    "cwd": os.fspath(tmp_path),
                },
                {
                    "type": "message",
                    "id": "hook",
                    "parentId": None,
                    "timestamp": "2026-08-24T00:00:01.000Z",
                    "message": {"role": "hookMessage", "content": "legacy"},
                },
            )
        )
        + b"\n"
    )

    SessionManager.open(os.fspath(v2_file))
    v2_values = [json.loads(line) for line in v2_file.read_bytes().splitlines()]
    assert v2_values[0]["version"] == 3
    assert v2_values[1]["message"]["role"] == "custom"


def test_recent_probe_and_best_effort_discovery_keep_distinct_failure_rules(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    sessions = tmp_path / "sessions"
    sessions.mkdir()

    strict = sessions / "strict.jsonl"
    strict.write_bytes(
        b"\n".join(
            _json_bytes(value)
            for value in (
                {
                    "type": "session",
                    "version": 3,
                    "id": "strict",
                    "timestamp": "2026-08-24T00:00:00.000Z",
                    "cwd": os.fspath(project),
                },
                {
                    "type": "message",
                    "id": "user",
                    "parentId": None,
                    "timestamp": "2026-08-24T00:00:01.000Z",
                    "message": {
                        "role": "user",
                        "content": "first question",
                        "timestamp": 10,
                    },
                },
                {
                    "type": "session_info",
                    "id": "name",
                    "parentId": "user",
                    "timestamp": "2026-08-24T00:00:02.000Z",
                    "name": "  Named Session  ",
                },
            )
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
                "timestamp": "2026-08-24T00:00:03.000Z",
                "cwd": os.fspath(project),
            }
        )
        + b"\n"
        + _json_bytes(
            {
                "type": "message",
                "id": "assistant",
                "parentId": None,
                "timestamp": "2026-08-24T00:00:04.000Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "later answer"}],
                    "timestamp": 20,
                },
            }
        )
    )
    invalid = sessions / "invalid.jsonl"
    invalid.write_text('{"type":"not-session","id":"invalid"}\n')
    os.utime(strict, ns=(100, 100))
    os.utime(tolerant, ns=(300, 300))
    os.utime(invalid, ns=(200, 200))

    recent = SessionManager.continueRecent(
        os.fspath(project), os.fspath(sessions)
    )
    assert recent.getSessionId() == "strict"

    progress: list[tuple[int, int]] = []
    rows = asyncio.run(
        SessionManager.list(
            os.fspath(project),
            os.fspath(sessions),
            lambda loaded, total: progress.append((loaded, total)),
        )
    )
    assert [row.id for row in rows] == ["tolerant", "strict"]
    assert progress == [(1, 3), (2, 3), (3, 3)]
    strict_row = next(row for row in rows if row.id == "strict")
    assert strict_row.name == "Named Session"
    assert strict_row.messageCount == 1
    assert strict_row.firstMessage == "first question"
    assert strict_row.allMessagesText == "first question"
    tolerant_row = next(row for row in rows if row.id == "tolerant")
    assert tolerant_row.messageCount == 1
    assert tolerant_row.firstMessage == "(no messages)"
    assert tolerant_row.allMessagesText == "later answer"

    assert [row.id for row in asyncio.run(SessionManager.listAll(os.fspath(sessions)))] == [
        "tolerant",
        "strict",
    ]
    assert asyncio.run(SessionManager.listAll(os.fspath(strict))) == ()


def test_manager_appends_typed_tree_entries_and_materializes_one_path(
    tmp_path: Path,
) -> None:
    manager = SessionManager.create(os.fspath(tmp_path), os.fspath(tmp_path))
    assistant_id = manager.appendMessage(fauxAssistantMessage("root"))
    model_id = manager.appendModelChange("deepseek", "branch-model")
    manager.appendLabelChange(assistant_id, "kept label")
    source_file = manager.getSessionFile()
    assert source_file is not None
    source_bytes = Path(source_file).read_bytes()

    manager.branch(model_id)
    selected_file = manager.createBranchedSession(model_id)
    assert selected_file is not None
    assert selected_file != source_file
    assert Path(source_file).read_bytes() == source_bytes
    assert manager.getHeader().parentSession == source_file
    assert manager.getLabel(assistant_id) == "kept label"
    selected = manager.getEntries()
    assert [type(entry) for entry in selected] == [
        SessionMessageEntry,
        ModelChangeEntry,
        LabelEntry,
    ]
    assert selected[0].parentId is None
    assert selected[1].parentId == selected[0].id
    assert selected[2].parentId == selected[1].id
    assert SessionManager.open(selected_file).getEntries() == selected

    memory = SessionManager.inMemory(os.fspath(tmp_path))
    custom_id = memory.appendCustomEntry("state", {"count": 1})
    custom_message_id = memory.appendCustomMessageEntry(
        "notice", (TextContent(text="visible"),), True, {"source": "test"}
    )
    memory.appendSessionInfo("  one\r\n two  ")
    compaction_id = memory.appendCompaction("summary", custom_id, 12)
    summary_id = memory.branchWithSummary(custom_message_id, "abandoned")
    assert memory.getSessionName() == "one  two"
    assert isinstance(memory.getEntry(custom_id), CustomEntry)
    assert isinstance(memory.getEntry(custom_message_id), CustomMessageEntry)
    assert isinstance(memory.getEntry(compaction_id), CompactionEntry)
    assert isinstance(memory.getEntry(summary_id), BranchSummaryEntry)
    assert memory.getLeafId() == summary_id


def test_multiple_process_snapshots_append_without_a_continuing_lease(
    tmp_path: Path,
) -> None:
    seed = SessionManager.create(os.fspath(tmp_path), os.fspath(tmp_path))
    parent_id = seed.appendMessage(fauxAssistantMessage("seed"))
    session_file = seed.getSessionFile()
    assert session_file is not None

    context = multiprocessing.get_context("spawn")
    first_ready = context.Event()
    second_ready = context.Event()
    release = context.Event()
    first = context.Process(
        target=_append_from_process,
        args=(session_file, "process-one", first_ready, release),
    )
    second = context.Process(
        target=_append_from_process,
        args=(session_file, "process-two", second_ready, release),
    )
    first.start()
    second.start()
    assert first_ready.wait(10)
    assert second_ready.wait(10)
    assert seed.getEntries()[-1].id == parent_id
    release.set()
    first.join(10)
    second.join(10)
    assert first.exitcode == 0
    assert second.exitcode == 0

    reopened = SessionManager.open(session_file)
    appended = reopened.getEntries()[1:]
    assert {entry.modelId for entry in appended if isinstance(entry, ModelChangeEntry)} == {
        "process-one",
        "process-two",
    }
    assert all(entry.parentId == parent_id for entry in appended)
    assert reopened.getLeafId() == appended[-1].id
    assert len(reopened.getTree()[0].children) == 2


def test_set_session_file_retains_manager_mode_and_raw_invalid_failure(
    tmp_path: Path,
) -> None:
    source = SessionManager.create(os.fspath(tmp_path), os.fspath(tmp_path))
    source.appendMessage(fauxAssistantMessage("source"))
    source_file = source.getSessionFile()
    assert source_file is not None
    source_bytes = Path(source_file).read_bytes()

    memory = SessionManager.inMemory(os.fspath(tmp_path / "memory"))
    memory.setSessionFile(source_file)
    assert memory.isPersisted() is False
    assert memory.getSessionDir() == ""
    assert memory.getSessionFile() == source_file
    assert memory.getEntries() == source.getEntries()
    memory.appendModelChange("deepseek", "memory-only")
    assert Path(source_file).read_bytes() == source_bytes

    persistent = SessionManager.create(
        os.fspath(tmp_path / "project"), os.fspath(tmp_path / "configured")
    )
    absent = tmp_path / "exact" / "absent.jsonl"
    persistent.setSessionFile(os.fspath(absent))
    assert persistent.isPersisted() is True
    assert persistent.getSessionDir() == os.fspath(tmp_path / "configured")
    assert persistent.getSessionFile() == os.fspath(absent)
    assert not absent.exists()

    invalid = tmp_path / "invalid.jsonl"
    invalid_bytes = b'{"type":"not-session","id":"bad"}\n'
    invalid.write_bytes(invalid_bytes)
    with pytest.raises(ValueError, match="not a valid omh session"):
        persistent.setSessionFile(os.fspath(invalid))
    assert invalid.read_bytes() == invalid_bytes


def test_fork_copies_parseable_physical_entries_without_rewriting_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.jsonl"
    source_bytes = (
        _json_bytes(
            {
                "type": "session",
                "version": 3,
                "id": "source",
                "timestamp": "2026-08-24T00:00:00.000Z",
                "cwd": os.fspath(tmp_path / "source-project"),
            }
        )
        + b"\nmalformed\n"
        + _json_bytes(
            {
                "type": "model_change",
                "id": "copied",
                "parentId": None,
                "timestamp": "2026-08-24T00:00:01.000Z",
                "provider": "deepseek",
                "modelId": "copied-model",
            }
        )
    )
    source.write_bytes(source_bytes)

    forked = SessionManager.forkFrom(
        os.fspath(source),
        os.fspath(tmp_path / "target-project"),
        os.fspath(tmp_path / "forks"),
    )

    assert source.read_bytes() == source_bytes
    assert forked.getHeader().parentSession == os.fspath(source)
    assert forked.getHeader().cwd == os.fspath(tmp_path / "target-project")
    assert len(forked.getEntries()) == 1
    copied = forked.getEntries()[0]
    assert isinstance(copied, ModelChangeEntry)
    assert copied.id == "copied"
    assert copied.modelId == "copied-model"


def test_direct_rewrite_and_read_faults_propagate_raw_with_actual_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.touch()
    attempted: list[bytes] = []
    cutoff = 19

    def fail_rewrite(output: BinaryIO, data: bytes) -> None:
        attempted.append(data)
        output.write(data[:cutoff])
        raise OSError("rewrite cutoff")

    monkeypatch.setattr(session_manager_module, "_write_bytes", fail_rewrite)
    with pytest.raises(OSError, match="rewrite cutoff"):
        SessionManager.open(os.fspath(empty), cwdOverride=os.fspath(tmp_path))
    assert empty.read_bytes() == attempted[0][:cutoff]
    assert tuple(tmp_path.glob("*.tmp")) == ()

    valid = tmp_path / "valid.jsonl"
    valid.write_bytes(
        _json_bytes(
            {
                "type": "session",
                "version": 3,
                "id": "read-fault",
                "timestamp": "2026-08-24T00:00:00.000Z",
                "cwd": os.fspath(tmp_path),
            }
        )
        + b"\n"
    )
    valid_bytes = valid.read_bytes()

    def fail_read(_path: str) -> bytes:
        raise OSError("read cutoff")

    monkeypatch.setattr(session_manager_module, "_read_bytes", fail_read)
    with pytest.raises(OSError, match="read cutoff"):
        SessionManager.open(os.fspath(valid))
    assert valid.read_bytes() == valid_bytes


def test_recovered_tree_orders_siblings_by_timestamp(tmp_path: Path) -> None:
    session_file = tmp_path / "tree.jsonl"
    values = [
        {
            "type": "session",
            "version": 3,
            "id": "tree",
            "timestamp": "2026-08-24T00:00:00.000Z",
            "cwd": os.fspath(tmp_path),
        },
        {
            "type": "model_change",
            "id": "root",
            "parentId": None,
            "timestamp": "2026-08-24T00:00:00.000Z",
            "provider": "deepseek",
            "modelId": "root",
        },
        {
            "type": "model_change",
            "id": "newer",
            "parentId": "root",
            "timestamp": "2026-08-24T00:00:02.000Z",
            "provider": "deepseek",
            "modelId": "newer",
        },
        {
            "type": "model_change",
            "id": "older",
            "parentId": "root",
            "timestamp": "2026-08-24T00:00:01.000Z",
            "provider": "deepseek",
            "modelId": "older",
        },
    ]
    session_file.write_bytes(
        b"".join(_json_bytes(value) + b"\n" for value in values)
    )

    tree = SessionManager.open(os.fspath(session_file)).getTree()
    assert [child.entry.id for child in tree[0].children] == ["older", "newer"]


def test_continue_recent_directory_failure_returns_fresh_lazy_manager(
    tmp_path: Path,
) -> None:
    not_a_directory = tmp_path / "sessions-as-file"
    not_a_directory.write_text("not a directory")

    manager = SessionManager.continueRecent(
        os.fspath(tmp_path / "project"), os.fspath(not_a_directory)
    )

    assert manager.getSessionDir() == os.fspath(not_a_directory)
    assert manager.getEntries() == ()
    assert manager.getSessionFile() is not None
    assert not Path(manager.getSessionFile() or "").exists()


def test_discovery_top_level_enumeration_failure_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()

    def fail_glob(_self: Path, _pattern: str) -> object:
        raise OSError("enumeration cutoff")

    monkeypatch.setattr(Path, "glob", fail_glob)

    assert asyncio.run(
        SessionManager.list(os.fspath(tmp_path / "project"), os.fspath(sessions))
    ) == ()
    assert asyncio.run(SessionManager.listAll(os.fspath(sessions))) == ()


def test_ticket_14_matrix_records_fault_recovery_and_concurrency() -> None:
    root = Path(__file__).parents[2]
    matrix = json.loads((root / "conformance/obligation-matrix.json").read_text())
    corpus = json.loads(
        (root / "conformance/reference-observation-corpus.json").read_text()
    )
    required = {
        "omh-v0.session-persistence-fault-continuity": (
            "reference.session-persistence-fault-continuity"
        ),
        "omh-v0.session-best-effort-recovery": (
            "reference.session-best-effort-recovery"
        ),
        "omh-v0.session-stale-multi-manager-append": (
            "reference.session-stale-multi-manager-append"
        ),
    }
    rows = {row["id"]: row for row in matrix["obligations"]}
    cases = {case["id"]: case for case in corpus["cases"]}
    assert required.keys() <= rows.keys()
    assert set(required.values()) <= cases.keys()
    for obligation, corpus_case in required.items():
        assert rows[obligation]["corpusCase"] == corpus_case
        assert rows[obligation]["executableCases"] == [
            "ticket-14-session",
            "ticket-14-installed",
        ]
        assert cases[corpus_case]["obligation"] == obligation


def test_explicit_default_discovery_path_is_not_a_custom_cwd_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", os.fspath(tmp_path / "home"))
    project = tmp_path / "project"
    default_manager = SessionManager.create(os.fspath(project))
    default_dir = Path(default_manager.getSessionDir())
    foreign = default_dir / "foreign.jsonl"
    foreign.write_bytes(
        _json_bytes(
            {
                "type": "session",
                "version": 3,
                "id": "foreign",
                "timestamp": "2026-08-24T00:00:00.000Z",
                "cwd": os.fspath(tmp_path / "different-project"),
            }
        )
        + b"\n"
    )

    rows = asyncio.run(SessionManager.list(os.fspath(project), os.fspath(default_dir)))
    assert [row.id for row in rows] == ["foreign"]
    recent = SessionManager.continueRecent(os.fspath(project), os.fspath(default_dir))
    assert recent.getSessionId() == "foreign"
    assert recent.usesDefaultSessionDir() is True


def test_recovery_uses_lf_as_the_physical_line_boundary(tmp_path: Path) -> None:
    session_file = tmp_path / "physical-lines.jsonl"
    header = _json_bytes(
        {
            "type": "session",
            "version": 3,
            "id": "physical-lines",
            "timestamp": "2026-08-24T00:00:00.000Z",
            "cwd": os.fspath(tmp_path),
        }
    )
    hidden_value = _json_bytes(
        {
            "type": "model_change",
            "id": "must-not-parse",
            "parentId": None,
            "timestamp": "2026-08-24T00:00:01.000Z",
            "provider": "deepseek",
            "modelId": "hidden-after-cr",
        }
    )
    session_file.write_bytes(header + b"\ninvalid\r" + hidden_value)

    assert SessionManager.open(os.fspath(session_file)).getEntries() == ()


def test_recovered_tree_keeps_a_forward_parent_link(tmp_path: Path) -> None:
    session_file = tmp_path / "forward-parent.jsonl"
    values = (
        {
            "type": "session",
            "version": 3,
            "id": "forward-parent",
            "timestamp": "2026-08-24T00:00:00.000Z",
            "cwd": os.fspath(tmp_path),
        },
        {
            "type": "model_change",
            "id": "child",
            "parentId": "root",
            "timestamp": "2026-08-24T00:00:01.000Z",
            "provider": "deepseek",
            "modelId": "child",
        },
        {
            "type": "model_change",
            "id": "root",
            "parentId": None,
            "timestamp": "2026-08-24T00:00:00.000Z",
            "provider": "deepseek",
            "modelId": "root",
        },
    )
    session_file.write_bytes(
        b"".join(_json_bytes(value) + b"\n" for value in values)
    )

    tree = SessionManager.open(os.fspath(session_file)).getTree()
    assert tree[0].entry.id == "root"
    assert [child.entry.id for child in tree[0].children] == ["child"]
