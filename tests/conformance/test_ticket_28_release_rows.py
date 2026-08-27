from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[2]
MATRIX = ROOT / "conformance/release-row-matrix.json"
PROVER = ROOT / "conformance/prove_release_row.py"
BUILDER = ROOT / "conformance/build_candidate_wheel.py"
MANIFEST = ROOT / "conformance/release-toolchain-manifest.json"
OBLIGATION_MATRIX = ROOT / "conformance/obligation-matrix.json"
CORPUS = ROOT / "conformance/reference-observation-corpus.json"
PYTHON_313 = shutil.which("python3.13")


def _run_prover(*args: str, timeout: int = 240) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, os.fspath(PROVER), *args),
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


SELECTED_ROWS = [
    "macOS 26|arm64|CPython 3.12",
    "macOS 26|arm64|CPython 3.13",
]


def test_release_matrix_contains_exactly_both_selected_rows() -> None:
    matrix = json.loads(MATRIX.read_text())
    assert matrix["schemaVersion"] == 1
    rows = matrix["rows"]
    assert [row["id"] for row in rows] == SELECTED_ROWS
    assert len(rows) == 2
    assert [row["osFamily"] for row in rows] == [
        "macOS 26",
        "macOS 26",
    ]
    assert [row["architecture"] for row in rows] == [
        "arm64",
        "arm64",
    ]
    assert [row["cpythonMinor"] for row in rows] == ["3.12", "3.13"]
    assert matrix["unselected"]["inferredSupport"] is False


def test_identify_accepts_only_a_selected_release_row() -> None:
    completed = _run_prover("identify")
    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout)
    assert observed["selected"] is True
    assert observed["row"] in SELECTED_ROWS
    assert observed["osFamily"] == "macOS 26"
    assert observed["architecture"] == "arm64"
    assert observed["cpythonMinor"] in {"3.12", "3.13"}
    assert observed["cpythonImplementation"] == "CPython"
    assert observed["osPointRelease"]
    assert observed["osBuild"]
    assert observed["cpythonPatch"]
    assert observed["runner"]


@pytest.mark.parametrize(
    ("identity", "needle"),
    (
        ("windows", "outside v0"),
        ("macos-15", "outside v0"),
        ("macos-x86_64", "outside v0"),
        ("ubuntu-22.04", "outside v0"),
        ("ubuntu-24.04", "outside v0"),
        ("linux-arm64", "outside v0"),
        ("pypy", "outside v0"),
        ("cpython-3.11", "outside v0"),
        ("cpython-3.14", "outside v0"),
        ("free-threaded", "outside v0"),
    ),
)
def test_unselected_environments_remain_outside_v0(identity: str, needle: str) -> None:
    completed = _run_prover("identify", "--inject-identity", identity)
    assert completed.returncode != 0, completed.stdout
    assert needle in completed.stderr.lower()
    assert "support" not in completed.stdout.lower() or "no support" in completed.stdout.lower()


def test_row_wheelhouses_are_lock_resolved_and_platform_specific() -> None:
    houses: dict[str, list[str]] = {}
    for row in SELECTED_ROWS:
        completed = _run_prover(
            "wheelhouse",
            "--row",
            row,
            "--repository",
            os.fspath(ROOT),
        )
        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout)
        assert payload["row"] == row
        names = [item["filename"] for item in payload["packages"]]
        assert names
        assert all(name.endswith(".whl") for name in names)
        assert all(len(item["sha256"]) == 64 for item in payload["packages"])
        houses[row] = names
        assert any("google_re2" in name for name in names)

    macos_312 = houses[SELECTED_ROWS[0]]
    macos_313 = houses[SELECTED_ROWS[1]]
    assert any("macosx" in name and "arm64" in name for name in macos_312)
    assert any("macosx" in name and "arm64" in name for name in macos_313)
    assert not any("manylinux" in name for name in macos_312)
    assert any("cp312" in name for name in macos_312 if "google_re2" in name)
    assert any("cp313" in name for name in macos_313 if "google_re2" in name)


@pytest.fixture(scope="session")
def candidate_wheel(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, str]:
    work = tmp_path_factory.mktemp("candidate-build")
    completed = subprocess.run(
        (
            sys.executable,
            os.fspath(BUILDER),
            "build",
            "--repository",
            os.fspath(ROOT),
            "--manifest",
            os.fspath(MANIFEST),
            "--work-root",
            os.fspath(work),
        ),
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(completed.stdout)
    wheel = work / "candidate" / evidence["wheelFilename"]
    assert wheel.is_file()
    return wheel, evidence["wheelSha256"], evidence["candidateCommit"]


def test_removed_ubuntu_row_cannot_be_proven(tmp_path: Path) -> None:
    dummy = tmp_path / "omh-0.1.0-py3-none-any.whl"
    dummy.write_bytes(b"not-the-candidate")
    completed = _run_prover(
        "prove",
        "--row",
        "Ubuntu 24.04|x86_64|CPython 3.12",
        "--repository",
        os.fspath(ROOT),
        "--work-root",
        os.fspath(tmp_path / "ubuntu"),
        "--wheel",
        os.fspath(dummy),
        "--wheel-sha256",
        "0" * 64,
        "--skip-suite",
    )
    assert completed.returncode != 0, completed.stdout
    assert "does not match" in completed.stderr.lower()


def test_matching_row_records_offline_install_and_environment_evidence(
    tmp_path: Path, candidate_wheel: tuple[Path, str, str]
) -> None:
    wheel, digest, commit = candidate_wheel
    work = tmp_path / "macos-312"
    completed = _run_prover(
        "prove",
        "--row",
        SELECTED_ROWS[0],
        "--repository",
        os.fspath(ROOT),
        "--work-root",
        os.fspath(work),
        "--wheel",
        os.fspath(wheel),
        "--wheel-sha256",
        digest,
        "--skip-suite",
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(completed.stdout)
    assert evidence["row"] == SELECTED_ROWS[0]
    assert evidence["wheelFilename"] == wheel.name
    assert evidence["wheelSha256"] == digest
    assert evidence["candidateCommit"] == commit
    assert evidence["os"]["family"] == "macOS 26"
    assert evidence["os"]["architecture"] == "arm64"
    assert evidence["os"]["productVersion"]
    assert evidence["os"]["buildVersion"]
    assert evidence["cpython"]["implementation"] == "CPython"
    assert evidence["cpython"]["minor"] == "3.12"
    assert evidence["cpython"]["patch"].startswith("3.12.")
    assert evidence["runner"]
    assert evidence["install"]["offline"] is True
    assert evidence["install"]["onlyBinary"] is True
    assert evidence["install"]["checkoutImport"] is False
    assert evidence["wheelhouse"]["packages"]
    assert evidence["wheelhouse"]["identity"]
    assert evidence["conformance"]["matrixSha256"] == sha256_file(OBLIGATION_MATRIX)
    assert evidence["conformance"]["corpusSha256"] == sha256_file(CORPUS)
    assert evidence["conformance"]["lockSha256"] == sha256_file(ROOT / "uv.lock")
    assert evidence["inferredSupport"] is False
    assert evidence["publishRight"] is False
    assert evidence["waiver"] is False
    python = Path(evidence["install"]["python"])
    probe = subprocess.run(
        (
            os.fspath(python),
            "-I",
            "-c",
            "import oh_my_llm, pathlib;"
            "print(pathlib.Path(oh_my_llm.__file__).resolve())",
        ),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=work,
        timeout=30,
    )
    installed = Path(probe.stdout.splitlines()[0])
    assert python.parent.parent in installed.parents
    assert ROOT / "src" not in installed.parents


def sha256_file(path: Path) -> str:
    from hashlib import sha256

    return sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("fault", "needle"),
    (
        ("missing-prebuilt", "missing prebuilt"),
        ("source-fallback", "source"),
        ("checkout-import", "checkout"),
        ("differing-wheel", "differ"),
        ("lock-drift", "lock"),
        ("undeclared", "undeclared"),
    ),
)
def test_row_install_gate_fails_closed(
    tmp_path: Path,
    candidate_wheel: tuple[Path, str, str],
    fault: str,
    needle: str,
) -> None:
    wheel, digest, _commit = candidate_wheel
    completed = _run_prover(
        "prove",
        "--row",
        SELECTED_ROWS[0],
        "--repository",
        os.fspath(ROOT),
        "--work-root",
        os.fspath(tmp_path / f"fault-{fault}"),
        "--wheel",
        os.fspath(wheel),
        "--wheel-sha256",
        digest,
        "--skip-suite",
        "--inject-fault",
        fault,
    )
    assert completed.returncode != 0, completed.stdout
    assert needle in completed.stderr.lower()


def test_macos_cpython_313_row_installs_the_same_candidate_wheel(
    tmp_path: Path, candidate_wheel: tuple[Path, str, str]
) -> None:
    assert PYTHON_313 is not None
    wheel, digest, commit = candidate_wheel
    completed = _run_prover(
        "prove",
        "--row",
        SELECTED_ROWS[1],
        "--python",
        PYTHON_313,
        "--repository",
        os.fspath(ROOT),
        "--work-root",
        os.fspath(tmp_path / "macos-313"),
        "--wheel",
        os.fspath(wheel),
        "--wheel-sha256",
        digest,
        "--platform-only",
    )
    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(completed.stdout)
    assert evidence["row"] == SELECTED_ROWS[1]
    assert evidence["wheelSha256"] == digest
    assert evidence["candidateCommit"] == commit
    assert evidence["cpython"]["minor"] == "3.13"
    assert evidence["cpython"]["patch"].startswith("3.13.")
    names = [item["filename"] for item in evidence["wheelhouse"]["packages"]]
    assert any("cp313" in name and "google_re2" in name for name in names)
    assert evidence["conformance"]["platformEvidence"] is True


def test_matching_row_runs_real_platform_resource_evidence(
    tmp_path: Path, candidate_wheel: tuple[Path, str, str]
) -> None:
    wheel, digest, _commit = candidate_wheel
    completed = _run_prover(
        "prove",
        "--row",
        SELECTED_ROWS[0],
        "--repository",
        os.fspath(ROOT),
        "--work-root",
        os.fspath(tmp_path / "platform"),
        "--wheel",
        os.fspath(wheel),
        "--wheel-sha256",
        digest,
        "--platform-only",
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(completed.stdout)
    assert evidence["conformance"]["platformEvidence"] is True
    assert evidence["conformance"]["suiteRan"] is False
    assert evidence["waiver"] is False


def _row_evidence(
    row: str,
    *,
    digest: str,
    commit: str,
    matrix_sha: str,
    corpus_sha: str,
    lock_sha: str,
    suite_ran: bool = True,
    platform: bool = True,
    waiver: bool = False,
    checkout: bool = False,
    filename: str = "omh-0.1.0-py3-none-any.whl",
) -> dict[object, object]:
    family, architecture, interpreter = row.split("|")
    minor = interpreter.rsplit(" ", 1)[1]
    return {
        "row": row,
        "candidateCommit": commit,
        "wheelFilename": filename,
        "wheelSha256": digest,
        "os": {
            "family": family,
            "architecture": architecture,
            "productVersion": "point",
            "buildVersion": "build",
        },
        "cpython": {
            "implementation": "CPython",
            "minor": minor,
            "patch": f"{minor}.0",
        },
        "runner": f"runner-{row}",
        "install": {
            "offline": True,
            "onlyBinary": True,
            "checkoutImport": checkout,
            "python": "/tmp/python",
        },
        "wheelhouse": {"packages": [{"filename": "dep.whl", "sha256": "ab"}], "identity": f"house-{row}"},
        "conformance": {
            "matrixSha256": matrix_sha,
            "corpusSha256": corpus_sha,
            "lockSha256": lock_sha,
            "suiteRan": suite_ran,
            "platformEvidence": platform,
        },
        "inferredSupport": False,
        "publishRight": False,
        "waiver": waiver,
    }


def test_bind_requires_both_matching_results(tmp_path: Path) -> None:
    digest = "a" * 64
    commit = "candidate"
    matrix_sha = sha256_file(OBLIGATION_MATRIX)
    corpus_sha = sha256_file(CORPUS)
    lock_sha = sha256_file(ROOT / "uv.lock")
    paths: list[Path] = []
    for index, row in enumerate(SELECTED_ROWS):
        path = tmp_path / f"row-{index}.json"
        path.write_text(json.dumps(_row_evidence(row, digest=digest, commit=commit, matrix_sha=matrix_sha, corpus_sha=corpus_sha, lock_sha=lock_sha)))
        paths.append(path)
    completed = _run_prover(
        "bind",
        "--repository",
        os.fspath(ROOT),
        *[os.fspath(path) for path in paths],
    )
    assert completed.returncode == 0, completed.stderr
    bound = json.loads(completed.stdout)
    assert bound["rows"] == SELECTED_ROWS
    assert bound["wheelSha256"] == digest
    assert bound["candidateCommit"] == commit
    assert bound["readyForReleaseEvidenceBundle"] is True
    assert bound["inferredSupport"] is False
    assert bound["publishRight"] is False

    missing = _run_prover(
        "bind",
        "--repository",
        os.fspath(ROOT),
        *[os.fspath(path) for path in paths[:1]],
    )
    assert missing.returncode != 0
    assert "two" in missing.stderr.lower()

    skipped = tmp_path / "skipped.json"
    skipped.write_text(
        json.dumps(
            _row_evidence(
                SELECTED_ROWS[1],
                digest=digest,
                commit=commit,
                matrix_sha=matrix_sha,
                corpus_sha=corpus_sha,
                lock_sha=lock_sha,
                suite_ran=False,
            )
        )
    )
    waived = _run_prover(
        "bind",
        "--repository",
        os.fspath(ROOT),
        *[os.fspath(path) for path in paths[:1]],
        os.fspath(skipped),
    )
    assert waived.returncode != 0
    assert "skip" in waived.stderr.lower() or "suite" in waived.stderr.lower()

    source_tree = tmp_path / "source.json"
    source_tree.write_text(
        json.dumps(
            _row_evidence(
                SELECTED_ROWS[1],
                digest=digest,
                commit=commit,
                matrix_sha=matrix_sha,
                corpus_sha=corpus_sha,
                lock_sha=lock_sha,
                checkout=True,
            )
        )
    )
    checkout = _run_prover(
        "bind",
        "--repository",
        os.fspath(ROOT),
        *[os.fspath(path) for path in paths[:1]],
        os.fspath(source_tree),
    )
    assert checkout.returncode != 0
    assert "checkout" in checkout.stderr.lower() or "source" in checkout.stderr.lower()

    waived_row = tmp_path / "waived.json"
    waived_row.write_text(
        json.dumps(
            _row_evidence(
                SELECTED_ROWS[1],
                digest=digest,
                commit=commit,
                matrix_sha=matrix_sha,
                corpus_sha=corpus_sha,
                lock_sha=lock_sha,
                waiver=True,
            )
        )
    )
    waiver = _run_prover(
        "bind",
        "--repository",
        os.fspath(ROOT),
        *[os.fspath(path) for path in paths[:1]],
        os.fspath(waived_row),
    )
    assert waiver.returncode != 0
    assert "waiv" in waiver.stderr.lower()
