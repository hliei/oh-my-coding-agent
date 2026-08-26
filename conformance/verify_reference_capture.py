from __future__ import annotations

import argparse
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tarfile
import tempfile
from typing import Any


REFERENCE_REVISION = "0e6909f050eeb15e8f6c05185511f3788357ddb3"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _clean_export(reference_repository: Path, destination: Path) -> None:
    archived = subprocess.run(
        (
            "git",
            "-C",
            os.fspath(reference_repository),
            "archive",
            REFERENCE_REVISION,
        ),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    with tarfile.open(fileobj=BytesIO(archived.stdout), mode="r:") as archive:
        archive.extractall(destination, filter="data")


def _capture(
    *,
    node: Path,
    python: Path,
    harness: Path,
    manifest: Path,
    reference_export: Path,
) -> bytes:
    environment = {
        "PATH": f"{node.parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": os.environ.get("HOME", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }
    completed = subprocess.run(
        (
            os.fspath(node),
            os.fspath(harness),
            os.fspath(python),
            os.fspath(manifest),
            os.fspath(reference_export),
        ),
        check=True,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
    )
    return completed.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference-repository", required=True, type=Path)
    parser.add_argument("--node", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--harness", required=True, type=Path)
    parser.add_argument("--expected-corpus", required=True, type=Path)
    args = parser.parse_args()

    expected = args.expected_corpus.read_bytes()
    captures: list[bytes] = []
    with tempfile.TemporaryDirectory(prefix="omh-reference-row-") as raw_root:
        root = Path(raw_root)
        for index in range(2):
            reference_export = root / f"fixed-reference-{index}"
            reference_export.mkdir()
            _clean_export(args.reference_repository, reference_export)
            captures.append(
                _capture(
                    node=args.node,
                    python=args.python,
                    harness=args.harness,
                    manifest=args.manifest,
                    reference_export=reference_export,
                )
            )

    if captures != [expected, expected]:
        raise RuntimeError("independent Reference captures differ from the corpus")
    evidence: dict[str, Any] = {
        "schemaVersion": 1,
        "referenceRevision": REFERENCE_REVISION,
        "platform": platform.platform(),
        "nodeSha256": _sha256(args.node),
        "npm": subprocess.run(
            (os.fspath(args.node.parent / "npm"), "--version"),
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        ).stdout.strip(),
        "manifestSha256": _sha256(args.manifest),
        "harnessSha256": _sha256(args.harness),
        "corpusSha256": hashlib.sha256(expected).hexdigest(),
        "captureSha256": [hashlib.sha256(value).hexdigest() for value in captures],
    }
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
