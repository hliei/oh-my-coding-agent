from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
import fcntl
import hashlib
from importlib import metadata
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import BinaryIO, Iterator, cast
from urllib.parse import urljoin, urlsplit
from zipfile import BadZipFile, ZipFile

import httpx


_LATEST_RELEASE_URL = "https://api.github.com/repos/hliei/omh/releases/latest"
_RELEASE_PAGE_PREFIX = "https://github.com/hliei/omh/releases/tag/"
_ASSET_API_PREFIX = "https://api.github.com/repos/hliei/omh/releases/assets/"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_SEMVER_LENGTH = 256
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_MAX_WHEEL_BYTES = 32 * 1024 * 1024
_MAX_WHEEL_METADATA_BYTES = 64 * 1024
_MAX_UV_PROBE_OUTPUT = 65_536
_OWNED_ENTRY_POINT = "oh_my_coding_agent._cli:main"
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


@dataclass(frozen=True)
class _UpdateOwnership:
    uv: Path
    environment: Path
    entry_point: Path
    python: Path
    python_minor: str


def run_self_update() -> tuple[int, str | None]:
    try:
        pending = _pending_update_release()
    except Exception:
        return 1, None
    if pending is None:
        return 0, None
    release, installed_version = pending
    try:
        ownership = _prove_uv_ownership(installed_version)
    except Exception:
        return 1, f"Update manually from {release.page_url}\n"
    try:
        with _acquire_update_lock():
            with _verified_update_wheel(release) as wheel:
                _install_update_wheel(ownership, wheel)
                updated = _prove_uv_ownership(release.version)
                if (
                    updated.environment != ownership.environment
                    or updated.entry_point != ownership.entry_point
                    or updated.python != ownership.python
                    or updated.python_minor != ownership.python_minor
                ):
                    raise ValueError("update postcondition")
    except Exception:
        return 1, None
    return 0, None


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

    pending = _pending_update_release()
    return None if pending is None else pending[0].version


def _pending_update_release() -> tuple[_EligibleRelease, str] | None:
    release = _discover_release()
    installed = _installed_version()
    if _is_newer_version(release.version, installed):
        return release, installed
    return None


def _prove_uv_ownership(installed_version: str) -> _UpdateOwnership:
    uv = _resolved_command("uv")
    resolved_entry_point = _resolved_command("omh")
    distribution = metadata.distribution("omh")
    if distribution.version != installed_version:
        raise ValueError("distribution version")
    if distribution.metadata.get("Name") != "omh":
        raise ValueError("distribution name")
    if distribution.read_text("INSTALLER") != "uv":
        raise ValueError("distribution installer")
    entry_points = tuple(
        entry_point
        for entry_point in distribution.entry_points
        if entry_point.group == "console_scripts" and entry_point.name == "omh"
    )
    if len(entry_points) != 1 or entry_points[0].value != _OWNED_ENTRY_POINT:
        raise ValueError("distribution entry point")

    environment = _resolve_existing_directory(Path(sys.prefix))
    if _resolve_existing_directory(Path(sys.exec_prefix)) != environment:
        raise ValueError("interpreter environment")
    environment_bin = _resolve_existing_directory(environment / "bin")
    executable = Path(sys.executable)
    if not executable.is_absolute() or executable.parent.resolve(strict=True) != environment_bin:
        raise ValueError("interpreter executable")
    if not executable.samefile(environment_bin / "python"):
        raise ValueError("interpreter executable")
    distribution_root = _resolve_existing_directory(
        cast(Path, distribution.locate_file(""))
    )
    if not distribution_root.is_relative_to(environment):
        raise ValueError("distribution environment")

    tool_directory_text = _run_uv_probe(
        uv,
        ("tool", "dir", "--offline", "--no-config", "--color", "never", "--no-progress"),
    )
    tool_directory_lines = tool_directory_text.splitlines()
    if len(tool_directory_lines) != 1:
        raise ValueError("uv tool directory")
    tool_directory = _resolve_existing_directory(Path(tool_directory_lines[0]))
    expected_environment = _resolve_existing_directory(tool_directory / "omh")
    if expected_environment != environment:
        raise ValueError("uv tool environment")

    listing = _run_uv_probe(
        uv,
        (
            "tool",
            "list",
            "--show-paths",
            "--show-python",
            "--offline",
            "--no-config",
            "--color",
            "never",
            "--no-progress",
        ),
    )
    listed_environment, listed_entry_point, listed_python = _listed_omh_tool(
        listing, installed_version
    )
    if _resolve_existing_directory(listed_environment) != environment:
        raise ValueError("listed environment")
    if listed_python != (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    ):
        raise ValueError("listed interpreter")
    listed_entry = _resolve_existing_file(listed_entry_point)
    environment_entry = _resolve_existing_file(environment_bin / "omh")
    if listed_entry != resolved_entry_point or listed_entry != environment_entry:
        raise ValueError("resolved entry point")
    return _UpdateOwnership(
        uv=uv,
        environment=environment,
        entry_point=listed_entry,
        python=executable.resolve(strict=True),
        python_minor=f"{sys.version_info.major}.{sys.version_info.minor}",
    )


def _resolved_command(name: str) -> Path:
    value = shutil.which(name)
    if value is None:
        raise ValueError("missing command")
    return _resolve_existing_file(Path(value))


def _resolve_existing_file(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("relative path")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("not a file")
    return resolved


def _resolve_existing_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("relative path")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("not a directory")
    return resolved


def _run_uv_probe(uv: Path, arguments: tuple[str, ...]) -> str:
    completed = subprocess.run(
        (os.fspath(uv), *arguments),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("uv probe")
    if len(completed.stdout) > _MAX_UV_PROBE_OUTPUT:
        raise ValueError("uv probe output")
    return completed.stdout.decode("utf-8", errors="strict")


def _listed_omh_tool(listing: str, installed_version: str) -> tuple[Path, Path, str]:
    header = re.compile(
        rf"omh v{re.escape(installed_version)} "
        r"\[CPython (?P<python>[0-9]+\.[0-9]+\.[0-9]+)\] "
        r"\((?P<environment>[^\r\n]+)\)"
    )
    entry = re.compile(r"- omh \((?P<entry>[^\r\n]+)\)")
    lines = listing.splitlines()
    omh_indexes = [index for index, line in enumerate(lines) if line.startswith("omh ")]
    if len(omh_indexes) != 1:
        raise ValueError("uv tool listing")
    index = omh_indexes[0]
    header_match = header.fullmatch(lines[index])
    if header_match is None:
        raise ValueError("uv tool listing")
    entry_lines: list[str] = []
    for following in lines[index + 1 :]:
        if not following.startswith("- "):
            break
        entry_lines.append(following)
    entry_matches = [
        match for line in entry_lines if (match := entry.fullmatch(line)) is not None
    ]
    if len(entry_matches) != 1:
        raise ValueError("uv tool listing")
    return (
        Path(header_match.group("environment")),
        Path(entry_matches[0].group("entry")),
        header_match.group("python"),
    )


@contextmanager
def _acquire_update_lock() -> Iterator[None]:
    directory = Path.home() / ".cache" / "omh"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "update.lock"
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
            raise ValueError("update lock")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("update lock") from error
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def _verified_update_wheel(release: _EligibleRelease) -> Iterator[Path]:
    if release.wheel.size > _MAX_WHEEL_BYTES:
        raise ValueError("candidate wheel size")
    directory = Path(tempfile.mkdtemp(prefix="omh-update-"))
    wheel = directory / release.wheel.name
    descriptor = os.open(
        wheel,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            _download_update_wheel(release.wheel, output)
        _verify_update_wheel(wheel, release.version)
        yield wheel
    finally:
        wheel.unlink(missing_ok=True)
        directory.rmdir()


def _download_update_wheel(candidate: _CandidateWheel, output: BinaryIO) -> None:
    url = _ASSET_API_PREFIX + str(candidate.asset_id)
    redirects = 0
    byte_count = 0
    digest = hashlib.sha256()
    with httpx.Client(
        follow_redirects=False,
        timeout=30.0,
        trust_env=False,
        headers={
            "Accept": "application/octet-stream",
            "Accept-Encoding": "identity",
            "User-Agent": "omh updater",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    ) as client:
        while True:
            _require_credential_free_https(url)
            with client.stream("GET", url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirects >= 5:
                        raise ValueError("candidate wheel redirects")
                    location = response.headers.get("Location")
                    if location is None:
                        raise ValueError("candidate wheel redirect")
                    url = urljoin(url, location)
                    redirects += 1
                    continue
                response.raise_for_status()
                content_encoding = response.headers.get("Content-Encoding")
                if content_encoding not in {None, "identity"}:
                    raise ValueError("candidate wheel encoding")
                for chunk in response.iter_bytes():
                    byte_count += len(chunk)
                    if byte_count > candidate.size or byte_count > _MAX_WHEEL_BYTES:
                        raise ValueError("candidate wheel size")
                    output.write(chunk)
                    digest.update(chunk)
                break
    if byte_count != candidate.size:
        raise ValueError("candidate wheel size")
    if "sha256:" + digest.hexdigest() != candidate.digest:
        raise ValueError("candidate wheel digest")


def _require_credential_free_https(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("candidate wheel url")


def _verify_update_wheel(wheel: Path, version: str) -> None:
    try:
        with ZipFile(wheel) as archive:
            metadata_member, wheel_member = _wheel_metadata_members(archive)
            metadata_message = _parse_wheel_message(
                _read_bounded_member(archive, metadata_member)
            )
            wheel_message = _parse_wheel_message(
                _read_bounded_member(archive, wheel_member)
            )
    except (BadZipFile, OSError, RuntimeError) as error:
        raise ValueError("candidate wheel archive") from error
    name = _unique_header(metadata_message, "Name")
    wheel_version = _unique_header(metadata_message, "Version")
    tags = wheel_message.get_all("Tag", failobj=[])
    if (
        re.sub(r"[-_.]+", "-", name).lower() != "omh"
        or wheel_version != version
        or tags != ["py3-none-any"]
    ):
        raise ValueError("candidate wheel metadata")


def _wheel_metadata_members(archive: ZipFile) -> tuple[str, str]:
    metadata_members = [
        member.filename
        for member in archive.infolist()
        if member.filename.split("/")[-1] == "METADATA"
    ]
    wheel_members = [
        member.filename
        for member in archive.infolist()
        if member.filename.split("/")[-1] == "WHEEL"
    ]
    if len(metadata_members) != 1 or len(wheel_members) != 1:
        raise ValueError("candidate wheel metadata members")
    metadata_parts = metadata_members[0].split("/")
    wheel_parts = wheel_members[0].split("/")
    if (
        len(metadata_parts) != 2
        or len(wheel_parts) != 2
        or metadata_parts[0] != wheel_parts[0]
        or not metadata_parts[0].endswith(".dist-info")
    ):
        raise ValueError("candidate wheel metadata members")
    return metadata_members[0], wheel_members[0]


def _read_bounded_member(archive: ZipFile, name: str) -> bytes:
    member = archive.getinfo(name)
    if member.file_size > _MAX_WHEEL_METADATA_BYTES:
        raise ValueError("candidate wheel metadata size")
    with archive.open(member) as source:
        value = source.read(_MAX_WHEEL_METADATA_BYTES + 1)
    if len(value) > _MAX_WHEEL_METADATA_BYTES:
        raise ValueError("candidate wheel metadata size")
    return value


def _parse_wheel_message(value: bytes) -> Message:
    message = BytesParser(policy=policy.compat32).parsebytes(value)
    if message.defects or message.is_multipart():
        raise ValueError("candidate wheel metadata")
    return message


def _unique_header(message: Message, name: str) -> str:
    values = message.get_all(name, failobj=[])
    if len(values) != 1 or not values[0]:
        raise ValueError("candidate wheel metadata")
    return values[0]


def _install_update_wheel(ownership: _UpdateOwnership, wheel: Path) -> None:
    completed = subprocess.run(
        (
            os.fspath(ownership.uv),
            "tool",
            "install",
            "--python",
            os.fspath(ownership.python),
            os.fspath(wheel),
        ),
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("uv install")


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
