from __future__ import annotations

from collections.abc import Callable
import errno
import fcntl
import json
import os
import secrets
import stat
import time
from typing import Literal, TypeVar, cast

from ._project_rules import _discovery_chain
from ._resource_state import ResourceAdmissionError, TrustPolicyError


_T = TypeVar("_T")
_MISSING = object()
_POLICY_ATTEMPTS = 10
_POLICY_RETRY_SECONDS = 0.02


def resolve_project_trust(cwd: str, choice: bool | None) -> bool:
    if choice is not None:
        return choice
    if not _has_protected_resources(cwd):
        return True
    saved = _nearest_saved_policy(cwd)
    if saved is not None:
        return saved
    return _default_project_trust() == "always"


def interactive_trust_is_pending(cwd: str) -> bool:
    if not _has_protected_resources(cwd):
        return False
    if _nearest_saved_policy(cwd) is not None:
        return False
    return _default_project_trust() == "ask"


def trust_path_has_parent(cwd: str) -> bool:
    path = os.path.realpath(cwd)
    return os.path.dirname(path) != path


def update_default_project_trust(
    value: Literal["ask", "always", "never"]
) -> None:
    target = os.path.join(
        os.path.expanduser("~"), ".omh", "agent", "settings.json"
    )

    def mutate(
        _document: dict[str, Literal["ask", "always", "never"]],
    ) -> dict[str, Literal["ask", "always", "never"]]:
        return {"defaultProjectTrust": value}

    _update_policy_document(target, _validate_settings_document, mutate)


def update_project_trust(
    cwd: str, action: Literal["trust", "distrust", "trust_parent"]
) -> None:
    trust_path = os.path.realpath(cwd)
    target = os.path.join(os.path.expanduser("~"), ".omh", "agent", "trust.json")

    def mutate(
        document: dict[str, bool | None],
    ) -> dict[str, bool | None]:
        updated = dict(document)
        if action == "trust":
            updated[trust_path] = True
        elif action == "distrust":
            updated[trust_path] = False
        else:
            parent = os.path.dirname(trust_path)
            if parent == trust_path:
                raise TrustPolicyError(operation="update", stage="document")
            updated[parent] = True
            updated.pop(trust_path, None)
        return updated

    _update_policy_document(target, _validate_trust_document, mutate)


def _has_protected_resources(cwd: str) -> bool:
    found = False
    for directory in _discovery_chain(cwd):
        if _admit_presence_path(
            os.path.join(directory, "AGENTS.md"),
            expected="file",
            kind="project_rule",
        ):
            found = True
        omh = os.path.join(directory, ".omh")
        if _admit_presence_path(omh, expected="directory", kind="project_resources"):
            for name in ("skills", "prompts"):
                if _admit_presence_path(
                    os.path.join(omh, name),
                    expected="directory",
                    kind="project_resources",
                ):
                    found = True
            if directory == cwd and _admit_presence_path(
                os.path.join(omh, "extensions"),
                expected="directory",
                kind="project_resources",
            ):
                found = True
        agents = os.path.join(directory, ".agents")
        if _admit_presence_path(
            agents, expected="directory", kind="project_resources"
        ) and _admit_presence_path(
            os.path.join(agents, "skills"),
            expected="directory",
            kind="project_resources",
        ):
            found = True
    return found


def _admit_presence_path(
    path: str,
    *,
    expected: Literal["file", "directory"],
    kind: Literal["project_resources", "project_rule"],
) -> bool:
    failed = False
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        failed = True
        info = None
    if failed:
        raise ResourceAdmissionError(
            kind=kind,
            path=os.path.abspath(path),
            stage="structure",
        )
    assert info is not None
    actual = (
        "file"
        if stat.S_ISREG(info.st_mode)
        else "directory"
        if stat.S_ISDIR(info.st_mode)
        else "other"
    )
    if actual != expected:
        raise ResourceAdmissionError(
            kind=kind,
            path=os.path.abspath(path),
            stage="structure",
        )
    return True


def _nearest_saved_policy(cwd: str) -> bool | None:
    trust_path = os.path.realpath(cwd)
    target = os.path.join(os.path.expanduser("~"), ".omh", "agent", "trust.json")
    document = _read_policy_document(target, _validate_trust_document)
    current = trust_path
    while True:
        decision = document.get(current)
        if type(decision) is bool:
            return decision
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _default_project_trust() -> Literal["ask", "always", "never"]:
    target = os.path.join(
        os.path.expanduser("~"), ".omh", "agent", "settings.json"
    )
    document = _read_policy_document(target, _validate_settings_document)
    return document.get("defaultProjectTrust", "ask")


def _read_policy_document(target: str, validate: Callable[[object], _T]) -> _T:
    lock = _acquire_policy_lock(f"{target}.lock")
    if lock is None:
        raise TrustPolicyError(operation="resolve", stage="lock")
    raw, read_stage = _read_policy_target(target)
    release_failed = _release_policy_lock(lock)
    if read_stage is not None:
        raise TrustPolicyError(operation="resolve", stage=read_stage)
    if release_failed:
        raise TrustPolicyError(operation="resolve", stage="lock")
    if raw is _MISSING:
        value: object = {}
    else:
        assert isinstance(raw, bytes)
        decoded, encoding_failed = _decode_policy(raw)
        if encoding_failed:
            raise TrustPolicyError(operation="resolve", stage="encoding")
        assert decoded is not None
        value, document_failed = _parse_policy(decoded)
        if document_failed:
            raise TrustPolicyError(operation="resolve", stage="document")
    try:
        return validate(value)
    except (TypeError, ValueError):
        pass
    raise TrustPolicyError(operation="resolve", stage="document")


def _update_policy_document(
    target: str,
    validate: Callable[[object], _T],
    mutate: Callable[[_T], _T],
) -> None:
    lock = _acquire_policy_lock(f"{target}.lock")
    if lock is None:
        raise TrustPolicyError(operation="update", stage="lock")
    committed = False
    failure: TrustPolicyError | None = None
    try:
        raw, read_stage = _read_policy_target(target)
        if read_stage is not None:
            raise TrustPolicyError(operation="update", stage=read_stage)
        if raw is _MISSING:
            value: object = {}
        else:
            assert isinstance(raw, bytes)
            decoded, encoding_failed = _decode_policy(raw)
            if encoding_failed:
                raise TrustPolicyError(operation="update", stage="encoding")
            assert decoded is not None
            parsed, document_failed = _parse_policy(decoded)
            if document_failed:
                raise TrustPolicyError(operation="update", stage="document")
            value = parsed
        try:
            current = validate(value)
        except (TypeError, ValueError):
            raise TrustPolicyError(operation="update", stage="document") from None
        payload = _serialize_policy(mutate(current))
        _commit_policy_target(target, payload)
        committed = True
    except TrustPolicyError as error:
        failure = error
    finally:
        release_failed = _release_policy_lock(lock)
    if committed:
        return
    if failure is not None:
        raise failure
    if release_failed:
        raise TrustPolicyError(operation="update", stage="lock")


def _serialize_policy(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _commit_policy_target(target: str, payload: bytes) -> None:
    directory = os.path.dirname(target)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    temporary: str | None = None
    try:
        for _attempt in range(_POLICY_ATTEMPTS):
            candidate = os.path.join(
                directory, f".omh-policy-{os.getpid()}-{secrets.token_hex(8)}.tmp"
            )
            try:
                descriptor = os.open(candidate, flags, 0o600)
            except FileExistsError:
                continue
            temporary = candidate
            break
        if descriptor is None or temporary is None:
            raise OSError("policy temporary create failed")
        os.fchmod(descriptor, 0o600)
        opened = os.fstat(descriptor)
        if not _valid_lock_carrier(opened):
            raise OSError("policy temporary is not a private regular file")
        remaining = payload
        while remaining:
            remaining = remaining[os.write(descriptor, remaining) :]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, target)
        temporary = None
    except OSError:
        raise TrustPolicyError(operation="update", stage="write") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                raise TrustPolicyError(operation="update", stage="write") from None


def _acquire_policy_lock(path: str) -> int | None:
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        existing = os.lstat(path)
    except FileNotFoundError:
        existing = None
    except OSError:
        return None
    if existing is not None and not _valid_lock_carrier(existing):
        return None
    descriptor = _open_policy_lock(path, existing is None)
    if descriptor is None:
        return None
    try:
        opened = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        return None
    if not _valid_lock_carrier(opened):
        os.close(descriptor)
        return None
    for attempt in range(_POLICY_ATTEMPTS):
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                os.close(descriptor)
                return None
            if attempt + 1 == _POLICY_ATTEMPTS:
                os.close(descriptor)
                return None
            time.sleep(_POLICY_RETRY_SECONDS)
        else:
            return descriptor
    os.close(descriptor)
    return None


def _open_policy_lock(path: str, missing: bool) -> int | None:
    flags = os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    created = False
    descriptor: int | None = None
    try:
        if missing:
            try:
                descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
            except FileExistsError:
                current = os.lstat(path)
                if not _valid_lock_carrier(current):
                    return None
                descriptor = os.open(path, flags)
        else:
            descriptor = os.open(path, flags)
        if created:
            os.fchmod(descriptor, 0o600)
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        return None
    assert descriptor is not None
    return descriptor


def _valid_lock_carrier(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600


def _release_policy_lock(descriptor: int) -> bool:
    failed = False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        failed = True
    try:
        os.close(descriptor)
    except OSError:
        failed = True
    return failed


def _read_policy_target(
    path: str,
) -> tuple[bytes | object, Literal["read"] | None]:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return _MISSING, None
    except OSError:
        return _MISSING, "read"
    if not stat.S_ISREG(info.st_mode):
        return _MISSING, "read"
    descriptor: int | None = None
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            return _MISSING, "read"
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        os.close(descriptor)
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        return _MISSING, "read"
    return b"".join(chunks), None


def _decode_policy(raw: bytes) -> tuple[str | None, bool]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, True
    if text.startswith("\ufeff") or "\0" in text:
        return None, True
    return text, False


def _parse_policy(text: str) -> tuple[object, bool]:
    failed = False
    value: object = None
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, ValueError):
        failed = True
    return value, failed


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate policy key")
        result[key] = value
    return result


def _validate_trust_document(value: object) -> dict[str, bool | None]:
    if type(value) is not dict:
        raise TypeError
    result: dict[str, bool | None] = {}
    for key, decision in cast(dict[object, object], value).items():
        if (
            type(key) is not str
            or not os.path.isabs(key)
            or "\0" in key
            or any(0xD800 <= ord(character) <= 0xDFFF for character in key)
        ):
            raise ValueError
        if decision is not None and type(decision) is not bool:
            raise TypeError
        result[key] = decision
    return result


def _validate_settings_document(
    value: object,
) -> dict[str, Literal["ask", "always", "never"]]:
    if type(value) is not dict:
        raise TypeError
    document = cast(dict[object, object], value)
    if set(document) - {"defaultProjectTrust"}:
        raise ValueError
    setting = document.get("defaultProjectTrust", "ask")
    if type(setting) is not str or setting not in {"ask", "always", "never"}:
        raise ValueError
    return {
        "defaultProjectTrust": cast(
            Literal["ask", "always", "never"], setting
        )
    }
