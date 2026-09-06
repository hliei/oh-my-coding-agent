from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
import os
import re
from typing import cast

import httpx


_LATEST_RELEASE_URL = "https://api.github.com/repos/hliei/omh/releases/latest"
_RELEASE_PAGE_PREFIX = "https://github.com/hliei/omh/releases/tag/"
_ASSET_API_PREFIX = "https://api.github.com/repos/hliei/omh/releases/assets/"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_SEMVER_LENGTH = 256
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_SEMVER = re.compile(
    r"v?(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)
_SemanticVersion = tuple[int, int, int, tuple[str, ...] | None]


@dataclass(frozen=True)
class _CandidateWheel:
    asset_id: int
    name: str
    size: int
    digest: str


@dataclass(frozen=True)
class _EligibleRelease:
    version: str
    page_url: str
    wheel: _CandidateWheel


def run_self_update() -> int:
    try:
        version = _pending_update_version()
    except Exception:
        return 1
    return 1 if version is not None else 0


def check_for_update_notice() -> str | None:
    """Return the fixed Update Notice text for a newer Eligible Update Release,
    otherwise ``None``. Any failure resolves silently to ``None`` so the caller
    receives no diagnostic detail and cannot change command status."""

    try:
        version = _pending_update_version()
    except Exception:
        return None
    if version is None:
        return None
    return f"New omh version {version} is available. Run omh update"


def startup_update_check_enabled() -> bool:
    """Return ``True`` unless ``OMH_SKIP_VERSION_CHECK`` is set to a nonempty
    value. An empty or absent variable retains the ordinary startup check."""

    return not os.environ.get("OMH_SKIP_VERSION_CHECK", "")


def _pending_update_version() -> str | None:
    """Return the eligible newer Release version relative to the installed
    Distribution, or ``None`` when there is nothing to announce."""

    release = _discover_release()
    installed = _installed_version()
    if _is_newer_version(release.version, installed):
        return release.version
    return None


def _discover_release() -> _EligibleRelease:
    with httpx.Client(
        follow_redirects=False,
        timeout=10.0,
        trust_env=False,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "omh updater",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    ) as client:
        response = client.get(_LATEST_RELEASE_URL)
        response.raise_for_status()
        payload = cast(object, response.json())
    return _eligible_release(payload)


def _eligible_release(payload: object) -> _EligibleRelease:
    if type(payload) is not dict:
        raise ValueError("release schema")
    release = cast(dict[object, object], payload)
    tag = _required_string(release, "tag_name")
    version = tag[1:] if tag.startswith("v") else tag
    if not version:
        raise ValueError("release version")
    page_url = _required_string(release, "html_url")
    if page_url != _RELEASE_PAGE_PREFIX + tag:
        raise ValueError("release page")
    if release.get("immutable") is not True:
        raise ValueError("mutable release")
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise ValueError("unstable release")
    assets = release.get("assets")
    if type(assets) is not list or len(assets) != 1:
        raise ValueError("asset inventory")
    asset_value = assets[0]
    if type(asset_value) is not dict:
        raise ValueError("asset schema")
    asset = cast(dict[object, object], asset_value)
    asset_id = asset.get("id")
    size = asset.get("size")
    if type(asset_id) is not int or asset_id <= 0:
        raise ValueError("asset id")
    if type(size) is not int or size <= 0:
        raise ValueError("asset size")
    name = _required_string(asset, "name")
    if name != f"omh-{version}-py3-none-any.whl":
        raise ValueError("candidate wheel")
    if asset.get("state") != "uploaded":
        raise ValueError("asset state")
    digest = _required_string(asset, "digest")
    if _DIGEST.fullmatch(digest) is None:
        raise ValueError("asset digest")
    if asset.get("url") != _ASSET_API_PREFIX + str(asset_id):
        raise ValueError("asset url")
    return _EligibleRelease(
        version=version,
        page_url=page_url,
        wheel=_CandidateWheel(
            asset_id=asset_id,
            name=name,
            size=size,
            digest=digest,
        ),
    )


def _required_string(values: dict[object, object], key: str) -> str:
    value = values.get(key)
    if type(value) is not str or not value:
        raise ValueError("release schema")
    return value


def _installed_version() -> str:
    version = metadata.distribution("omh").version
    if type(version) is not str or not version:
        raise ValueError("installed version")
    return version


def _is_newer_version(candidate: str, installed: str) -> bool:
    candidate_value = candidate.strip()
    installed_value = installed.strip()
    candidate_semver = _parse_semver(candidate_value)
    installed_semver = _parse_semver(installed_value)
    if candidate_semver is None or installed_semver is None:
        return candidate_value != installed_value
    return _compare_semver(candidate_semver, installed_semver) > 0


def _parse_semver(value: str) -> _SemanticVersion | None:
    if len(value) > _MAX_SEMVER_LENGTH:
        return None
    match = _SEMVER.fullmatch(value)
    if match is None:
        return None
    core = tuple(int(match.group(index)) for index in range(1, 4))
    if any(part > _MAX_SAFE_INTEGER for part in core):
        return None
    prerelease = match.group(4)
    return (
        core[0],
        core[1],
        core[2],
        None if prerelease is None else tuple(prerelease.split(".")),
    )


def _compare_semver(left: _SemanticVersion, right: _SemanticVersion) -> int:
    if left[:3] != right[:3]:
        return 1 if left[:3] > right[:3] else -1
    left_pre = left[3]
    right_pre = right[3]
    if left_pre is None or right_pre is None:
        if left_pre is right_pre:
            return 0
        return 1 if left_pre is None else -1
    for left_id, right_id in zip(left_pre, right_pre, strict=False):
        if left_id == right_id:
            continue
        left_numeric = left_id.isdigit()
        right_numeric = right_id.isdigit()
        if left_numeric and right_numeric:
            left_number = float(left_id)
            right_number = float(right_id)
            if left_number == right_number:
                return 0
            return 1 if left_number > right_number else -1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return 1 if left_id > right_id else -1
    if len(left_pre) == len(right_pre):
        return 0
    return 1 if len(left_pre) > len(right_pre) else -1
