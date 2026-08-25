from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[2]
REFERENCE_REVISION = "0e6909f050eeb15e8f6c05185511f3788357ddb3"


def _run(*command: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@pytest.fixture(scope="session")
def installed_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    isolated = tmp_path_factory.mktemp("installed-omh")
    clean_checkout = isolated / "checkout"
    distribution = isolated / "dist"
    environment = isolated / "venv"

    checkout_files = _run(
        "git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"
    ).stdout.split("\0")
    for relative in filter(None, checkout_files):
        source = ROOT / relative
        target = clean_checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    _run("uv", "lock", "--check", cwd=clean_checkout)
    _run(
        "uv",
        "build",
        "--wheel",
        "--out-dir",
        os.fspath(distribution),
        cwd=clean_checkout,
    )
    wheel = next(distribution.glob("omh-*.whl"))
    assert wheel.name.endswith("-py3-none-any.whl")
    _run("uv", "venv", "--python", sys.executable, os.fspath(environment))
    python = environment / "bin" / "python"
    _run(
        "uv",
        "pip",
        "install",
        "--python",
        os.fspath(python),
        os.fspath(wheel),
        cwd=isolated,
    )
    return python


def _assert_installed_scenario(installed_python: Path, scenario_name: str) -> None:
    scenario = Path(__file__).with_name(scenario_name)
    completed = _run(
        os.fspath(installed_python),
        "-I",
        os.fspath(scenario),
        cwd=installed_python.parent,
    )
    actual = json.loads(completed.stdout)
    corpus = json.loads((ROOT / "conformance/reference-observation-corpus.json").read_text())
    all_expected = {
        case["id"]: case.get("omhExpectation", case["observations"])
        for case in corpus["cases"]
    }
    assert actual == {case_id: all_expected[case_id] for case_id in actual}


def test_installed_wheel_completes_no_tool_faux_run(installed_python: Path) -> None:
    _assert_installed_scenario(installed_python, "ticket_01_scenario.py")


def test_installed_wheel_completes_tool_schema_and_callable_values(
    installed_python: Path,
) -> None:
    _assert_installed_scenario(installed_python, "ticket_03_scenario.py")


def test_installed_wheel_owns_streams_and_four_low_level_loops(
    installed_python: Path,
) -> None:
    _assert_installed_scenario(installed_python, "ticket_04_scenario.py")


def test_installed_wheel_executes_one_validated_tool_turn(
    installed_python: Path,
) -> None:
    _assert_installed_scenario(installed_python, "ticket_05_scenario.py")


def test_installed_wheel_settles_deterministic_tool_batches(
    installed_python: Path,
) -> None:
    _assert_installed_scenario(installed_python, "ticket_06_scenario.py")


def test_installed_wheel_makes_agent_stateful_and_reusable(
    installed_python: Path,
) -> None:
    _assert_installed_scenario(installed_python, "ticket_07_scenario.py")


def test_installed_wheel_settles_agent_cancellation_and_lifecycle_failure(
    installed_python: Path,
) -> None:
    _assert_installed_scenario(installed_python, "ticket_08_scenario.py")


def test_installed_wheel_streams_deepseek_text_through_deterministic_transport(
    installed_python: Path,
) -> None:
    _assert_installed_scenario(installed_python, "ticket_09_scenario.py")


def test_installed_wheel_finalizes_deepseek_tool_calls_and_usage(
    installed_python: Path,
) -> None:
    _assert_installed_scenario(installed_python, "ticket_10_scenario.py")


def test_installed_wheel_settles_deepseek_failures_and_cancellation(
    installed_python: Path,
) -> None:
    _assert_installed_scenario(installed_python, "ticket_11_scenario.py")


def test_installed_wheel_creates_and_disposes_empty_product_sessions(
    installed_python: Path,
) -> None:
    _assert_installed_scenario(installed_python, "ticket_12_scenario.py")


def test_installed_wheel_prompts_and_recovers_one_no_tool_product_session(
    installed_python: Path,
) -> None:
    _assert_installed_scenario(installed_python, "ticket_13_scenario.py")


def test_installed_wheel_preserves_session_persistence_failure_semantics(
    installed_python: Path,
) -> None:
    _assert_installed_scenario(installed_python, "ticket_14_scenario.py")


def test_installed_wheel_settles_product_session_lifecycle(
    installed_python: Path,
) -> None:
    _assert_installed_scenario(installed_python, "ticket_15_scenario.py")


def test_installed_wheel_registers_builtins_and_inspects_with_read(
    installed_python: Path,
) -> None:
    _assert_installed_scenario(installed_python, "ticket_16_scenario.py")


def test_installed_wheel_writes_exact_workspace_bytes(
    installed_python: Path,
) -> None:
    _assert_installed_scenario(installed_python, "ticket_17_scenario.py")


def test_installed_wheel_edits_workspace_text_exactly(
    installed_python: Path,
) -> None:
    _assert_installed_scenario(installed_python, "ticket_18_scenario.py")


def test_installed_wheel_runs_shell_command_in_workspace(
    installed_python: Path,
) -> None:
    _assert_installed_scenario(installed_python, "ticket_19_scenario.py")


def test_installed_wheel_loads_prompt_resources_into_fixed_system_prompt(
    installed_python: Path,
) -> None:
    _assert_installed_scenario(installed_python, "ticket_20_scenario.py")


def test_installed_wheel_loads_python_extension_lifecycle_and_tool(
    installed_python: Path,
) -> None:
    _assert_installed_scenario(installed_python, "ticket_21_scenario.py")


def test_first_conformance_authorities_are_closed_and_linked() -> None:
    matrix = json.loads((ROOT / "conformance/obligation-matrix.json").read_text())
    corpus = json.loads((ROOT / "conformance/reference-observation-corpus.json").read_text())

    obligations = matrix["obligations"]
    cases = corpus["cases"]
    obligation_ids = [row["id"] for row in obligations]
    case_ids = [case["id"] for case in cases]

    assert matrix["schemaVersion"] == 1
    assert corpus["schemaVersion"] == 1
    assert corpus["referenceRevision"] == REFERENCE_REVISION
    assert len(obligation_ids) == len(set(obligation_ids))
    assert len(case_ids) == len(set(case_ids))
    assert {row["corpusCase"] for row in obligations} == set(case_ids)
    assert {case["obligation"] for case in cases} == set(obligation_ids)

    required = {
        "omh-v0.public-import-roots",
        "omh-v0.no-tool-run-trace",
        "omh-v0.run-result-identity",
        "omh-v0.excluded-public-aliases",
    }
    assert required <= set(obligation_ids)
    ticket_01_rows = [row for row in obligations if row["id"] in required]
    assert all(row["executableCases"] == ["ticket-01-installed"] for row in ticket_01_rows)
    assert all(row["normalization"] for row in obligations)
    assert all(
        row["evidenceClass"] in {"exact-parity", "local-release"}
        or row["evidenceClass"].startswith(("PA:", "ABD:"))
        for row in obligations
    )
    local_release = next(
        row for row in obligations if row["evidenceClass"] == "local-release"
    )
    assert local_release["referenceApplicability"] == {
        "status": "not_applicable",
        "reason": "product-scope",
    }
    assert all(case["comparator"] and case["normalization"] for case in cases)
    assert all(case["captureCaseId"] for case in cases)
    assert all(case["referenceRevision"] == REFERENCE_REVISION for case in cases)
    assert all(case["referenceCitations"] for case in cases)
