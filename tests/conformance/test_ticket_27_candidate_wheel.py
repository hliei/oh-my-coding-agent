from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).parents[2]
BUILDER = ROOT / "conformance/build_candidate_wheel.py"
MANIFEST = ROOT / "conformance/release-toolchain-manifest.json"
MATRIX = ROOT / "conformance/obligation-matrix.json"
CORPUS = ROOT / "conformance/reference-observation-corpus.json"


def _run_builder(*args: str, cwd: Path | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, os.fspath(BUILDER), *args),
        cwd=ROOT if cwd is None else cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def test_release_toolchain_manifest_pins_the_candidate_build_row() -> None:
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["schemaVersion"] == 1
    assert manifest["row"] == "Candidate Build Row"
    assert manifest["os"]["family"] == "macOS 26"
    assert manifest["os"]["architecture"] == "arm64"
    assert manifest["os"]["productVersion"]
    assert manifest["os"]["buildVersion"]
    assert manifest["cpython"]["implementation"] == "CPython"
    assert manifest["cpython"]["version"].startswith("3.12.")
    assert len(manifest["cpython"]["sha256"]) == 64
    assert manifest["uv"]["version"]
    assert len(manifest["uv"]["sha256"]) == 64
    assert manifest["hatchling"] == "1.32.0"
    artifacts = manifest["buildArtifacts"]
    names = [item["filename"] for item in artifacts]
    assert "hatchling-1.32.0-py3-none-any.whl" in names
    for item in artifacts:
        path = ROOT / "conformance/build-artifacts" / item["filename"]
        assert path.is_file(), item["filename"]
        assert sha256(path.read_bytes()).hexdigest() == item["sha256"]
        assert item["filename"].endswith(".whl")


def test_two_isolated_candidate_builds_are_byte_identical(
    tmp_path: Path,
) -> None:
    work = tmp_path / "candidate-build"
    completed = _run_builder(
        "build",
        "--repository",
        os.fspath(ROOT),
        "--manifest",
        os.fspath(MANIFEST),
        "--work-root",
        os.fspath(work),
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(completed.stdout)
    wheels = [work / "dist-0" / evidence["wheelFilename"], work / "dist-1" / evidence["wheelFilename"]]
    payloads = [path.read_bytes() for path in wheels]
    assert payloads[0] == payloads[1]
    assert evidence["wheelSha256"] == sha256(payloads[0]).hexdigest()
    assert evidence["wheelFilename"] == "omh-0.1.0-py3-none-any.whl"
    assert evidence["builds"] == 2
    assert evidence["sourceDateEpoch"]
    assert evidence["candidateCommit"]
    assert evidence["distributionVersion"] == "0.1.0"
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        assert "oh_my_llm/__init__.py" in names
        assert "oh_my_core/__init__.py" in names
        assert "oh_my_coding_agent/__init__.py" in names
        assert archive.read("omh-0.1.0.dist-info/WHEEL").decode().count("Tag: py3-none-any") == 1
        assert b"omh = oh_my_coding_agent._cli:main" in archive.read(
            "omh-0.1.0.dist-info/entry_points.txt"
        )
        record = archive.read("omh-0.1.0.dist-info/RECORD")
        assert record
    dist_files = sorted(path.name for path in (work / "dist-0").iterdir() if path.is_file())
    assert dist_files == [evidence["wheelFilename"]]


def test_offline_locked_install_runs_against_the_candidate_wheel(
    tmp_path: Path,
) -> None:
    work = tmp_path / "candidate-install"
    completed = _run_builder(
        "prove",
        "--repository",
        os.fspath(ROOT),
        "--manifest",
        os.fspath(MANIFEST),
        "--work-root",
        os.fspath(work),
        "--skip-suite",
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(completed.stdout)
    wheel = work / "candidate" / evidence["wheelFilename"]
    assert sha256(wheel.read_bytes()).hexdigest() == evidence["wheelSha256"]
    assert evidence["install"]["offline"] is True
    assert evidence["install"]["onlyBinary"] is True
    assert evidence["install"]["checkoutImport"] is False
    assert evidence["conformance"]["matrixSha256"] == sha256(MATRIX.read_bytes()).hexdigest()
    assert evidence["conformance"]["corpusSha256"] == sha256(CORPUS.read_bytes()).hexdigest()
    assert evidence["conformance"]["lockSha256"] == sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
    assert evidence["conformance"]["manifestSha256"] == sha256(MANIFEST.read_bytes()).hexdigest()
    assert evidence["conformance"]["suiteRan"] is False
    assert evidence["wheelhouse"]["packages"]
    assert evidence["publishRight"] is False
    assert evidence["platformSupportClaim"] is False
    python = Path(evidence["install"]["python"])
    probe = subprocess.run(
        (
            os.fspath(python),
            "-I",
            "-c",
            "import oh_my_llm, oh_my_core, oh_my_coding_agent, pathlib, sys;"
            "print(pathlib.Path(oh_my_llm.__file__).resolve());"
            "print('src' in pathlib.Path(oh_my_llm.__file__).parts)",
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
    version = subprocess.run(
        (os.fspath(python.parent / "omh"), "--version"),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=work,
        timeout=30,
    )
    assert "0.1.0" in version.stdout
    surface = subprocess.run(
        (
            os.fspath(python),
            "-I",
            os.fspath(Path(__file__).with_name("ticket_26_scenario.py")),
        ),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=work,
        timeout=30,
    )
    observed = json.loads(surface.stdout)
    closed = observed["reference.public-surface-closure"]
    assert closed["A"]["importPaths"] == [
        "oh_my_llm",
        "oh_my_llm.providers.deepseek",
        "oh_my_core",
        "oh_my_coding_agent",
    ]
    assert closed["A"]["consoleScripts"] == ["omh=oh_my_coding_agent._cli:main"]
    assert closed["C"] == "closed_installed_public_surface"


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
def test_candidate_install_gate_fails_closed(
    tmp_path: Path, fault: str, needle: str
) -> None:
    work = tmp_path / f"fault-{fault}"
    completed = _run_builder(
        "prove",
        "--repository",
        os.fspath(ROOT),
        "--manifest",
        os.fspath(MANIFEST),
        "--work-root",
        os.fspath(work),
        "--skip-suite",
        "--inject-fault",
        fault,
        timeout=180,
    )
    assert completed.returncode != 0, completed.stdout
    assert needle in completed.stderr.lower()
