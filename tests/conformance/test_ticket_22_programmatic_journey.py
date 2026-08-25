from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from ticket_22_scenario import (
    run_failure_boundaries,
    run_golden_journey,
    run_recovery_continuity,
    run_session_topology,
    run_verification_matrix,
)


ROOT = Path(__file__).parents[2]


def _expected(case_id: str) -> object:
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    case = next(item for item in corpus["cases"] if item["id"] == case_id)
    return case.get("omhExpectation", case["observations"])


def test_programmatic_journey_inspects_mutates_checks_and_reports(
    tmp_path: Path,
) -> None:
    before = os.environ.get("DEEPSEEK_API_KEY")
    actual = asyncio.run(run_golden_journey(tmp_path))

    assert actual == _expected("reference.programmatic-product-journey")
    assert os.environ.get("DEEPSEEK_API_KEY") == before


def test_named_check_failures_never_become_success_claims_and_can_recover(
    tmp_path: Path,
) -> None:
    actual = asyncio.run(run_verification_matrix(tmp_path))

    assert actual == _expected("reference.require-named-check-for-success")


def test_provider_failure_cancellation_busy_and_reuse_are_distinct(
    tmp_path: Path,
) -> None:
    actual = asyncio.run(run_failure_boundaries(tmp_path))

    assert actual == _expected("reference.programmatic-failure-boundaries")


def test_recovery_preserves_prefix_and_rebinds_current_inputs_without_replay(
    tmp_path: Path,
) -> None:
    actual = asyncio.run(run_recovery_continuity(tmp_path))

    assert actual == _expected("reference.programmatic-recovery-continuity")


def test_tree_fork_compaction_memory_and_skill_remain_publicly_observable(
    tmp_path: Path,
) -> None:
    actual = asyncio.run(run_session_topology(tmp_path))

    assert actual == _expected("reference.programmatic-session-topology")


def test_ticket_22_matrix_connects_each_journey_observation_to_authority() -> None:
    matrix = json.loads((ROOT / "conformance/obligation-matrix.json").read_text())
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    required = {
        "omh-v0.programmatic-product-journey": (
            "reference.programmatic-product-journey"
        ),
        "omh-v0.require-named-check-for-success": (
            "reference.require-named-check-for-success"
        ),
        "omh-v0.programmatic-failure-boundaries": (
            "reference.programmatic-failure-boundaries"
        ),
        "omh-v0.programmatic-session-topology": (
            "reference.programmatic-session-topology"
        ),
        "omh-v0.programmatic-recovery-continuity": (
            "reference.programmatic-recovery-continuity"
        ),
    }
    expected = {
        "omh-v0.programmatic-product-journey": {
            "authority": ".scratch/omh-v0/issues/01-choose-v0-product-journey.md#core-code-change-journey",
            "publicInterface": "installed oh_my_coding_agent.createAgentSession, AgentSession, and SessionManager",
            "observations": {
                "A": "nominal deepseek-v4-flash persistent untrusted Product Session admitted",
                "L": "ordered read, read, edit, bash, AgentEnd, and AgentSettled",
                "T": "ordinary report names the change and successful check",
                "E": "one bounded file mutation, post-mutation check, five local Provider requests, zero external network",
                "C": "complete append-only persistent tree remains public",
            },
            "comparator": "compare the bounded repository outcome, Tool order, terminal report, and persistent continuity while applying the named-check ABD only to the success-claim gate",
            "normalization": "product identity, Python Tool/value carriers, and ABD:require-named-check-for-success",
            "evidenceClass": "local-release",
        },
        "omh-v0.require-named-check-for-success": {
            "authority": ".scratch/omh-v0/issues/02-choose-v0-parity-policy.md#ABD:require-named-check-for-success",
            "publicInterface": "installed oh_my_coding_agent.AgentSession.prompt",
            "observations": {
                "A": "all five named-check fixtures admitted",
                "L": "no-check has no bash; negative and unrelated checks follow edit; correction stays in one Run",
                "T": "only the successful applicable post-mutation check produces verified",
                "E": "unavailable 127, failed 1, recovered and unrelated 0, and no Provider failure",
                "C": "all Runs settle and the corrected Run verifies without replacement",
            },
            "comparator": "match command identity, position after final mutation, Tool Result status, and final claim; normal completion and unrelated or failed commands never substitute",
            "normalization": "ABD:require-named-check-for-success",
            "evidenceClass": "ABD:require-named-check-for-success",
        },
        "omh-v0.programmatic-failure-boundaries": {
            "authority": ".scratch/omh-v0/issues/01-choose-v0-product-journey.md#required-non-golden-paths",
            "publicInterface": "installed oh_my_coding_agent.AgentSession.prompt and abort",
            "observations": {
                "A": "failure, cancellation, and reuse Runs admitted while overlap is busy-rejected",
                "L": "one AgentSettled per admitted Run",
                "T": "error, aborted, and stop remain distinct",
                "E": "busy adds no request and cancellation closes exactly one request",
                "C": "the same Session returns idle and reusable",
            },
            "comparator": "compare terminal classifications, request effects, overlap admission, and same-instance reuse",
            "normalization": "Python LifecycleError carrier and owned cancellation mechanics only",
            "evidenceClass": "local-release",
        },
        "omh-v0.programmatic-session-topology": {
            "authority": ".scratch/omh-v0/issues/01-choose-v0-product-journey.md#branch-fork-and-compaction-journeys and #ephemeral-session-journey and #companion-resource-scenarios",
            "publicInterface": "installed oh_my_coding_agent.AgentSession and SessionManager",
            "observations": {
                "A": "branch, fork, explicit/threshold/overflow compaction, in-memory mode, and trusted Skill admitted",
                "L": "manual compaction brackets once, threshold does not retry, and overflow marks one retry",
                "T": "source tree, fork entries, live/recovered compacted contexts, and Skill influence remain visible",
                "E": "source is append-only and unchanged by fork continuation; in-memory has no file, discovery, filesystem, or Adapter effect; automatic request counts are bounded",
                "C": "divergent leaves, independent fork append, in-memory history, and untrusted no-Skill core remain",
            },
            "comparator": "compare public tree/context projections, source and fork bytes, compaction retry bound, persistence effects, and trusted versus untrusted Skill behavior",
            "normalization": "Session carrier paths, Python value records, Project Resource Trust, and accepted compaction carrier mechanics",
            "evidenceClass": "local-release",
        },
        "omh-v0.programmatic-recovery-continuity": {
            "authority": ".scratch/omh-v0/issues/01-choose-v0-product-journey.md#best-effort-recovery-incomplete-runs-and-concurrency and .scratch/omh-v0/issues/08-choose-agent-session-surface.md#jsonl-persistence-and-recovery",
            "publicInterface": "installed oh_my_coding_agent.SessionManager.list and open, and AgentSession.prompt",
            "observations": {
                "A": "path/id recovery and fresh untrusted construction admitted",
                "L": "only the fresh correction read Tool runs",
                "T": "active history retains the incomplete prefix and ends normally",
                "E": "current instruction bytes and credential are rebound with no interrupted-effect replay",
                "C": "completion/disposal, malformed bytes, and every parseable prefix entry remain",
            },
            "comparator": "compare parsed-prefix entries, absent replay, fresh resource/auth binding, active-path continuation, and unchanged malformed bytes",
            "normalization": "Python JSONL carrier and product identity paths only",
            "evidenceClass": "local-release",
        },
    }
    rows = {row["id"]: row for row in matrix["obligations"]}
    cases = {case["id"]: case for case in corpus["cases"]}

    assert required.keys() <= rows.keys()
    assert set(required.values()) <= cases.keys()
    for obligation, corpus_case in required.items():
        row = rows[obligation]
        assert row["corpusCase"] == corpus_case
        assert row["executableCases"] == [
            "ticket-22-journey",
            "ticket-22-installed",
        ]
        assert {
            key: row[key]
            for key in (
                "authority",
                "publicInterface",
                "observations",
                "comparator",
                "normalization",
                "evidenceClass",
            )
        } == expected[obligation]
        assert cases[corpus_case]["obligation"] == obligation
        assert cases[corpus_case]["comparator"] == row["comparator"]
        assert cases[corpus_case]["normalization"] == row["normalization"]

    composed = required.keys() - {"omh-v0.require-named-check-for-success"}
    not_applicable = {
        "status": "not_applicable",
        "reason": "composed-journey-coverage",
    }
    for obligation in composed:
        row = rows[obligation]
        assert row["evidenceClass"] == "local-release"
        assert row["referenceApplicability"] == not_applicable
        assert cases[required[obligation]]["observations"] == {
            "reference": "not_applicable",
            "reason": "composed-journey-coverage",
        }
