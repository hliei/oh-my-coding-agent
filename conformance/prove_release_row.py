from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
from typing import Any


DEFAULT_MATRIX = Path(__file__).with_name("release-row-matrix.json")
INJECTED_IDENTITIES = {
    "windows": {
        "osFamily": "Windows",
        "architecture": "x86_64",
        "osPointRelease": "11",
        "osBuild": "injected",
        "cpythonImplementation": "CPython",
        "cpythonMinor": "3.12",
        "cpythonPatch": "3.12.0",
        "freeThreaded": False,
    },
    "macos-15": {
        "osFamily": "macOS 15",
        "architecture": "arm64",
        "osPointRelease": "15.6.0",
        "osBuild": "injected",
        "cpythonImplementation": "CPython",
        "cpythonMinor": "3.12",
        "cpythonPatch": "3.12.0",
        "freeThreaded": False,
    },
    "macos-x86_64": {
        "osFamily": "macOS 26",
        "architecture": "x86_64",
        "osPointRelease": "26.0.0",
        "osBuild": "injected",
        "cpythonImplementation": "CPython",
        "cpythonMinor": "3.12",
        "cpythonPatch": "3.12.0",
        "freeThreaded": False,
    },
    "ubuntu-22.04": {
        "osFamily": "Ubuntu 22.04",
        "architecture": "x86_64",
        "osPointRelease": "22.04.4",
        "osBuild": "injected",
        "cpythonImplementation": "CPython",
        "cpythonMinor": "3.12",
        "cpythonPatch": "3.12.0",
        "freeThreaded": False,
    },
    "ubuntu-24.04": {
        "osFamily": "Ubuntu 24.04",
        "architecture": "x86_64",
        "osPointRelease": "24.04",
        "osBuild": "injected",
        "cpythonImplementation": "CPython",
        "cpythonMinor": "3.12",
        "cpythonPatch": "3.12.0",
        "freeThreaded": False,
    },
    "linux-arm64": {
        "osFamily": "Ubuntu 24.04",
        "architecture": "arm64",
        "osPointRelease": "24.04",
        "osBuild": "injected",
        "cpythonImplementation": "CPython",
        "cpythonMinor": "3.12",
        "cpythonPatch": "3.12.0",
        "freeThreaded": False,
    },
    "pypy": {
        "osFamily": "macOS 26",
        "architecture": "arm64",
        "osPointRelease": "26.0.0",
        "osBuild": "injected",
        "cpythonImplementation": "PyPy",
        "cpythonMinor": "3.12",
        "cpythonPatch": "3.12.0",
        "freeThreaded": False,
    },
    "cpython-3.11": {
        "osFamily": "macOS 26",
        "architecture": "arm64",
        "osPointRelease": "26.0.0",
        "osBuild": "injected",
        "cpythonImplementation": "CPython",
        "cpythonMinor": "3.11",
        "cpythonPatch": "3.11.0",
        "freeThreaded": False,
    },
    "cpython-3.14": {
        "osFamily": "macOS 26",
        "architecture": "arm64",
        "osPointRelease": "26.0.0",
        "osBuild": "injected",
        "cpythonImplementation": "CPython",
        "cpythonMinor": "3.14",
        "cpythonPatch": "3.14.0",
        "freeThreaded": False,
    },
    "free-threaded": {
        "osFamily": "macOS 26",
        "architecture": "arm64",
        "osPointRelease": "26.0.0",
        "osBuild": "injected",
        "cpythonImplementation": "CPython",
        "cpythonMinor": "3.13",
        "cpythonPatch": "3.13.0",
        "freeThreaded": True,
    },
}


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path}: root must be an object")
    return value


def _run(command: tuple[str, ...], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def _os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    path = Path("/etc/os-release")
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        name, _, raw = line.partition("=")
        values[name] = raw.strip().strip('"')
    return values


def _sw_vers() -> dict[str, str]:
    values: dict[str, str] = {}
    completed = _run(("sw_vers",), timeout=10)
    if completed.returncode != 0:
        return values
    for line in completed.stdout.splitlines():
        name, _, value = line.partition(":")
        values[name.strip()] = value.strip()
    return values


def _architecture() -> str:
    machine = platform.machine()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    if machine in {"x86_64", "amd64", "AMD64"}:
        return "x86_64"
    return machine


def _probe_os() -> tuple[str, str, str]:
    system = platform.system()
    if system == "Darwin":
        info = _sw_vers()
        product = info.get("ProductVersion", "")
        major = product.split(".", 1)[0]
        family = f"macOS {major}" if major else "macOS"
        return family, product, info.get("BuildVersion", "")
    release = _os_release()
    ident = release.get("ID", "")
    version = release.get("VERSION_ID", "")
    if ident == "ubuntu":
        family = f"Ubuntu {version}"
        point = release.get("VERSION", version).split(" ", 1)[0]
        build = release.get("VERSION_CODENAME", "") or platform.release()
        return family, point, build
    family = ident or system
    return family, version or platform.version(), platform.release()


def _python_identity(python: Path) -> dict[str, Any]:
    completed = _run(
        (
            os.fspath(python),
            "-c",
            "import platform, sys, sysconfig, json;"
            "print(json.dumps({"
            "'implementation': platform.python_implementation(),"
            "'major': sys.version_info[0],"
            "'minor': sys.version_info[1],"
            "'micro': sys.version_info[2],"
            "'freeThreaded': bool(sysconfig.get_config_var('Py_GIL_DISABLED')),"
            "}))",
        ),
        timeout=10,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"CPython identity probe failed: {completed.stderr.strip()}")
    payload = json.loads(completed.stdout)
    minor = f"{payload['major']}.{payload['minor']}"
    patch = f"{payload['major']}.{payload['minor']}.{payload['micro']}"
    return {
        "cpythonImplementation": payload["implementation"],
        "cpythonMinor": minor,
        "cpythonPatch": patch,
        "freeThreaded": bool(payload["freeThreaded"]),
    }


def _row_id(os_family: str, architecture: str, cpython_minor: str) -> str:
    return f"{os_family}|{architecture}|CPython {cpython_minor}"


def load_matrix(path: Path) -> dict[str, Any]:
    matrix = _load_json(path)
    rows = matrix.get("rows")
    if not isinstance(rows, list) or len(rows) != 2:
        raise RuntimeError("Release Row matrix must contain exactly two rows")
    ids = [row["id"] for row in rows]
    expected = [
        "macOS 26|arm64|CPython 3.12",
        "macOS 26|arm64|CPython 3.13",
    ]
    if ids != expected:
        raise RuntimeError("Release Row matrix rows are not the closed v0 selection")
    if matrix.get("unselected", {}).get("inferredSupport") is not False:
        raise RuntimeError("unselected environments must not infer support")
    return matrix


def probe_host(*, python: Path, inject: str | None) -> dict[str, Any]:
    if inject is not None:
        injected = INJECTED_IDENTITIES.get(inject)
        if injected is None:
            raise RuntimeError(f"unknown injected identity: {inject}")
        identity = dict(injected)
    else:
        family, point, build = _probe_os()
        python_identity = _python_identity(python)
        identity = {
            "osFamily": family,
            "architecture": _architecture(),
            "osPointRelease": point,
            "osBuild": build,
            **python_identity,
        }
    identity["runner"] = f"{socket.gethostname()}:{os.fspath(python)}"
    identity["row"] = _row_id(
        str(identity["osFamily"]),
        str(identity["architecture"]),
        str(identity["cpythonMinor"]),
    )
    return identity


def classify_host(matrix: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    selected_ids = [row["id"] for row in matrix["rows"]]
    selected = (
        identity["row"] in selected_ids
        and identity["cpythonImplementation"] == "CPython"
        and not identity["freeThreaded"]
    )
    observed = {
        **identity,
        "selected": selected,
        "inferredSupport": False,
    }
    if not selected:
        raise RuntimeError(
            f"{identity['row']} is outside v0; artifact availability creates no support promise"
        )
    return observed


def identify(*, matrix_path: Path, python: Path, inject: str | None) -> dict[str, Any]:
    matrix = load_matrix(matrix_path)
    identity = probe_host(python=python, inject=inject)
    return classify_host(matrix, identity)


def _load_builder() -> Any:
    import importlib.util

    path = Path(__file__).with_name("build_candidate_wheel.py")
    spec = importlib.util.spec_from_file_location("omh_build_candidate_wheel", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Candidate Wheel builder is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row_from_matrix(matrix: dict[str, Any], row_id: str) -> dict[str, Any]:
    for row in matrix["rows"]:
        if row["id"] == row_id:
            return row
    raise RuntimeError(f"{row_id} is outside v0")


def select_wheelhouse(
    *,
    matrix_path: Path,
    repository: Path,
    row_id: str,
) -> dict[str, Any]:
    import shutil
    import tomllib

    matrix = load_matrix(matrix_path)
    row = _row_from_matrix(matrix, row_id)
    builder = _load_builder()
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv binary is not on PATH")
    lock = tomllib.loads((repository / "uv.lock").read_text())
    needed = builder._needed_packages(Path(uv), repository)
    version = tuple(int(part) for part in str(row["cpythonMinor"]).split("."))
    packages = builder._select_locked_wheels(
        lock, needed, version, str(row["architecture"])
    )
    if any(not item["filename"].endswith(".whl") for item in packages):
        raise RuntimeError("source fallback is forbidden in the row wheelhouse")
    return {
        "row": row_id,
        "packages": [
            {
                "name": item["name"],
                "filename": item["filename"],
                "sha256": item["sha256"],
            }
            for item in packages
        ],
        "lockSha256": sha256((repository / "uv.lock").read_bytes()).hexdigest(),
    }


PLATFORM_TESTS = (
    "tests/conformance/test_ticket_14_session_persistence.py",
    "tests/conformance/test_ticket_19_builtin_bash.py",
    "tests/conformance/test_ticket_25_command_signals.py",
)
RECURSIVE_TESTS = (
    "tests/conformance/test_ticket_27_candidate_wheel.py",
    "tests/conformance/test_ticket_28_release_rows.py",
    "tests/conformance/test_ticket_01_installed.py",
)


def _git(repository: Path, *arguments: str) -> str:
    completed = _run(("git", "-C", os.fspath(repository), *arguments), timeout=10)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git command failed")
    return completed.stdout.strip()


def _require_outside_checkout(repository: Path, work_root: Path) -> None:
    repo = repository.resolve()
    work = work_root.resolve()
    if work == repo or repo in work.parents:
        raise RuntimeError("fresh environment must be outside the checkout")


def _wheelhouse_identity(packages: list[dict[str, str]]) -> str:
    payload = json.dumps(packages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def prove_row(
    *,
    matrix_path: Path,
    repository: Path,
    work_root: Path,
    row_id: str,
    python: Path,
    wheel: Path,
    wheel_sha256: str,
    skip_suite: bool,
    platform_only: bool,
    fault: str | None,
    inject: str | None,
) -> dict[str, Any]:
    import tomllib

    _require_outside_checkout(repository, work_root)
    host = identify(matrix_path=matrix_path, python=python, inject=inject)
    if host["row"] != row_id:
        raise RuntimeError(
            f"host {host['row']} does not match requested Release Row {row_id}"
        )
    if not wheel.is_file():
        raise RuntimeError("Candidate Wheel is missing")
    actual_sha = sha256(wheel.read_bytes()).hexdigest()
    if actual_sha != wheel_sha256 and fault != "differing-wheel":
        raise RuntimeError("installed Candidate Wheel hash differs from the double-build artifact")
    builder = _load_builder()
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv binary is not on PATH")
    uv_path = Path(uv)
    lock_check = builder._run(
        (os.fspath(uv_path), "--no-config", "lock", "--check"),
        cwd=repository,
        timeout=30,
    )
    builder._require(lock_check, "lock drift")
    if fault == "lock-drift":
        raise RuntimeError("lock drift")
    lock_sha = sha256((repository / "uv.lock").read_bytes()).hexdigest()
    lock = tomllib.loads((repository / "uv.lock").read_text())
    needed = builder._needed_packages(uv_path, repository)
    version = tuple(int(part) for part in host["cpythonMinor"].split("."))
    with_urls = builder._select_locked_wheels(
        lock, needed, version, str(host["architecture"])
    )
    work_root.mkdir(parents=True, exist_ok=True)
    wheelhouse = work_root / "wheelhouse"
    records = builder._materialize_wheelhouse(
        wheels=with_urls, dest=wheelhouse, lock_sha=lock_sha
    )
    builder._fail_injected_wheelhouse(wheelhouse, fault)
    if fault == "undeclared":
        extra = matrix_path.parent / "build-artifacts" / "hatchling-1.32.0-py3-none-any.whl"
        shutil.copy2(extra, wheelhouse / extra.name)
    candidate_dir = work_root / "candidate"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    installed_wheel = candidate_dir / wheel.name
    shutil.copy2(wheel, installed_wheel)
    if fault == "differing-wheel":
        installed_wheel.write_bytes(installed_wheel.read_bytes() + b"\n")
    venv = work_root / "venv"
    home = work_root / "install-home"
    cache = work_root / "install-cache"
    home.mkdir(exist_ok=True)
    cache.mkdir(exist_ok=True)
    commit = _git(repository, "rev-parse", "HEAD")
    epoch = _git(repository, "log", "-1", "--format=%ct")
    env = builder._isolated_env(
        home=home,
        python=python,
        uv=uv_path,
        source_date_epoch=epoch,
        cache=cache,
    )
    installed_python = builder._install_candidate(
        wheel=installed_wheel,
        wheelhouse=wheelhouse,
        venv=venv,
        python=python,
        uv=uv_path,
        env=env,
        checkout=repository,
        expected_sha=wheel_sha256,
        fault=fault,
        allowed={item["name"] for item in with_urls},
    )
    env = {
        **env,
        "PATH": os.pathsep.join((os.fspath(installed_python.parent), env["PATH"])),
    }
    resolved_python = shutil.which("python", path=env["PATH"])
    if resolved_python is None or Path(resolved_python).resolve() != installed_python.resolve():
        raise RuntimeError("row environment must resolve python to the installed venv")
    suite_ran = False
    platform_ran = False
    if not skip_suite:
        tests_root = repository / "tests"
        if platform_only:
            command = [
                os.fspath(installed_python),
                "-I",
                "-m",
                "pytest",
                *[os.fspath(repository / relative) for relative in PLATFORM_TESTS],
            ]
        else:
            command = [
                os.fspath(installed_python),
                "-I",
                "-m",
                "pytest",
                os.fspath(tests_root),
            ]
            for relative in RECURSIVE_TESTS:
                command.extend(("--ignore", os.fspath(repository / relative)))
        suite = builder._run(tuple(command), cwd=repository, env=env, timeout=600, sandbox=True)
        builder._require(suite, "installed Deterministic Conformance Suite failed")
        suite_ran = not platform_only
        platform_ran = True
    packages = [
        {"filename": item["filename"], "sha256": item["sha256"]} for item in records
    ]
    return {
        "schemaVersion": 1,
        "row": row_id,
        "candidateCommit": commit,
        "wheelFilename": wheel.name,
        "wheelSha256": wheel_sha256,
        "os": {
            "family": host["osFamily"],
            "architecture": host["architecture"],
            "productVersion": host["osPointRelease"],
            "buildVersion": host["osBuild"],
        },
        "cpython": {
            "implementation": host["cpythonImplementation"],
            "minor": host["cpythonMinor"],
            "patch": host["cpythonPatch"],
        },
        "runner": host["runner"],
        "install": {
            "offline": True,
            "onlyBinary": True,
            "checkoutImport": False,
            "python": os.fspath(installed_python),
        },
        "wheelhouse": {
            "packages": packages,
            "identity": _wheelhouse_identity(packages),
        },
        "conformance": {
            "matrixSha256": sha256(
                (repository / "conformance/obligation-matrix.json").read_bytes()
            ).hexdigest(),
            "corpusSha256": sha256(
                (repository / "conformance/reference-observation-corpus.json").read_bytes()
            ).hexdigest(),
            "lockSha256": lock_sha,
            "suiteRan": suite_ran,
            "platformEvidence": platform_ran,
        },
        "inferredSupport": False,
        "publishRight": False,
        "waiver": False,
    }


def bind_rows(*, matrix_path: Path, repository: Path, evidence_paths: list[Path]) -> dict[str, Any]:
    matrix = load_matrix(matrix_path)
    expected = [row["id"] for row in matrix["rows"]]
    if len(evidence_paths) != 2:
        raise RuntimeError("bind requires exactly two Release Row results")
    loaded: list[dict[str, Any]] = []
    for path in evidence_paths:
        payload = _load_json(path)
        loaded.append(payload)
    observed_rows = [item["row"] for item in loaded]
    if sorted(observed_rows) != sorted(expected):
        raise RuntimeError("bind requires exactly the two selected Release Rows")
    by_row = {item["row"]: item for item in loaded}
    ordered = [by_row[row_id] for row_id in expected]
    identities: list[str] = []
    wheel = ordered[0]["wheelSha256"]
    filename = ordered[0]["wheelFilename"]
    commit = ordered[0]["candidateCommit"]
    matrix_sha = sha256((repository / "conformance/obligation-matrix.json").read_bytes()).hexdigest()
    corpus_sha = sha256(
        (repository / "conformance/reference-observation-corpus.json").read_bytes()
    ).hexdigest()
    lock_sha = sha256((repository / "uv.lock").read_bytes()).hexdigest()
    for item in ordered:
        family, architecture, interpreter = str(item["row"]).split("|")
        minor = interpreter.rsplit(" ", 1)[1]
        os_info = item["os"]
        cpython = item["cpython"]
        if os_info.get("family") != family or os_info.get("architecture") != architecture:
            raise RuntimeError("Release Row evidence OS does not match the selected row")
        if cpython.get("implementation") != "CPython" or cpython.get("minor") != minor:
            raise RuntimeError("Release Row evidence CPython does not match the selected row")
        if not os_info.get("productVersion") or not os_info.get("buildVersion"):
            raise RuntimeError("Release Row evidence must record OS point release/build")
        if not cpython.get("patch") or not item.get("runner"):
            raise RuntimeError("Release Row evidence must record CPython patch and runner identity")
        if item["wheelSha256"] != wheel or item["wheelFilename"] != filename:
            raise RuntimeError("Release Rows must bind the identical Candidate Wheel")
        if item["candidateCommit"] != commit:
            raise RuntimeError("Release Rows must bind the same release-candidate commit")
        conformance = item["conformance"]
        if conformance["matrixSha256"] != matrix_sha or conformance["corpusSha256"] != corpus_sha:
            raise RuntimeError("Release Rows must bind the same Matrix and corpus")
        if conformance["lockSha256"] != lock_sha:
            raise RuntimeError("Release Rows must bind the committed lock")
        if conformance.get("suiteRan") is not True:
            raise RuntimeError("skipped suite results cannot be retried into acceptance")
        if conformance.get("platformEvidence") is not True:
            raise RuntimeError("platform evidence is required on every Release Row")
        if item.get("waiver") is True:
            raise RuntimeError("Release Row failure cannot be waived")
        install = item["install"]
        if install.get("checkoutImport") is True:
            raise RuntimeError("source-tree results cannot replace a Release Row")
        if install.get("offline") is not True or install.get("onlyBinary") is not True:
            raise RuntimeError("Release Row install must be offline and only-binary")
        if item.get("inferredSupport") is not False:
            raise RuntimeError("unselected environments remain outside v0")
        if item.get("publishRight") is True:
            raise RuntimeError("Release Rows do not grant the Publish Right")
        house = item.get("wheelhouse") if isinstance(item.get("wheelhouse"), dict) else {}
        identity = house.get("identity")
        if not identity:
            raise RuntimeError("Release Row evidence must record wheelhouse identity")
        identities.append(str(identity))
    if len(set(identities)) != len(expected):
        raise RuntimeError("row-specific wheelhouse identity is required")
    return {
        "schemaVersion": 1,
        "rows": expected,
        "candidateCommit": commit,
        "wheelFilename": filename,
        "wheelSha256": wheel,
        "conformance": {
            "matrixSha256": matrix_sha,
            "corpusSha256": corpus_sha,
            "lockSha256": lock_sha,
        },
        "readyForReleaseEvidenceBundle": True,
        "inferredSupport": False,
        "publishRight": False,
        "waiver": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("identify", "wheelhouse", "prove", "bind"))
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--row")
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--wheel-sha256")
    parser.add_argument("--skip-suite", action="store_true")
    parser.add_argument("--platform-only", action="store_true")
    parser.add_argument("--inject-identity", choices=tuple(INJECTED_IDENTITIES))
    parser.add_argument(
        "--inject-fault",
        choices=(
            "missing-prebuilt",
            "source-fallback",
            "checkout-import",
            "differing-wheel",
            "lock-drift",
            "undeclared",
        ),
    )
    parser.add_argument("evidence", nargs="*", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "identify":
            evidence = identify(
                matrix_path=args.matrix.resolve(),
                python=args.python.resolve(),
                inject=args.inject_identity,
            )
        elif args.command == "wheelhouse":
            if not args.row:
                raise RuntimeError("wheelhouse requires --row")
            evidence = select_wheelhouse(
                matrix_path=args.matrix.resolve(),
                repository=args.repository.resolve(),
                row_id=args.row,
            )
        elif args.command == "prove":
            if not args.row or args.work_root is None or args.wheel is None or not args.wheel_sha256:
                raise RuntimeError("prove requires --row, --work-root, --wheel, and --wheel-sha256")
            evidence = prove_row(
                matrix_path=args.matrix.resolve(),
                repository=args.repository.resolve(),
                work_root=args.work_root.resolve(),
                row_id=args.row,
                python=args.python.resolve(),
                wheel=args.wheel.resolve(),
                wheel_sha256=args.wheel_sha256,
                skip_suite=args.skip_suite,
                platform_only=args.platform_only,
                fault=args.inject_fault,
                inject=args.inject_identity,
            )
        else:
            evidence = bind_rows(
                matrix_path=args.matrix.resolve(),
                repository=args.repository.resolve(),
                evidence_paths=[path.resolve() for path in args.evidence],
            )
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
