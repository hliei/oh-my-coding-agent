from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


REFERENCE_REVISION = "0e6909f050eeb15e8f6c05185511f3788357ddb3"


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, separators=(",", ": "))
        + "\n"
    ).encode("utf-8")


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"{path}: root must be an object")
    return value


def _validate_manifest(
    manifest: dict[str, Any],
    reference_export: Path,
    capture_output: dict[str, Any],
) -> dict[str, Any]:
    if manifest.get("schemaVersion") != 1:
        raise ValueError("Reference capture manifest schemaVersion must be 1")
    if manifest.get("referenceRevision") != REFERENCE_REVISION:
        raise ValueError("Reference capture manifest revision is not fixed")
    source_files = manifest.get("sourceFiles")
    entries = manifest.get("cases")
    if not isinstance(source_files, dict) or not isinstance(entries, list):
        raise ValueError("Reference capture manifest shape is invalid")

    for relative, expected in sorted(source_files.items()):
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise ValueError("Reference source hashes must be string mappings")
        source = reference_export / relative
        if not source.is_file():
            raise ValueError(f"Fixed Reference source is missing: {relative}")
        actual = hashlib.sha256(source.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"Fixed Reference source hash differs: {relative}")

    if capture_output.get("schemaVersion") != 1:
        raise ValueError("Reference runtime capture schemaVersion must be 1")
    if capture_output.get("referenceRevision") != REFERENCE_REVISION:
        raise ValueError("Reference runtime capture revision is not fixed")
    captured_entries = capture_output.get("cases")
    if not isinstance(captured_entries, list):
        raise ValueError("Reference runtime capture cases must be a list")
    captured_cases: dict[str, dict[str, Any]] = {}
    for captured in captured_entries:
        if not isinstance(captured, dict) or not isinstance(captured.get("id"), str):
            raise ValueError("Reference runtime capture case is invalid")
        captured_id = captured["id"]
        if captured_id in captured_cases:
            raise ValueError("Reference runtime capture case ids must be unique")
        observations = captured.get("observations")
        if not isinstance(observations, dict):
            raise ValueError(f"{captured_id}: captured observations are invalid")
        captured_cases[captured_id] = captured

    cases: list[dict[str, Any]] = []
    case_ids: set[str] = set()
    capture_ids: set[str] = set()
    cited_sources: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Reference capture entry must be an object")
        origin = entry.get("origin")
        if origin == "fixed-reference":
            reference_case = entry.get("referenceCase")
            if not isinstance(reference_case, dict):
                raise ValueError("Fixed Reference capture case is invalid")
            if "omhExpectation" in reference_case:
                raise ValueError("Fixed Reference capture contains an omh oracle")
            capture_driver = entry.get("captureDriver")
            if not isinstance(capture_driver, str):
                raise ValueError("Fixed Reference capture driver is required")
            if "observations" in reference_case:
                raise ValueError("Fixed Reference case contains static observations")
            captured = captured_cases.pop(reference_case.get("id", ""), None)
            if captured is None:
                raise ValueError(
                    f"{reference_case.get('id')}: Reference capture is missing"
                )
            captured_observations = captured["observations"]
            case = {}
            for name, value in reference_case.items():
                case[name] = value
                if name == "canonicalInput":
                    case["observations"] = captured_observations
                    if "omhExpectation" in entry:
                        case["omhExpectation"] = entry["omhExpectation"]
                elif name == "observations" and "omhExpectation" in entry:
                    case["omhExpectation"] = entry["omhExpectation"]
        elif origin == "local-release":
            local_case = entry.get("localCase")
            if not isinstance(local_case, dict):
                raise ValueError("Local release case is invalid")
            case = local_case
        else:
            raise ValueError("Reference capture entry origin is invalid")
        case_id = case.get("id")
        capture_id = case.get("captureCaseId")
        if not isinstance(case_id, str) or case_id in case_ids:
            raise ValueError("Reference corpus case ids must be unique strings")
        if not isinstance(capture_id, str) or capture_id in capture_ids:
            raise ValueError("Reference capture ids must be unique strings")
        if case.get("referenceRevision") != REFERENCE_REVISION:
            raise ValueError(f"{case_id}: revision is not fixed")
        if not isinstance(case.get("observations"), dict):
            raise ValueError(f"{case_id}: observations are required")
        citations = case.get("referenceCitations")
        if not isinstance(citations, list) or not citations:
            raise ValueError(f"{case_id}: citations are required")
        for citation in citations:
            if not isinstance(citation, str):
                raise ValueError(f"{case_id}: citation must be a string")
            source_path = citation.split("#", 1)[0]
            if origin == "fixed-reference":
                if source_path.startswith(".scratch/"):
                    raise ValueError(f"{case_id}: fixed capture cites local authority")
                cited_sources.add(source_path)
        if origin == "local-release":
            observations = case.get("observations")
            if not isinstance(observations, dict) or observations.get(
                "reference"
            ) != "not_applicable":
                raise ValueError(f"{case_id}: local release lacks not_applicable")
        cases.append(case)
        case_ids.add(case_id)
        capture_ids.add(capture_id)

    if captured_cases:
        raise ValueError(
            f"Reference runtime capture contains orphan cases: {sorted(captured_cases)}"
        )
    if not cited_sources <= set(source_files):
        missing = sorted(cited_sources - set(source_files))
        raise ValueError(f"Reference capture source hashes are missing: {missing}")
    return {
        "schemaVersion": 1,
        "referenceRevision": REFERENCE_REVISION,
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--reference-export", required=True, type=Path)
    parser.add_argument("--capture-output", required=True, type=Path)
    parser.add_argument("--check", type=Path)
    args = parser.parse_args()
    generated = _canonical_bytes(
        _validate_manifest(
            _load_object(args.manifest),
            args.reference_export,
            _load_object(args.capture_output),
        )
    )
    if args.check is not None:
        if args.check.read_bytes() != generated:
            print("Reference corpus differs from fixed capture", file=sys.stderr)
            return 1
        return 0
    sys.stdout.buffer.write(generated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
