from __future__ import annotations

import argparse
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from typing import Any
from urllib.parse import unquote
from urllib.request import urlopen
import zipfile


SANDBOX = "/usr/bin/sandbox-exec"
SANDBOX_PROFILE = "(version 1)(allow default)(deny network*)"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path}: root must be an object")
    return value


def _run(
    command: tuple[str, ...],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int,
    sandbox: bool = False,
) -> subprocess.CompletedProcess[str]:
    invocation = (
        (SANDBOX, "-p", SANDBOX_PROFILE, *command) if sandbox else command
    )
    return subprocess.run(
        invocation,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def _require(completed: subprocess.CompletedProcess[str], message: str) -> None:
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or message
        raise RuntimeError(f"{message}: {detail}")


def _git(repository: Path, *arguments: str, timeout: int = 30) -> str:
    completed = _run(
        ("git", "-C", os.fspath(repository), *arguments),
        timeout=timeout,
    )
    _require(completed, "git command failed")
    return completed.stdout.strip()


def _clean_export(repository: Path, commit: str, destination: Path) -> None:
    archived = subprocess.run(
        ("git", "-C", os.fspath(repository), "archive", commit),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    with tarfile.open(fileobj=BytesIO(archived.stdout), mode="r:") as archive:
        archive.extractall(destination, filter="data")


def _sw_vers() -> dict[str, str]:
    values: dict[str, str] = {}
    completed = _run(("sw_vers",), timeout=10)
    _require(completed, "sw_vers failed")
    for line in completed.stdout.splitlines():
        name, _, value = line.partition(":")
        values[name.strip()] = value.strip()
    return values


def _verify_toolchain(
    manifest: dict[str, Any], python: Path, uv: Path
) -> dict[str, Any]:
    os_info = _sw_vers()
    expected_os = manifest["os"]
    if expected_os["family"] != "macOS 26":
        raise RuntimeError("Candidate Build Row is pinned to macOS 26")
    if expected_os["architecture"] != platform.machine():
        raise RuntimeError("Candidate Build Row architecture mismatch")
    if os_info.get("ProductVersion") != expected_os["productVersion"]:
        raise RuntimeError("macOS point release does not match the toolchain manifest")
    if os_info.get("BuildVersion") != expected_os["buildVersion"]:
        raise RuntimeError("macOS build does not match the toolchain manifest")
    python_bytes = python.read_bytes()
    actual_python = hashlib.sha256(python_bytes).hexdigest()
    if actual_python != manifest["cpython"]["sha256"]:
        raise RuntimeError("CPython binary hash does not match the toolchain manifest")
    version = _run((os.fspath(python), "--version"), timeout=10)
    _require(version, "CPython version probe failed")
    expected_version = f"Python {manifest['cpython']['version']}"
    if version.stdout.strip() != expected_version:
        raise RuntimeError("CPython version does not match the toolchain manifest")
    uv_hash = _sha256(uv)
    if uv_hash != manifest["uv"]["sha256"]:
        raise RuntimeError("uv binary hash does not match the toolchain manifest")
    uv_version = _run((os.fspath(uv), "--version"), timeout=10)
    _require(uv_version, "uv version probe failed")
    if manifest["uv"]["version"] not in uv_version.stdout:
        raise RuntimeError("uv version does not match the toolchain manifest")
    if manifest["hatchling"] != "1.32.0":
        raise RuntimeError("locked Hatchling must be 1.32.0")
    return {
        "os": os_info,
        "cpython": manifest["cpython"],
        "uv": manifest["uv"],
        "hatchling": manifest["hatchling"],
    }


def _verify_artifacts(manifest: dict[str, Any], artifacts: Path) -> list[dict[str, str]]:
    recorded: list[dict[str, str]] = []
    for item in manifest["buildArtifacts"]:
        path = artifacts / item["filename"]
        if not path.is_file():
            raise RuntimeError(f"prefetched build artifact is missing: {item['filename']}")
        digest = _sha256(path)
        if digest != item["sha256"]:
            raise RuntimeError(f"prefetched build artifact hash mismatch: {item['filename']}")
        if not item["filename"].endswith(".whl"):
            raise RuntimeError(f"build artifact is not a prebuilt wheel: {item['filename']}")
        recorded.append({"filename": item["filename"], "sha256": digest})
    return recorded


def _isolated_env(
    *,
    home: Path,
    python: Path,
    uv: Path,
    source_date_epoch: str,
    cache: Path,
) -> dict[str, str]:
    path = os.pathsep.join(
        (os.fspath(uv.parent), os.fspath(python.parent), "/usr/bin", "/bin")
    )
    return {
        "PATH": path,
        "HOME": os.fspath(home),
        "SOURCE_DATE_EPOCH": source_date_epoch,
        "TZ": "UTC",
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "UV_NO_CONFIG": "1",
        "UV_CACHE_DIR": os.fspath(cache),
        "UV_PYTHON": os.fspath(python),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }


def _build_wheel(
    *,
    export: Path,
    dist: Path,
    python: Path,
    uv: Path,
    artifacts: Path,
    env: dict[str, str],
) -> Path:
    dist.mkdir(parents=True, exist_ok=True)
    completed = _run(
        (
            os.fspath(uv),
            "--offline",
            "--no-config",
            "build",
            "--wheel",
            "--no-index",
            "--find-links",
            os.fspath(artifacts),
            "--no-create-gitignore",
            "--out-dir",
            os.fspath(dist),
            "--python",
            os.fspath(python),
            os.fspath(export),
        ),
        env=env,
        timeout=120,
        sandbox=True,
    )
    _require(completed, "Candidate Wheel build failed")
    wheels = sorted(dist.glob("*.whl"))
    extras = [path.name for path in dist.iterdir() if path.is_file() and path.suffix != ".whl"]
    sdists = [path.name for path in dist.iterdir() if path.suffix in {".gz", ".zip"}]
    if sdists or extras:
        raise RuntimeError("Candidate Build produced an sdist or alternative wheel path")
    if len(wheels) != 1:
        raise RuntimeError("Candidate Build must produce exactly one wheel")
    wheel = wheels[0]
    if not wheel.name.endswith("-py3-none-any.whl"):
        raise RuntimeError("Candidate Wheel must be a universal py3-none-any artifact")
    return wheel


def _inspect_wheel(wheel: Path) -> dict[str, Any]:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata = archive.read(_dist_member(names, "METADATA")).decode()
        record = archive.read(_dist_member(names, "RECORD")).decode()
        wheel_meta = archive.read(_dist_member(names, "WHEEL")).decode()
        entry_points = archive.read(_dist_member(names, "entry_points.txt")).decode()
    if "Tag: py3-none-any" not in wheel_meta:
        raise RuntimeError("Candidate Wheel WHEEL metadata is not py3-none-any")
    if "omh = oh_my_coding_agent._cli:main" not in entry_points:
        raise RuntimeError("Candidate Wheel is missing the omh command")
    for package in ("oh_my_llm/__init__.py", "oh_my_core/__init__.py", "oh_my_coding_agent/__init__.py"):
        if package not in names:
            raise RuntimeError(f"Candidate Wheel is missing {package}")
    return {
        "filename": wheel.name,
        "sha256": _sha256(wheel),
        "names": names,
        "metadata": metadata,
        "record": record,
        "wheel": wheel_meta,
        "entryPoints": entry_points,
    }


def _dist_member(names: list[str], suffix: str) -> str:
    matches = [name for name in names if name.endswith(f".dist-info/{suffix}")]
    if len(matches) != 1:
        raise RuntimeError(f"Candidate Wheel metadata {suffix} is missing")
    return matches[0]


def _compare_inspections(first: dict[str, Any], second: dict[str, Any]) -> None:
    for field in ("filename", "sha256", "names", "metadata", "record", "wheel", "entryPoints"):
        if first[field] != second[field]:
            raise RuntimeError(f"independent Candidate Wheel builds differ in {field}")


def _wheel_filename(url: str) -> str:
    return unquote(url.rstrip("/").rsplit("/", 1)[-1])


def _wheel_compatible(filename: str, version: tuple[int, int], machine: str) -> bool:
    if not filename.endswith(".whl"):
        return False
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        return False
    python, abi, plat = parts[-3], parts[-2], parts[-1]
    impl = f"cp{version[0]}{version[1]}"
    py_ok = python in {
        "py3",
        "py2.py3",
        impl,
        f"py{version[0]}",
        f"py{version[0]}{version[1]}",
    }
    abi_ok = abi in {"none", "abi3", impl}
    if plat == "any":
        return py_ok and abi_ok
    if not py_ok or not abi_ok:
        return False
    if machine == "arm64":
        return "macosx" in plat and "arm64" in plat
    return False


def _macosx_rank(filename: str) -> tuple[int, int]:
    stem = filename[:-4]
    plat = stem.split("-")[-1]
    if plat == "any":
        return (1_000, 0)
    marker = "macosx_"
    if marker not in plat:
        return (0, 0)
    version = plat.split(marker, 1)[1].split("_", 2)
    try:
        return (int(version[0]), int(version[1]))
    except (ValueError, IndexError):
        return (0, 0)


def _select_locked_wheels(
    lock: dict[str, Any], needed: set[str], version: tuple[int, int], machine: str
) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    packages = {item["name"]: item for item in lock["package"]}
    for name in sorted(needed):
        item = packages.get(name)
        if item is None:
            raise RuntimeError(f"lock is missing package {name}")
        compatible: list[dict[str, str]] = []
        for wheel in item.get("wheels") or []:
            filename = _wheel_filename(wheel["url"])
            if _wheel_compatible(filename, version, machine):
                compatible.append(
                    {
                        "name": name,
                        "filename": filename,
                        "url": wheel["url"],
                        "sha256": str(wheel["hash"]).removeprefix("sha256:"),
                    }
                )
        if not compatible:
            raise RuntimeError(f"missing prebuilt dependency wheel for {name}")
        compatible.sort(key=lambda wheel: _macosx_rank(wheel["filename"]), reverse=True)
        selected.append(compatible[0])
    return selected


def _needed_packages(uv: Path, repository: Path) -> set[str]:
    completed = _run(
        (
            os.fspath(uv),
            "--no-config",
            "export",
            "--frozen",
            "--no-emit-project",
            "--no-header",
            "--no-annotate",
            "--format",
            "requirements-txt",
        ),
        cwd=repository,
        timeout=30,
    )
    _require(completed, "uv export failed")
    names: set[str] = set()
    for raw in completed.stdout.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("--"):
            continue
        if ";" in line:
            requirement, _, marker = line.partition(";")
            if "sys_platform == 'win32'" in marker:
                continue
        else:
            requirement = line
        package = requirement.split("==", 1)[0].strip().lower()
        if package:
            names.add(package)
    if not names:
        raise RuntimeError("uv export produced no locked dependencies")
    return names


def _materialize_wheelhouse(
    *,
    wheels: list[dict[str, str]],
    dest: Path,
    lock_sha: str,
) -> list[dict[str, str]]:
    dest.mkdir(parents=True, exist_ok=True)
    cache = Path(tempfile.gettempdir()) / f"omh-wheelhouse-{lock_sha[:16]}"
    cache.mkdir(parents=True, exist_ok=True)
    recorded: list[dict[str, str]] = []
    for item in wheels:
        cached = cache / item["filename"]
        target = dest / item["filename"]
        if cached.is_file() and _sha256(cached) == item["sha256"]:
            shutil.copy2(cached, target)
        else:
            with urlopen(item["url"], timeout=60) as response:
                payload = response.read()
            digest = hashlib.sha256(payload).hexdigest()
            if digest != item["sha256"]:
                raise RuntimeError(f"wheelhouse hash mismatch for {item['filename']}")
            cached.write_bytes(payload)
            target.write_bytes(payload)
        if target.suffix != ".whl":
            raise RuntimeError("source fallback is forbidden in the row wheelhouse")
        recorded.append({"filename": item["filename"], "sha256": item["sha256"]})
    extras = [path.name for path in dest.iterdir() if path.suffix in {".gz", ".zip", ".tar"}]
    if extras:
        raise RuntimeError("source fallback is forbidden in the row wheelhouse")
    return recorded


def _fail_injected_wheelhouse(dest: Path, fault: str | None) -> None:
    if fault == "missing-prebuilt":
        for path in dest.glob("google_re2-*.whl"):
            path.unlink()
        return
    if fault == "source-fallback":
        for path in dest.glob("google_re2-*.whl"):
            sdist = dest / "google_re2-1.1.20251105.tar.gz"
            sdist.write_bytes(path.read_bytes())
            path.unlink()
        return


def _install_candidate(
    *,
    wheel: Path,
    wheelhouse: Path,
    venv: Path,
    python: Path,
    uv: Path,
    env: dict[str, str],
    checkout: Path,
    expected_sha: str,
    fault: str | None,
    allowed: set[str],
) -> Path:
    if fault == "source-fallback" and any(
        path.suffix in {".gz", ".zip", ".tar"} for path in wheelhouse.iterdir()
    ):
        raise RuntimeError("source fallback is forbidden in the row wheelhouse")
    missing = not any(wheelhouse.glob("google_re2-*.whl"))
    if missing:
        raise RuntimeError("missing prebuilt dependency wheel for google-re2")
    completed = _run(
        (
            os.fspath(uv),
            "--offline",
            "--no-config",
            "venv",
            "--python",
            os.fspath(python),
            os.fspath(venv),
        ),
        env=env,
        timeout=30,
        sandbox=True,
    )
    _require(completed, "virtual environment creation failed")
    venv_python = venv / "bin" / "python"
    if fault == "checkout-import":
        install = _run(
            (
                os.fspath(uv),
                "--no-config",
                "pip",
                "install",
                "--python",
                os.fspath(venv_python),
                "-e",
                os.fspath(checkout),
            ),
            env=env,
            timeout=120,
        )
        _require(install, "checkout install failed")
    else:
        install = _run(
            (
                os.fspath(uv),
                "--offline",
                "--no-config",
                "pip",
                "install",
                "--python",
                os.fspath(venv_python),
                "--no-index",
                "--find-links",
                os.fspath(wheelhouse),
                "--only-binary",
                ":all:",
                os.fspath(wheel),
            ),
            env=env,
            timeout=120,
            sandbox=True,
        )
        _require(install, "offline Candidate Wheel install failed")
        extras = [
            os.fspath(path)
            for path in wheelhouse.glob("*.whl")
            if not path.name.startswith("omh-")
        ]
        suite_deps = _run(
            (
                os.fspath(uv),
                "--offline",
                "--no-config",
                "pip",
                "install",
                "--python",
                os.fspath(venv_python),
                "--no-index",
                "--find-links",
                os.fspath(wheelhouse),
                "--only-binary",
                ":all:",
                *extras,
            ),
            env=env,
            timeout=120,
            sandbox=True,
        )
        _require(suite_deps, "offline wheelhouse install failed")
    probe = _run(
        (
            os.fspath(venv_python),
            "-I",
            "-c",
            "import importlib.metadata, pathlib, sys, oh_my_llm, oh_my_core, oh_my_coding_agent;"
            f"root = pathlib.Path({os.fspath(checkout)!r}).resolve();"
            "modules = (oh_my_llm, oh_my_core, oh_my_coding_agent);"
            "paths = [pathlib.Path(module.__file__).resolve() for module in modules];"
            "print(importlib.metadata.version('omh'));"
            "print('\\n'.join(str(path) for path in paths));"
            "raise SystemExit('checkout import' if any(root in path.parents or (root / 'src') in path.parents for path in paths) else 0)",
        ),
        env=env,
        timeout=30,
        sandbox=True,
    )
    if "checkout" in (probe.stderr + probe.stdout).lower() or probe.returncode != 0:
        raise RuntimeError("checkout import is forbidden for the Candidate Wheel")
    if fault != "checkout-import":
        listed = _run(
            (
                os.fspath(uv),
                "--offline",
                "--no-config",
                "pip",
                "list",
                "--python",
                os.fspath(venv_python),
                "--format",
                "json",
            ),
            env=env,
            timeout=30,
            sandbox=True,
        )
        _require(listed, "installed package inventory failed")
        installed = {
            str(item["name"]).replace("_", "-").lower()
            for item in json.loads(listed.stdout)
        }
        undeclared = sorted(installed - allowed - {"omh"})
        if undeclared:
            raise RuntimeError(f"undeclared dependency: {undeclared}")
    if _sha256(wheel) != expected_sha:
        raise RuntimeError("installed Candidate Wheel hash differs from the double-build artifact")
    return venv_python


def _run_suite(python: Path, tests: Path, env: dict[str, str], cwd: Path) -> None:
    completed = _run(
        (
            os.fspath(python),
            "-I",
            "-m",
            "pytest",
            os.fspath(tests),
            "--ignore",
            os.fspath(tests / "conformance/test_ticket_27_candidate_wheel.py"),
            "--ignore",
            os.fspath(tests / "conformance/test_ticket_01_installed.py"),
        ),
        cwd=cwd,
        env=env,
        timeout=600,
        sandbox=True,
    )
    _require(completed, "installed Deterministic Conformance Suite failed")


def _resolve_python(manifest: dict[str, Any]) -> Path:
    candidate = Path(sys.executable).resolve()
    if _sha256(candidate) == manifest["cpython"]["sha256"]:
        return candidate
    raise RuntimeError("CPython binary hash does not match the toolchain manifest")


def _resolve_uv(manifest: dict[str, Any]) -> Path:
    found = shutil.which("uv")
    if found is None:
        raise RuntimeError("uv binary is not on PATH")
    candidate = Path(found).resolve()
    if _sha256(candidate) == manifest["uv"]["sha256"]:
        return candidate
    raise RuntimeError("uv binary hash does not match the toolchain manifest")


def build_candidate(
    *,
    repository: Path,
    manifest_path: Path,
    work_root: Path,
    fault: str | None = None,
) -> dict[str, Any]:
    manifest = _load_json(manifest_path)
    artifacts = manifest_path.parent / "build-artifacts"
    python = _resolve_python(manifest)
    uv = _resolve_uv(manifest)
    toolchain = _verify_toolchain(manifest, python, uv)
    artifact_records = _verify_artifacts(manifest, artifacts)
    commit = _git(repository, "rev-parse", "HEAD")
    source_date_epoch = _git(repository, "log", "-1", "--format=%ct")
    lock_bytes = (repository / "uv.lock").read_bytes()
    lock_check = _run(
        (os.fspath(uv), "--no-config", "lock", "--check"),
        cwd=repository,
        timeout=30,
    )
    _require(lock_check, "lock drift")
    inspections: list[dict[str, Any]] = []
    wheels: list[Path] = []
    work_root.mkdir(parents=True, exist_ok=True)
    for index in range(2):
        export = work_root / f"export-{index}"
        export.mkdir()
        _clean_export(repository, commit, export)
        if fault == "lock-drift":
            export.joinpath("uv.lock").write_bytes(lock_bytes + b"\n")
        if export.joinpath("uv.lock").read_bytes() != lock_bytes:
            raise RuntimeError("lock drift: clean export uv.lock differs from the repository")
        dist = work_root / f"dist-{index}"
        home = work_root / f"home-{index}"
        cache = work_root / f"cache-{index}"
        home.mkdir()
        cache.mkdir()
        epoch = source_date_epoch if not (fault == "differing-wheel" and index == 1) else str(int(source_date_epoch) + 1)
        env = _isolated_env(
            home=home,
            python=python,
            uv=uv,
            source_date_epoch=epoch,
            cache=cache,
        )
        wheel = _build_wheel(
            export=export,
            dist=dist,
            python=python,
            uv=uv,
            artifacts=artifacts,
            env=env,
        )
        wheels.append(wheel)
        inspections.append(_inspect_wheel(wheel))
    try:
        _compare_inspections(inspections[0], inspections[1])
        if wheels[0].read_bytes() != wheels[1].read_bytes():
            raise RuntimeError("independent Candidate Wheel builds differ")
    except RuntimeError:
        if fault == "differing-wheel":
            raise RuntimeError("independent Candidate Wheel builds differ") from None
        raise
    project = tomllib.loads((repository / "pyproject.toml").read_text())
    candidate_dir = work_root / "candidate"
    candidate_dir.mkdir(exist_ok=True)
    candidate = candidate_dir / inspections[0]["filename"]
    shutil.copy2(wheels[0], candidate)
    return {
        "schemaVersion": 1,
        "row": "Candidate Build Row",
        "candidateCommit": commit,
        "distributionVersion": project["project"]["version"],
        "wheelFilename": inspections[0]["filename"],
        "wheelSha256": inspections[0]["sha256"],
        "sourceDateEpoch": source_date_epoch,
        "builds": 2,
        "toolchain": toolchain,
        "buildArtifacts": artifact_records,
        "python": os.fspath(python),
        "uv": os.fspath(uv),
    }


def prove_candidate(
    *,
    repository: Path,
    manifest_path: Path,
    work_root: Path,
    skip_suite: bool,
    fault: str | None,
) -> dict[str, Any]:
    evidence = build_candidate(
        repository=repository,
        manifest_path=manifest_path,
        work_root=work_root,
        fault=fault,
    )
    manifest = _load_json(manifest_path)
    python = Path(evidence["python"])
    uv = Path(evidence["uv"])
    lock = tomllib.loads((repository / "uv.lock").read_text())
    needed = _needed_packages(uv, repository)
    version_info = _run((os.fspath(python), "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"), timeout=10)
    _require(version_info, "CPython minor probe failed")
    major, minor = (int(part) for part in version_info.stdout.split())
    selected = _select_locked_wheels(lock, needed, (major, minor), platform.machine())
    wheelhouse = work_root / "wheelhouse"
    lock_sha = _sha256(repository / "uv.lock")
    records = _materialize_wheelhouse(wheels=selected, dest=wheelhouse, lock_sha=lock_sha)
    _fail_injected_wheelhouse(wheelhouse, fault)
    if fault == "undeclared":
        extra = manifest_path.parent / "build-artifacts" / "hatchling-1.32.0-py3-none-any.whl"
        shutil.copy2(extra, wheelhouse / extra.name)
    venv = work_root / "venv"
    home = work_root / "install-home"
    cache = work_root / "install-cache"
    home.mkdir(exist_ok=True)
    cache.mkdir(exist_ok=True)
    env = _isolated_env(
        home=home,
        python=python,
        uv=uv,
        source_date_epoch=evidence["sourceDateEpoch"],
        cache=cache,
    )
    installed_python = _install_candidate(
        wheel=work_root / "candidate" / evidence["wheelFilename"],
        wheelhouse=wheelhouse,
        venv=venv,
        python=python,
        uv=uv,
        env=env,
        checkout=repository,
        expected_sha=evidence["wheelSha256"],
        fault=fault,
        allowed={item["name"] for item in selected},
    )
    suite_ran = False
    if not skip_suite:
        _run_suite(installed_python, repository / "tests", env, repository)
        suite_ran = True
    evidence["install"] = {
        "offline": True,
        "onlyBinary": True,
        "checkoutImport": False,
        "python": os.fspath(installed_python),
    }
    evidence["wheelhouse"] = {"packages": records}
    evidence["conformance"] = {
        "matrixSha256": _sha256(repository / "conformance/obligation-matrix.json"),
        "corpusSha256": _sha256(repository / "conformance/reference-observation-corpus.json"),
        "lockSha256": lock_sha,
        "manifestSha256": _sha256(manifest_path),
        "suiteRan": suite_ran,
    }
    evidence["publishRight"] = False
    evidence["platformSupportClaim"] = False
    evidence["hatchling"] = manifest["hatchling"]
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("build", "prove"))
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--skip-suite", action="store_true")
    parser.add_argument("--inject-fault", choices=(
        "missing-prebuilt",
        "source-fallback",
        "checkout-import",
        "differing-wheel",
        "lock-drift",
        "undeclared",
    ))
    args = parser.parse_args()
    try:
        if args.command == "build":
            evidence = build_candidate(
                repository=args.repository.resolve(),
                manifest_path=args.manifest.resolve(),
                work_root=args.work_root.resolve(),
                fault=args.inject_fault,
            )
        else:
            evidence = prove_candidate(
                repository=args.repository.resolve(),
                manifest_path=args.manifest.resolve(),
                work_root=args.work_root.resolve(),
                skip_suite=args.skip_suite,
                fault=args.inject_fault,
            )
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
