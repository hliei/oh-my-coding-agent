from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from typing import get_type_hints

import oh_my_coding_agent
import oh_my_core
import oh_my_llm
import oh_my_llm.providers.deepseek as deepseek


ROOT = Path(__file__).parents[2]
ISSUES = ROOT / ".scratch/omh-v0/issues"
SPEC = ROOT / ".scratch/omh-v0/spec.md"
MATRIX = ROOT / "conformance/obligation-matrix.json"
CORPUS = ROOT / "conformance/reference-observation-corpus.json"
REFERENCE_REVISION = "0e6909f050eeb15e8f6c05185511f3788357ddb3"

_PUBLIC_ROOTS = {
    "oh_my_llm": {
        "AbortSignal",
        "AuthResult",
        "AssistantMessage",
        "AssistantMessageDoneEvent",
        "AssistantMessageErrorEvent",
        "AssistantMessageEvent",
        "AssistantMessageStartEvent",
        "AssistantMessageTextDeltaEvent",
        "AssistantMessageTextEndEvent",
        "AssistantMessageTextStartEvent",
        "AssistantMessageToolCallDeltaEvent",
        "AssistantMessageToolCallEndEvent",
        "AssistantMessageToolCallStartEvent",
        "Context",
        "EventStream",
        "FauxProviderHandle",
        "FauxResponseFactory",
        "FauxResponseStep",
        "JSONValue",
        "LifecycleError",
        "LifecycleErrorCode",
        "Model",
        "Models",
        "ModelsError",
        "ModelsErrorCode",
        "Message",
        "MutableModels",
        "Provider",
        "SimpleStreamOptions",
        "StopReason",
        "StreamOptions",
        "TextContent",
        "Tool",
        "ToolCall",
        "ToolResultMessage",
        "Usage",
        "UsageCost",
        "UserMessage",
        "createModels",
        "fauxAssistantMessage",
        "fauxProvider",
        "fauxText",
        "fauxToolCall",
        "validateToolArguments",
        "validateToolCall",
    },
    "oh_my_core": {
        "Agent",
        "AgentContext",
        "AgentEvent",
        "AgentEventSink",
        "AgentLoopConfig",
        "AgentMessage",
        "AgentOptions",
        "AgentState",
        "AgentTool",
        "AgentToolResult",
        "AgentToolUpdateCallback",
        "StreamFn",
        "ToolExecutionMode",
        "agentLoop",
        "agentLoopContinue",
        "runAgentLoop",
        "runAgentLoopContinue",
    },
    "oh_my_coding_agent": {
        "AgentSession",
        "AgentSessionEvent",
        "AgentSessionEventListener",
        "BranchSummaryEntry",
        "CURRENT_SESSION_VERSION",
        "CompactionEntry",
        "CompactionResult",
        "CreateAgentSessionOptions",
        "CreateAgentSessionResult",
        "CustomEntry",
        "CustomMessageEntry",
        "ExtensionAPI",
        "ExtensionContext",
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
        "createAgentSession",
    },
}

_PUBLIC_MEMBERS = {
    oh_my_llm.Models: {
        "complete",
        "completeSimple",
        "getAuth",
        "getModel",
        "getModels",
        "getProvider",
        "getProviders",
        "stream",
        "streamSimple",
    },
    oh_my_llm.MutableModels: {"setProvider"},
    oh_my_llm.Provider: {"id", "name"},
    oh_my_llm.FauxProviderHandle: {
        "appendResponses",
        "getModel",
        "getPendingResponseCount",
        "models",
        "provider",
        "setResponses",
        "state",
    },
    oh_my_llm.AbortSignal: {"aborted", "wait"},
    oh_my_llm.EventStream: {"aclose", "result"},
    oh_my_core.Agent: {
        "abort",
        "continue_",
        "prompt",
        "reset",
        "signal",
        "state",
        "subscribe",
        "waitForIdle",
    },
    oh_my_coding_agent.SessionManager: {
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
    },
    oh_my_coding_agent.AgentSession: {
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
    },
    oh_my_coding_agent.ExtensionAPI: {"on", "registerTool"},
    oh_my_coding_agent.ExtensionContext: {
        "cwd",
        "messages",
        "model",
        "sessionId",
        "signal",
    },
}

_SOURCE_RUNNERS = {
    "ticket-02-values": "test_ticket_02_values.py",
    "ticket-03-tools": "test_ticket_03_tools.py",
    "ticket-04-streams": "test_ticket_04_streams.py",
    "ticket-05-tool-turn": "test_ticket_05_tool_turn.py",
    "ticket-06-tool-batches": "test_ticket_06_tool_batches.py",
    "ticket-07-agent": "test_ticket_07_agent.py",
    "ticket-08-agent-cancellation": "test_ticket_08_agent_cancellation.py",
    "ticket-09-deepseek": "test_ticket_09_deepseek.py",
    "ticket-10-deepseek": "test_ticket_10_deepseek_tool_usage.py",
    "ticket-11-deepseek-failures": "test_ticket_11_deepseek_failures.py",
    "ticket-12-session": "test_ticket_12_session_creation.py",
    "ticket-13-session": "test_ticket_13_session_prompt.py",
    "ticket-14-session": "test_ticket_14_session_persistence.py",
    "ticket-15-session": "test_ticket_15_session_lifecycle.py",
    "ticket-16-read": "test_ticket_16_builtin_read.py",
    "ticket-17-write": "test_ticket_17_builtin_write.py",
    "ticket-18-edit": "test_ticket_18_builtin_edit.py",
    "ticket-19-bash": "test_ticket_19_builtin_bash.py",
    "ticket-20-resources": "test_ticket_20_prompt_resources.py",
    "ticket-21-extensions": "test_ticket_21_python_extensions.py",
    "ticket-22-journey": "test_ticket_22_programmatic_journey.py",
    "ticket-23-one-shot": "test_ticket_23_one_shot.py",
    "ticket-24-repl": "test_ticket_24_interactive_repl.py",
    "ticket-25-signals": "test_ticket_25_command_signals.py",
    "ticket-26-closure": "test_ticket_26_closure.py",
    "ticket-27-candidate-wheel": "test_ticket_27_candidate_wheel.py",
}


def _ledger_inventory() -> tuple[set[str], set[str], set[str]]:
    spec = SPEC.read_text()
    pa_line = next(
        line for line in spec.splitlines() if line.startswith("- The closed Python Adaptation keys")
    )
    abd_line = next(
        line
        for line in spec.splitlines()
        if line.startswith("- The closed active Accepted Behavioral Deviation keys")
    )
    tombstone_text = abd_line.split("The withdrawn keys", 1)[1]
    tombstones = set(re.findall(r"`(ABD:[^`]+)`", tombstone_text))
    adaptations = set(re.findall(r"`(PA:[^`]+)`", pa_line))
    deviations = set(re.findall(r"`(ABD:[^`]+)`", abd_line)) - tombstones
    return adaptations, deviations, tombstones


def _ledger_sections() -> dict[str, tuple[Path, str]]:
    sections: dict[str, tuple[Path, str]] = {}
    heading = re.compile(r"^#{3,4} `((?:PA|ABD):[^`]+)`[^\n]*$", re.MULTILINE)
    for path in sorted(ISSUES.glob("*.md")):
        source = path.read_text()
        matches = list(heading.finditer(source))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
            key = match.group(1)
            assert key not in sections, key
            sections[key] = (path, source[match.start() : end])
    return sections


def test_parity_ledger_authority_matrix_and_corpus_are_closed() -> None:
    adaptations, deviations, tombstones = _ledger_inventory()
    active = adaptations | deviations
    sections = _ledger_sections()
    matrix = json.loads(MATRIX.read_text())
    corpus = json.loads(CORPUS.read_text())
    rows = matrix["obligations"]
    cases = {case["id"]: case for case in corpus["cases"]}
    evidence_keys = {
        row["evidenceClass"]
        for row in rows
        if row["evidenceClass"].startswith(("PA:", "ABD:"))
    }

    assert len(adaptations) == 11
    assert len(deviations) == 48
    assert len(tombstones) == 6
    assert active == sections.keys()
    assert active == evidence_keys

    for key in active:
        authority_path, section = sections[key]
        assert all(f"{number}. **" in section for number in range(1, 7)), key
        owned_rows = [row for row in rows if row["evidenceClass"] == key]
        assert owned_rows, key
        assert any(
            row["authority"].startswith(
                f".scratch/omh-v0/issues/{authority_path.name}#"
            )
            for row in owned_rows
        ), key
        for row in owned_rows:
            case = cases[row["corpusCase"]]
            assert case["obligation"] == row["id"]
            assert case["referenceRevision"] == REFERENCE_REVISION
            assert case["referenceCitations"]
            assert "omhExpectation" in case

    authority_graph = MATRIX.read_text() + CORPUS.read_text()
    assert all(tombstone not in authority_graph for tombstone in tombstones)


def test_public_roots_child_path_and_factory_members_are_closed() -> None:
    modules = (oh_my_llm, oh_my_core, oh_my_coding_agent)
    assert {
        module.__name__: set(module.__all__) for module in modules
    } == _PUBLIC_ROOTS
    assert oh_my_coding_agent.CURRENT_SESSION_VERSION == 3
    assert deepseek.__all__ == ("deepseekProvider",)
    assert {
        name for name in vars(deepseek) if not name.startswith("_")
    } == {"deepseekProvider"}
    assert get_type_hints(deepseek.deepseekProvider)["return"] is oh_my_llm.Provider
    for public_type, expected in _PUBLIC_MEMBERS.items():
        assert {
            name for name in vars(public_type) if not name.startswith("_")
        } == expected

    matrix = json.loads(MATRIX.read_text())
    row = next(
        item
        for item in matrix["obligations"]
        if item["id"] == "omh-v0.public-surface-closure"
    )
    coverage = row["coverage"]
    assert coverage["publicPaths"] == [
        "oh_my_llm",
        "oh_my_llm.providers.deepseek",
        "oh_my_core",
        "oh_my_coding_agent",
    ]
    assert {
        name: set(values) for name, values in coverage["publicRoots"].items()
    } == _PUBLIC_ROOTS
    expected_members = {
        f"{public_type.__module__.split('.')[0]}.{public_type.__name__}": values
        for public_type, values in _PUBLIC_MEMBERS.items()
    }
    assert {
        name: set(values) for name, values in coverage["publicMembers"].items()
    } == expected_members


def test_matrix_and_corpus_form_one_fail_closed_authority_graph() -> None:
    matrix = json.loads(MATRIX.read_text())
    corpus = json.loads(CORPUS.read_text())
    rows = matrix["obligations"]
    cases = corpus["cases"]
    row_ids = [row["id"] for row in rows]
    case_ids = [case["id"] for case in cases]
    capture_ids = [case["captureCaseId"] for case in cases]
    executable_case_ids = [
        case_id for row in rows for case_id in row["executableCases"]
    ]

    assert matrix["schemaVersion"] == 1
    assert corpus["schemaVersion"] == 1
    assert corpus["referenceRevision"] == REFERENCE_REVISION
    assert len(row_ids) == len(set(row_ids))
    assert len(case_ids) == len(set(case_ids))
    assert len(capture_ids) == len(set(capture_ids))
    assert len(executable_case_ids) == len(set(executable_case_ids))
    assert set(executable_case_ids) == set(case_ids)
    assert {row["corpusCase"] for row in rows} == set(case_ids)
    assert {case["obligation"] for case in cases} == set(row_ids)
    closure = matrix["closure"]
    assert closure["knownParityGaps"] == []
    assert closure["unresolvedParityGaps"] == []
    assert set(closure["requiredJourneyObligations"]) <= set(row_ids)
    assert set(closure["requiredLocalObligations"]) <= set(row_ids)

    decisions = tuple(sorted(ISSUES.glob("*.md")))
    assert len(decisions) == 13
    assert all("Status: resolved" in decision.read_text() for decision in decisions)

    cases_by_id = {case["id"]: case for case in cases}
    accepted_not_applicable = {
        "product-scope",
        "packaging",
        "platform",
        "release-process",
    }
    for row in rows:
        assert set(row["observations"]) == {"A", "L", "T", "E", "C"}
        assert row["authority"]
        assert row["publicInterface"]
        assert row["scenario"]
        assert row["comparator"]
        assert row["normalization"]
        assert row["executableCases"] == [row["corpusCase"]]
        assert row["executableRunners"]
        case = cases_by_id[row["corpusCase"]]
        assert case["referenceRevision"] == REFERENCE_REVISION
        assert case["comparator"]
        assert case["normalization"]

        evidence_class = row["evidenceClass"]
        if evidence_class == "local-release":
            applicability = row["referenceApplicability"]
            assert applicability["status"] == "not_applicable"
            assert applicability["reason"] in accepted_not_applicable
            assert case["observations"] == {
                "reference": "not_applicable",
                "reason": applicability["reason"],
            }
            assert "omhExpectation" in case
        else:
            assert "referenceApplicability" not in row
            assert case["referenceCitations"]
            if evidence_class.startswith(("PA:", "ABD:")):
                assert "omhExpectation" in case
            else:
                assert evidence_class == "exact-parity"


def test_every_authority_and_executable_case_resolves_without_orphans() -> None:
    matrix = json.loads(MATRIX.read_text())
    authorities = [row["authority"] for row in matrix["obligations"]]
    authority_pattern = re.compile(
        r"(?P<path>\.scratch/omh-v0/issues/[^ #]+\.md)#(?P<anchor>[^ ]+)"
    )
    for authority in authorities:
        matches = list(authority_pattern.finditer(authority))
        assert matches, authority
        for match in matches:
            path = ROOT / match.group("path")
            assert path.is_file(), authority
            source = path.read_text()
            raw_anchor = match.group("anchor").rstrip(",.;")
            anchor = raw_anchor.lower()
            ledger_key = raw_anchor if raw_anchor.startswith(("PA:", "ABD:")) else None
            if ledger_key is not None:
                assert re.search(
                    rf"^#{{3,4}} `{re.escape(ledger_key)}`(?: |$)",
                    source,
                    re.MULTILINE,
                ), authority
                continue
            heading_slugs = {
                re.sub(
                    r" +",
                    "-",
                    re.sub(r"[^\w\- ]", "", heading.lower().replace("`", "")),
                )
                for heading in re.findall(r"^#{2,4} (.+)$", source, re.MULTILINE)
            }
            assert anchor in heading_slugs, authority

    executable_cases = {
        case
        for row in matrix["obligations"]
        for case in row["executableRunners"]
    }
    installed_test = (ROOT / "tests/conformance/test_ticket_01_installed.py").read_text()
    installed_runners = {case for case in executable_cases if case.endswith("-installed")}
    assert executable_cases == set(_SOURCE_RUNNERS) | installed_runners
    for runner, filename in _SOURCE_RUNNERS.items():
        path = ROOT / "tests/conformance" / filename
        assert path.is_file(), runner
        ticket = runner.split("-", 2)[1]
        assert path.name.startswith(f"test_ticket_{ticket}_"), runner
    for runner in installed_runners:
        runner_match = re.fullmatch(r"ticket-(\d{2})-installed", runner)
        assert runner_match is not None, runner
        scenario_name = f"ticket_{runner_match.group(1)}_scenario.py"
        assert (ROOT / "tests/conformance" / scenario_name).is_file(), runner
        assert scenario_name in installed_test, runner


def test_deterministic_suite_contains_no_timing_sleeps_skips_or_xfails() -> None:
    forbidden_calls: list[str] = []
    forbidden_marks: list[str] = []
    for path in sorted((ROOT / "tests/conformance").glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id in {"asyncio", "time"}
                    and node.func.attr == "sleep"
                ):
                    forbidden_calls.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr in {"skip", "skipif", "xfail"}:
                forbidden_marks.append(f"{path.name}:{node.lineno}")
    assert forbidden_calls == []
    assert forbidden_marks == []


def test_dependency_packaging_platform_and_release_policy_are_indexed_once() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["requires-python"] == ">=3.12,<3.14"
    assert project["project"]["dependencies"] == [
        "httpx==0.28.1",
        "google-re2==1.1.20251105",
        "PyYAML==6.0.3",
    ]
    assert project["project"]["scripts"] == {
        "omh": "oh_my_coding_agent._cli:main"
    }
    assert project["build-system"] == {
        "requires": ["hatchling==1.32.0"],
        "build-backend": "hatchling.build",
    }
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/oh_my_llm",
        "src/oh_my_core",
        "src/oh_my_coding_agent",
    ]

    matrix = json.loads(MATRIX.read_text())
    rows = {row["id"]: row for row in matrix["obligations"]}
    assert rows["omh-v0.locked-dependency-packaging-contract"][
        "referenceApplicability"
    ] == {"status": "not_applicable", "reason": "packaging"}
    assert rows["omh-v0.closed-release-row-selection"]["coverage"][
        "releaseRows"
    ] == [
        "macOS 26|arm64|CPython 3.12",
        "macOS 26|arm64|CPython 3.13",
        "Ubuntu 24.04|x86_64|CPython 3.12",
        "Ubuntu 24.04|x86_64|CPython 3.13",
    ]
    assert rows["omh-v0.deterministic-conformance-policy"][
        "referenceApplicability"
    ] == {"status": "not_applicable", "reason": "release-process"}
    assert rows["omh-v0.candidate-build-row-toolchain"][
        "executableRunners"
    ] == ["ticket-27-candidate-wheel"]
    assert rows["omh-v0.reproducible-universal-candidate-wheel"][
        "referenceApplicability"
    ] == {"status": "not_applicable", "reason": "packaging"}
    assert rows["omh-v0.offline-candidate-install-and-conformance"][
        "referenceApplicability"
    ] == {"status": "not_applicable", "reason": "packaging"}


def test_reference_corpus_generator_is_closed_without_omh_output() -> None:
    manifest = ROOT / "conformance/reference-capture-manifest.json"
    snapshot = ROOT / "conformance/reference-source-snapshot.tar.gz"
    manifest_value = json.loads(manifest.read_text())
    fixed_entries = [
        entry
        for entry in manifest_value["cases"]
        if entry["origin"] == "fixed-reference"
    ]
    fixed_ids = [entry["referenceCase"]["id"] for entry in fixed_entries]
    assert len(fixed_ids) == len(set(fixed_ids)) == 99
    assert all("observations" not in entry["referenceCase"] for entry in fixed_entries)
    assert all(isinstance(entry.get("captureDriver"), str) for entry in fixed_entries)
    drivers = {
        entry["referenceCase"]["id"]: entry["captureDriver"]
        for entry in fixed_entries
    }
    assert list(drivers.values()).count("source-citations") == 96
    assert drivers["reference.require-named-check-for-success"] == (
        "upstream-test:packages/agent/test/agent-loop.test.ts"
    )
    assert drivers["reference.deterministic-one-shot-terminal-carrier"] == (
        "upstream-test:packages/coding-agent/test/print-mode.test.ts"
    )
    assert drivers["reference.session-scoped-extension-modules"] == (
        "reference_capture_tests/session-scoped-extension-modules.test.ts"
    )

    runtime_capture = json.loads(
        (ROOT / "conformance/reference-runtime-capture.json").read_text()
    )
    captured_ids = [case["id"] for case in runtime_capture["cases"]]
    assert len(captured_ids) == len(set(captured_ids))
    assert set(captured_ids) == set(fixed_ids)
    corpus_by_id = {
        case["id"]: case for case in json.loads(CORPUS.read_text())["cases"]
    }
    assert all(
        captured["observations"] == corpus_by_id[captured["id"]]["observations"]
        for captured in runtime_capture["cases"]
    )

    generated: list[bytes] = []
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        for index in range(2):
            reference_export = root / f"reference-{index}"
            reference_export.mkdir()
            with tarfile.open(snapshot, "r:gz") as archive:
                archive.extractall(reference_export, filter="data")
            completed = subprocess.run(
                (
                    sys.executable,
                    os.fspath(ROOT / "conformance/regenerate_reference_corpus.py"),
                    os.fspath(manifest),
                    "--reference-export",
                    os.fspath(reference_export),
                    "--capture-output",
                    os.fspath(
                        ROOT / "conformance/reference-runtime-capture.json"
                    ),
                ),
                check=True,
                stdout=subprocess.PIPE,
                timeout=10,
            )
            generated.append(completed.stdout)
    assert generated == [CORPUS.read_bytes(), CORPUS.read_bytes()]

    harness = (ROOT / "conformance/capture_reference_corpus.mjs").read_text()
    assert 'const REQUIRED_NODE = "v22.19.0"' in harness
    assert 'runIsolated("npm", ["ci", "--offline"]' in harness
    assert 'const SANDBOX = "/usr/bin/sandbox-exec"' in harness
    assert "timeout: 120_000" in harness
    assert "capture_reference_source_cases.py" in harness
    assert "session-scoped-extension-modules.test.ts" in harness
    assert "regenerate_reference_corpus.py" in harness
    verifier = (ROOT / "conformance/verify_reference_capture.py").read_text()
    assert "for index in range(2)" in verifier
    assert "captures != [expected, expected]" in verifier
