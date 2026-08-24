from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import datetime, timezone
import inspect
import json
import os
from pathlib import Path
import re
import secrets
import time
from types import MappingProxyType
from typing import Any, BinaryIO, Literal, TypeAlias, cast, final
from urllib.parse import unquote, urlparse
import uuid

from oh_my_core import AgentMessage
from oh_my_llm import (
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
)


CURRENT_SESSION_VERSION: Literal[3] = 3
_MANAGER_TOKEN = object()


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class SessionHeader:
    id: str
    timestamp: str
    cwd: str
    parentSession: str | None = None
    type: Literal["session"] = field(init=False, default="session")
    version: Literal[3] = field(init=False, default=CURRENT_SESSION_VERSION)


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class NewSessionOptions:
    id: str | None = None
    parentSession: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionEntryBase:
    type: str
    id: str
    parentId: str | None
    timestamp: str


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class SessionMessageEntry(SessionEntryBase):
    message: AgentMessage
    type: Literal["message"] = field(init=False, default="message")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class ThinkingLevelChangeEntry(SessionEntryBase):
    thinkingLevel: str
    type: Literal["thinking_level_change"] = field(
        init=False, default="thinking_level_change"
    )


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class ModelChangeEntry(SessionEntryBase):
    provider: str
    modelId: str
    type: Literal["model_change"] = field(init=False, default="model_change")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class CompactionEntry(SessionEntryBase):
    summary: str
    firstKeptEntryId: str
    tokensBefore: int
    details: object | None = None
    fromHook: bool = False
    type: Literal["compaction"] = field(init=False, default="compaction")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class BranchSummaryEntry(SessionEntryBase):
    fromId: str
    summary: str
    details: object | None = None
    fromHook: bool = False
    type: Literal["branch_summary"] = field(init=False, default="branch_summary")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class CustomEntry(SessionEntryBase):
    customType: str
    data: object | None = None
    type: Literal["custom"] = field(init=False, default="custom")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class CustomMessageEntry(SessionEntryBase):
    customType: str
    content: str | tuple[TextContent, ...]
    display: bool
    details: object | None = None
    type: Literal["custom_message"] = field(init=False, default="custom_message")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class LabelEntry(SessionEntryBase):
    targetId: str
    label: str | None
    type: Literal["label"] = field(init=False, default="label")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class SessionInfoEntry(SessionEntryBase):
    name: str | None = None
    type: Literal["session_info"] = field(init=False, default="session_info")


SessionEntry: TypeAlias = (
    SessionMessageEntry
    | ThinkingLevelChangeEntry
    | ModelChangeEntry
    | CompactionEntry
    | BranchSummaryEntry
    | CustomEntry
    | CustomMessageEntry
    | LabelEntry
    | SessionInfoEntry
)
FileEntry: TypeAlias = SessionHeader | SessionEntry


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class SessionTreeNode:
    entry: SessionEntry
    children: tuple["SessionTreeNode", ...]
    label: str | None = None
    labelTimestamp: str | None = None


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class SessionContext:
    messages: tuple[AgentMessage, ...]
    thinkingLevel: str
    model: Mapping[str, str] | None


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class SessionInfo:
    path: str
    id: str
    cwd: str
    created: datetime
    modified: datetime
    messageCount: int
    firstMessage: str
    allMessagesText: str
    name: str | None = None
    parentSessionPath: str | None = None


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class CompactionResult:
    summary: str
    firstKeptEntryId: str
    tokensBefore: int
    estimatedTokensAfter: int | None = None
    details: object | None = None


_Progress: TypeAlias = Callable[[int, int], None | Awaitable[None]]


def _string(value: object, path: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{path}: must be a string")
    return value


def _normalize_path(value: object, path: str) -> str:
    text = _string(value, path)
    if text == "~":
        return os.path.expanduser("~")
    if text.startswith("~/"):
        return os.path.join(os.path.expanduser("~"), text[2:])
    if text.startswith("file://"):
        parsed = urlparse(text)
        if parsed.netloc not in ("", "localhost"):
            raise ValueError(f"{path}: file URL host is not local")
        return unquote(parsed.path)
    return text


def _resolve_path(value: object, path: str) -> str:
    normalized = _normalize_path(value, path)
    resolved = os.path.abspath(normalized)
    if os.sep == "/" and resolved.startswith("//"):
        resolved = "/" + resolved.lstrip("/")
    return resolved


def _default_session_dir(cwd: str) -> str:
    encoded = cwd.lstrip("/\\").replace("/", "-").replace("\\", "-")
    encoded = encoded.replace(":", "-")
    return os.path.join(os.path.expanduser("~"), ".omh", "agent", "sessions", f"--{encoded}--")


def _ensure_session_dir(path: str) -> None:
    if path and not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _uuid7() -> str:
    milliseconds = int(time.time() * 1000) & ((1 << 48) - 1)
    random_bits = secrets.randbits(74)
    integer = milliseconds << 80
    integer |= 0x7 << 76
    integer |= ((random_bits >> 62) & 0xFFF) << 64
    integer |= 0b10 << 62
    integer |= random_bits & ((1 << 62) - 1)
    return str(uuid.UUID(int=integer))


def _session_id(options: NewSessionOptions | None) -> str:
    if options is not None and type(options) is not NewSessionOptions:
        raise TypeError("options must be a NewSessionOptions")
    selected = None if options is None else options.id
    if selected is None:
        return _uuid7()
    _string(selected, "NewSessionOptions.id")
    if not selected[0:1].isascii() or not selected[-1:].isascii():
        raise ValueError("NewSessionOptions.id: invalid session id")
    if not selected[0:1].isalnum() or not selected[-1:].isalnum():
        raise ValueError("NewSessionOptions.id: invalid session id")
    if any(not (character.isascii() and (character.isalnum() or character in "._-")) for character in selected):
        raise ValueError("NewSessionOptions.id: invalid session id")
    return selected


def _header_value(header: SessionHeader) -> dict[str, object]:
    value: dict[str, object] = {
        "type": header.type,
        "version": header.version,
        "id": header.id,
        "timestamp": header.timestamp,
        "cwd": header.cwd,
    }
    if header.parentSession is not None:
        value["parentSession"] = header.parentSession
    return value


def _header_json(header: SessionHeader) -> bytes:
    return _json_line(_header_value(header))


def _json_value(value: object) -> object:
    if value is None or type(value) in (bool, int, float, str):
        return value
    if type(value) in (list, tuple):
        return [_json_value(item) for item in cast(Sequence[object], value)]
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        result: dict[str, object] = {}
        for item in fields(value):
            item_value = getattr(value, item.name)
            if item_value is None and item.default is None:
                continue
            result[item.name] = _json_value(item_value)
        return result
    raise TypeError(f"Session JSON does not support {type(value).__name__}")


def _entry_json(entry: SessionEntry) -> bytes:
    return _json_line(_json_value(entry))


def _json_line(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    ) + b"\n"


def _write_bytes(output: BinaryIO, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = output.write(data[offset:])
        if written is None or written <= 0:
            raise OSError("Session file write made no progress")
        offset += written


def _rewrite_values(path: str, values: Sequence[object]) -> None:
    with open(path, "wb", buffering=0) as output:
        for value in values:
            _write_bytes(output, _json_line(value))


def _read_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


def _parsed_values(path: str) -> list[object]:
    values: list[object] = []
    for physical_line in _read_bytes(path).split(b"\n"):
        if not physical_line.strip():
            continue
        try:
            values.append(json.loads(physical_line))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return values


def _read_header_value(values: Sequence[object], path: str) -> SessionHeader:
    parsed = values[0] if values else None
    if not isinstance(parsed, dict) or parsed.get("type") != "session":
        raise ValueError(f"Session file is not a valid omh session: {path}")
    identifier = parsed.get("id")
    timestamp = parsed.get("timestamp")
    cwd = parsed.get("cwd")
    parent = parsed.get("parentSession")
    if type(identifier) is not str:
        raise ValueError(f"Session file is not a valid omh session: {path}")
    return SessionHeader(
        id=identifier,
        timestamp=timestamp if type(timestamp) is str else "",
        cwd=cwd if type(cwd) is str else "",
        parentSession=parent if type(parent) is str else None,
    )


def _probe_header(path: str) -> SessionHeader | None:
    try:
        with open(path, "rb") as source:
            first_line = source.read(512).split(b"\n", 1)[0]
        if not first_line:
            return None
        value = json.loads(first_line)
        if (
            not isinstance(value, dict)
            or value.get("type") != "session"
            or type(value.get("id")) is not str
        ):
            return None
        return _read_header_value((value,), path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None


def _legacy_entry_id(used: set[str]) -> str:
    for _ in range(100):
        identifier = uuid.uuid4().hex[:8]
        if identifier not in used:
            used.add(identifier)
            return identifier
    identifier = str(uuid.uuid4())
    used.add(identifier)
    return identifier


def _migrate_values(values: list[object]) -> bool:
    header = values[0] if values else None
    if not isinstance(header, dict) or header.get("type") != "session":
        return False
    raw_version = header.get("version")
    version = raw_version if type(raw_version) is int else 1
    if version >= CURRENT_SESSION_VERSION:
        return False
    if version < 2:
        used: set[str] = set()
        previous: str | None = None
        header["version"] = 2
        for value in values[1:]:
            if not isinstance(value, dict):
                continue
            identifier = _legacy_entry_id(used)
            value["id"] = identifier
            value["parentId"] = previous
            previous = identifier
            if value.get("type") == "compaction":
                index = value.get("firstKeptEntryIndex")
                if type(index) is int and 0 <= index < len(values):
                    target = values[index]
                    if (
                        isinstance(target, dict)
                        and target.get("type") != "session"
                        and type(target.get("id")) is str
                    ):
                        value["firstKeptEntryId"] = target["id"]
                value.pop("firstKeptEntryIndex", None)
    if version < 3:
        header["version"] = 3
        for value in values[1:]:
            if not isinstance(value, dict) or value.get("type") != "message":
                continue
            message = value.get("message")
            if isinstance(message, dict) and message.get("role") == "hookMessage":
                message["role"] = "custom"
    return True


def _parse_datetime(value: object) -> datetime | None:
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _message_text(value: object) -> str:
    if type(value) is str:
        return value
    if type(value) is not list:
        return ""
    parts: list[str] = []
    for item in cast(list[object], value):
        if (
            isinstance(item, dict)
            and item.get("type") == "text"
            and type(item.get("text")) is str
        ):
            parts.append(cast(str, item["text"]))
    return " ".join(parts)


def _session_info(path: str) -> SessionInfo | None:
    try:
        stat = os.stat(path)
        values = _parsed_values(path)
        header = _read_header_value(values, path)
        created = _parse_datetime(header.timestamp)
        if created is None:
            created = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        name: str | None = None
        message_count = 0
        first_message = ""
        all_messages: list[str] = []
        last_activity: datetime | None = None
        for value in values[1:]:
            if not isinstance(value, dict):
                continue
            if value.get("type") == "session_info":
                raw_name = value.get("name")
                name = (raw_name.strip() or None) if type(raw_name) is str else None
                continue
            if value.get("type") != "message":
                continue
            message = value.get("message")
            if not isinstance(message, dict):
                continue
            message_count += 1
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            timestamp = message.get("timestamp")
            activity = (
                datetime.fromtimestamp(timestamp / 1000, timezone.utc)
                if type(timestamp) is int
                else _parse_datetime(value.get("timestamp"))
            )
            if activity is not None and activity.timestamp() > 0 and (
                last_activity is None or activity > last_activity
            ):
                last_activity = activity
            content = _message_text(message.get("content"))
            if not content:
                continue
            all_messages.append(content)
            if not first_message and role == "user":
                first_message = content
        return SessionInfo(
            path=path,
            id=header.id,
            cwd=header.cwd,
            name=name,
            parentSessionPath=header.parentSession,
            created=created,
            modified=last_activity or created,
            messageCount=message_count,
            firstMessage=first_message or "(no messages)",
            allMessagesText=" ".join(all_messages),
        )
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _text_content(value: object) -> TextContent:
    if type(value) is not dict:
        raise ValueError("Session Message content is invalid")
    raw = cast(dict[str, Any], value)
    return TextContent(
        text=raw["text"],
        textSignature=raw.get("textSignature"),
    )


def _tool_call(value: object) -> ToolCall:
    if type(value) is not dict:
        raise ValueError("Session Tool Call is invalid")
    raw = cast(dict[str, Any], value)
    return ToolCall(
        id=raw["id"],
        name=raw["name"],
        arguments=raw["arguments"],
        thoughtSignature=raw.get("thoughtSignature"),
    )


def _usage(value: object) -> Usage:
    if type(value) is not dict:
        raise ValueError("Session Usage is invalid")
    raw = cast(dict[str, Any], value)
    cost_value = raw["cost"]
    if type(cost_value) is not dict:
        raise ValueError("Session Usage cost is invalid")
    cost = cast(dict[str, Any], cost_value)
    return Usage(
        input=raw["input"],
        output=raw["output"],
        cacheRead=raw["cacheRead"],
        cacheWrite=raw["cacheWrite"],
        totalTokens=raw["totalTokens"],
        cacheWrite1h=raw.get("cacheWrite1h"),
        cost=UsageCost(
            input=float(cost["input"]),
            output=float(cost["output"]),
            cacheRead=float(cost["cacheRead"]),
            cacheWrite=float(cost["cacheWrite"]),
            total=float(cost["total"]),
        ),
    )


def _message(value: object) -> AgentMessage:
    if type(value) is not dict:
        raise ValueError("Session Message is invalid")
    raw = cast(dict[str, Any], value)
    role = raw.get("role")
    content_value = raw.get("content")
    if role == "user":
        content = (
            content_value
            if type(content_value) is str
            else tuple(_text_content(item) for item in cast(list[object], content_value))
        )
        return UserMessage(content=content, timestamp=raw["timestamp"])
    if role == "assistant":
        assistant_content = tuple(
            _text_content(item)
            if type(item) is dict and item.get("type") == "text"
            else _tool_call(item)
            for item in cast(list[object], content_value)
        )
        return AssistantMessage(
            content=assistant_content,
            api=raw["api"],
            provider=raw["provider"],
            model=raw["model"],
            usage=_usage(raw["usage"]),
            stopReason=raw["stopReason"],
            timestamp=raw["timestamp"],
            responseModel=raw.get("responseModel"),
            responseId=raw.get("responseId"),
            errorMessage=raw.get("errorMessage"),
        )
    if role == "toolResult":
        return ToolResultMessage(
            toolCallId=raw["toolCallId"],
            toolName=raw["toolName"],
            content=tuple(
                _text_content(item) for item in cast(list[object], content_value)
            ),
            details=raw.get("details"),
            isError=raw["isError"],
            timestamp=raw["timestamp"],
        )
    raise ValueError("Session Message role is invalid")


def _entry(value: object) -> SessionEntry:
    if type(value) is not dict:
        raise ValueError("Session entry is invalid")
    raw = cast(dict[str, Any], value)
    common: dict[str, Any] = {
        "id": raw["id"],
        "parentId": raw.get("parentId"),
        "timestamp": raw["timestamp"],
    }
    entry_type = raw.get("type")
    if entry_type == "message":
        return SessionMessageEntry(message=_message(raw["message"]), **common)
    if entry_type == "thinking_level_change":
        return ThinkingLevelChangeEntry(
            thinkingLevel=raw["thinkingLevel"], **common
        )
    if entry_type == "model_change":
        return ModelChangeEntry(
            provider=raw["provider"], modelId=raw["modelId"], **common
        )
    if entry_type == "compaction":
        return CompactionEntry(
            summary=raw["summary"],
            firstKeptEntryId=raw["firstKeptEntryId"],
            tokensBefore=raw["tokensBefore"],
            details=raw.get("details"),
            fromHook=raw.get("fromHook", False),
            **common,
        )
    if entry_type == "branch_summary":
        return BranchSummaryEntry(
            fromId=raw["fromId"],
            summary=raw["summary"],
            details=raw.get("details"),
            fromHook=raw.get("fromHook", False),
            **common,
        )
    if entry_type == "custom":
        return CustomEntry(
            customType=raw["customType"], data=raw.get("data"), **common
        )
    if entry_type == "custom_message":
        content = raw["content"]
        return CustomMessageEntry(
            customType=raw["customType"],
            content=content
            if type(content) is str
            else tuple(_text_content(item) for item in cast(list[object], content)),
            display=raw["display"],
            details=raw.get("details"),
            **common,
        )
    if entry_type == "label":
        return LabelEntry(
            targetId=raw["targetId"], label=raw.get("label"), **common
        )
    if entry_type == "session_info":
        return SessionInfoEntry(name=raw.get("name"), **common)
    raise ValueError("Session entry type is invalid")


@final
class SessionManager:
    _by_id: dict[str, SessionEntry]
    _cwd: str
    _entries: list[SessionEntry]
    _flushed: bool
    _header: SessionHeader
    _labels: dict[str, str]
    _label_timestamps: dict[str, str]
    _leaf_id: str | None
    _persist: bool
    _session_dir: str
    _session_file: str | None

    __slots__ = (
        "_by_id",
        "_cwd",
        "_entries",
        "_flushed",
        "_header",
        "_labels",
        "_label_timestamps",
        "_leaf_id",
        "_persist",
        "_session_dir",
        "_session_file",
    )

    def __init__(self, *, _token: object) -> None:
        if _token is not _MANAGER_TOKEN:
            raise TypeError("SessionManager values are factory-produced")

    @classmethod
    def _blank(
        cls,
        cwd: str,
        session_dir: str,
        persist: bool,
    ) -> SessionManager:
        manager = cls(_token=_MANAGER_TOKEN)
        manager._cwd = cwd
        manager._session_dir = session_dir
        manager._persist = persist
        manager._session_file = None
        manager._header = SessionHeader(id=_uuid7(), timestamp=_timestamp(), cwd=cwd)
        manager._entries = []
        manager._by_id = {}
        manager._labels = {}
        manager._label_timestamps = {}
        manager._leaf_id = None
        manager._flushed = False
        return manager

    @classmethod
    def create(
        cls,
        cwd: str,
        sessionDir: str | None = None,
        options: NewSessionOptions | None = None,
    ) -> SessionManager:
        resolved_cwd = _resolve_path(cwd, "SessionManager.create.cwd")
        identifier = _session_id(options)
        directory = (
            _default_session_dir(resolved_cwd)
            if sessionDir is None
            else _normalize_path(sessionDir, "SessionManager.create.sessionDir")
        )
        _ensure_session_dir(directory)
        manager = cls._blank(resolved_cwd, directory, True)
        manager._start_new(identifier, None if options is None else options.parentSession)
        return manager

    @classmethod
    def open(
        cls,
        path: str,
        sessionDir: str | None = None,
        cwdOverride: str | None = None,
    ) -> SessionManager:
        resolved_path = _resolve_path(path, "SessionManager.open.path")
        override = (
            None
            if cwdOverride is None
            else _resolve_path(cwdOverride, "SessionManager.open.cwdOverride")
        )
        directory = (
            os.path.dirname(resolved_path)
            if sessionDir is None
            else _normalize_path(sessionDir, "SessionManager.open.sessionDir")
        )
        _ensure_session_dir(directory)
        if not os.path.exists(resolved_path):
            manager = cls._blank(override or os.getcwd(), directory, True)
            manager._start_new(_uuid7(), None)
            manager._session_file = resolved_path
            return manager
        if os.path.getsize(resolved_path) == 0:
            manager = cls._blank(override or os.getcwd(), directory, True)
            manager._start_new(_uuid7(), None)
            manager._session_file = resolved_path
            _rewrite_values(resolved_path, (_header_value(manager._header),))
            manager._flushed = True
            return manager
        values = _parsed_values(resolved_path)
        header = _read_header_value(values, resolved_path)
        if _migrate_values(values):
            _rewrite_values(resolved_path, values)
            header = _read_header_value(values, resolved_path)
        manager = cls._blank(
            override or _resolve_path(header.cwd, "SessionHeader.cwd"),
            directory,
            True,
        )
        manager._header = header
        manager._session_file = resolved_path
        manager._flushed = True
        for value in values[1:]:
            try:
                entry = _entry(value)
            except (KeyError, TypeError, ValueError):
                continue
            manager._entries.append(entry)
            manager._index_entry(entry)
        return manager

    @classmethod
    def continueRecent(
        cls, cwd: str, sessionDir: str | None = None
    ) -> SessionManager:
        resolved_cwd = _resolve_path(cwd, "SessionManager.continueRecent.cwd")
        directory = (
            _default_session_dir(resolved_cwd)
            if sessionDir is None
            else _normalize_path(sessionDir, "SessionManager.continueRecent.sessionDir")
        )
        if sessionDir is None:
            _ensure_session_dir(directory)
        filter_cwd = sessionDir is not None and directory != _default_session_dir(
            resolved_cwd
        )
        try:
            candidates = sorted(
                Path(directory).glob("*.jsonl") if os.path.isdir(directory) else (),
                key=lambda item: item.stat().st_mtime_ns,
                reverse=True,
            )
        except OSError:
            candidates = []
        for candidate in candidates:
            header = _probe_header(os.fspath(candidate))
            if header is None:
                continue
            if filter_cwd and (
                not header.cwd
                or _resolve_path(header.cwd, "SessionHeader.cwd") != resolved_cwd
            ):
                continue
            return cls.open(os.fspath(candidate), directory, resolved_cwd)
        return cls.create(resolved_cwd, sessionDir)

    @classmethod
    def inMemory(
        cls,
        cwd: str | None = None,
        options: NewSessionOptions | None = None,
    ) -> SessionManager:
        resolved_cwd = _resolve_path(
            os.getcwd() if cwd is None else cwd, "SessionManager.inMemory.cwd"
        )
        identifier = _session_id(options)
        manager = cls._blank(resolved_cwd, "", False)
        manager._start_new(identifier, None if options is None else options.parentSession)
        return manager

    @classmethod
    def forkFrom(
        cls,
        sourcePath: str,
        targetCwd: str,
        sessionDir: str | None = None,
        options: NewSessionOptions | None = None,
    ) -> SessionManager:
        source = _resolve_path(sourcePath, "SessionManager.forkFrom.sourcePath")
        target = _resolve_path(targetCwd, "SessionManager.forkFrom.targetCwd")
        source_values = _parsed_values(source)
        _read_header_value(source_values, source)
        identifier = _session_id(options)
        directory = (
            _default_session_dir(target)
            if sessionDir is None
            else _normalize_path(sessionDir, "SessionManager.forkFrom.sessionDir")
        )
        _ensure_session_dir(directory)
        manager = cls._blank(target, directory, True)
        manager._start_new(identifier, source)
        assert manager._session_file is not None
        with open(manager._session_file, "xb", buffering=0) as output:
            _write_bytes(output, _header_json(manager._header))
            for value in source_values[1:]:
                _write_bytes(output, _json_line(value))
        return cls.open(manager._session_file, directory, target)

    @classmethod
    async def list(
        cls,
        cwd: str,
        sessionDir: str | None = None,
        onProgress: _Progress | None = None,
    ) -> tuple[SessionInfo, ...]:
        resolved_cwd = _resolve_path(cwd, "SessionManager.list.cwd")
        directory = (
            _default_session_dir(resolved_cwd)
            if sessionDir is None
            else _normalize_path(sessionDir, "SessionManager.list.sessionDir")
        )
        if sessionDir is None:
            _ensure_session_dir(directory)
        filter_cwd = sessionDir is not None and directory != _default_session_dir(
            resolved_cwd
        )
        return await cls._list_dir(
            directory,
            resolved_cwd if filter_cwd else None,
            onProgress,
        )

    @classmethod
    async def listAll(
        cls,
        sessionDir: str | None = None,
        onProgress: _Progress | None = None,
    ) -> tuple[SessionInfo, ...]:
        if sessionDir is not None:
            directory = _normalize_path(sessionDir, "SessionManager.listAll.sessionDir")
            return await cls._list_dir(directory, None, onProgress)
        root = os.path.join(os.path.expanduser("~"), ".omh", "agent", "sessions")
        if not os.path.isdir(root):
            return ()
        try:
            candidates = tuple(
                item
                for directory in Path(root).iterdir()
                if directory.is_dir()
                for item in directory.glob("*.jsonl")
            )
        except OSError:
            return ()
        return await cls._list_paths(candidates, None, onProgress)

    @classmethod
    async def _list_dir(
        cls,
        directory: str,
        cwd_filter: str | None,
        on_progress: _Progress | None,
    ) -> tuple[SessionInfo, ...]:
        try:
            candidates = (
                tuple(Path(directory).glob("*.jsonl"))
                if os.path.isdir(directory)
                else ()
            )
        except OSError:
            return ()
        return await cls._list_paths(candidates, cwd_filter, on_progress)

    @classmethod
    async def _list_paths(
        cls,
        candidates: Sequence[Path],
        cwd_filter: str | None,
        on_progress: _Progress | None,
    ) -> tuple[SessionInfo, ...]:
        results: list[SessionInfo] = []
        total = len(candidates)
        for loaded, candidate in enumerate(candidates, 1):
            info = _session_info(os.fspath(candidate))
            if info is not None and (
                cwd_filter is None
                or (
                    bool(info.cwd)
                    and _resolve_path(info.cwd, "SessionHeader.cwd") == cwd_filter
                )
            ):
                results.append(info)
            if on_progress is not None:
                settled = on_progress(loaded, total)
                if inspect.isawaitable(settled):
                    await settled
        results.sort(key=lambda item: item.modified, reverse=True)
        return tuple(results)

    def _start_new(self, identifier: str, parent_session: str | None) -> None:
        if parent_session is not None:
            _string(parent_session, "NewSessionOptions.parentSession")
        timestamp = _timestamp()
        self._header = SessionHeader(
            id=identifier,
            timestamp=timestamp,
            cwd=self._cwd,
            parentSession=parent_session,
        )
        self._entries.clear()
        self._by_id.clear()
        self._labels.clear()
        self._label_timestamps.clear()
        self._leaf_id = None
        self._flushed = False
        if self._persist:
            filename_timestamp = timestamp.replace(":", "-").replace(".", "-")
            self._session_file = os.path.join(
                self._session_dir, f"{filename_timestamp}_{identifier}.jsonl"
            )
        else:
            self._session_file = None

    def setSessionFile(self, path: str) -> None:
        resolved_path = _resolve_path(path, "SessionManager.setSessionFile.path")
        self._session_file = resolved_path
        if not os.path.exists(resolved_path):
            self._start_new(_uuid7(), None)
            self._session_file = resolved_path
            return
        if os.path.getsize(resolved_path) == 0:
            self._start_new(_uuid7(), None)
            self._session_file = resolved_path
            if self._persist:
                _rewrite_values(resolved_path, (_header_value(self._header),))
            self._flushed = True
            return
        values = _parsed_values(resolved_path)
        header = _read_header_value(values, resolved_path)
        if _migrate_values(values) and self._persist:
            _rewrite_values(resolved_path, values)
            header = _read_header_value(values, resolved_path)
        entries: list[SessionEntry] = []
        for value in values[1:]:
            try:
                entries.append(_entry(value))
            except (KeyError, TypeError, ValueError):
                continue
        self._header = header
        self._entries = entries
        self._rebuild_indexes()
        self._flushed = True

    def newSession(self, options: NewSessionOptions | None = None) -> str | None:
        identifier = _session_id(options)
        self._start_new(identifier, None if options is None else options.parentSession)
        return self._session_file

    def isPersisted(self) -> bool:
        return self._persist

    def getCwd(self) -> str:
        return self._cwd

    def getSessionDir(self) -> str:
        return self._session_dir

    def usesDefaultSessionDir(self) -> bool:
        return self._session_dir == _default_session_dir(self._cwd)

    def getSessionId(self) -> str:
        return self._header.id

    def getSessionFile(self) -> str | None:
        return self._session_file

    def appendMessage(self, message: AgentMessage) -> str:
        if not isinstance(message, (UserMessage, AssistantMessage, ToolResultMessage)):
            raise TypeError("message must be an AgentMessage")
        entry = SessionMessageEntry(
            id=self._entry_id(),
            parentId=self._leaf_id,
            timestamp=_timestamp(),
            message=message,
        )
        self._append_entry(entry)
        return entry.id

    def appendThinkingLevelChange(self, thinkingLevel: str) -> str:
        selected = _string(thinkingLevel, "thinkingLevel")
        entry = ThinkingLevelChangeEntry(
            id=self._entry_id(),
            parentId=self._leaf_id,
            timestamp=_timestamp(),
            thinkingLevel=selected,
        )
        self._append_entry(entry)
        return entry.id

    def appendModelChange(self, provider: str, modelId: str) -> str:
        entry = ModelChangeEntry(
            id=self._entry_id(),
            parentId=self._leaf_id,
            timestamp=_timestamp(),
            provider=_string(provider, "provider"),
            modelId=_string(modelId, "modelId"),
        )
        self._append_entry(entry)
        return entry.id

    def appendCompaction(
        self,
        summary: str,
        firstKeptEntryId: str,
        tokensBefore: int,
        details: object | None = None,
        fromHook: bool = False,
    ) -> str:
        selected_summary = _string(summary, "summary")
        selected_first = _string(firstKeptEntryId, "firstKeptEntryId")
        if type(tokensBefore) is not int:
            raise TypeError("tokensBefore must be an int")
        if type(fromHook) is not bool:
            raise TypeError("fromHook must be a bool")
        entry = CompactionEntry(
            id=self._entry_id(),
            parentId=self._leaf_id,
            timestamp=_timestamp(),
            summary=selected_summary,
            firstKeptEntryId=selected_first,
            tokensBefore=tokensBefore,
            details=details,
            fromHook=fromHook,
        )
        self._append_entry(entry)
        return entry.id

    def appendCustomEntry(self, customType: str, data: object | None = None) -> str:
        entry = CustomEntry(
            id=self._entry_id(),
            parentId=self._leaf_id,
            timestamp=_timestamp(),
            customType=_string(customType, "customType"),
            data=data,
        )
        self._append_entry(entry)
        return entry.id

    def appendSessionInfo(self, name: str) -> str:
        sanitized = re.sub(r"[\r\n]+", " ", _string(name, "name")).strip()
        entry = SessionInfoEntry(
            id=self._entry_id(),
            parentId=self._leaf_id,
            timestamp=_timestamp(),
            name=sanitized,
        )
        self._append_entry(entry)
        return entry.id

    def getSessionName(self) -> str | None:
        for entry in reversed(self._entries):
            if isinstance(entry, SessionInfoEntry):
                return entry.name or None
        return None

    def appendCustomMessageEntry(
        self,
        customType: str,
        content: str | Sequence[TextContent],
        display: bool,
        details: object | None = None,
    ) -> str:
        if type(content) is str:
            selected_content: str | tuple[TextContent, ...] = _string(
                content, "content"
            )
        else:
            selected_content = tuple(cast(Sequence[TextContent], content))
            if any(type(item) is not TextContent for item in selected_content):
                raise TypeError("content must contain only TextContent values")
        if type(display) is not bool:
            raise TypeError("display must be a bool")
        entry = CustomMessageEntry(
            id=self._entry_id(),
            parentId=self._leaf_id,
            timestamp=_timestamp(),
            customType=_string(customType, "customType"),
            content=selected_content,
            display=display,
            details=details,
        )
        self._append_entry(entry)
        return entry.id

    def getLeafId(self) -> str | None:
        return self._leaf_id

    def getLeafEntry(self) -> SessionEntry | None:
        return None if self._leaf_id is None else self._by_id.get(self._leaf_id)

    def getEntry(self, id: str) -> SessionEntry | None:
        return self._by_id.get(id)

    def getChildren(self, parentId: str) -> tuple[SessionEntry, ...]:
        return tuple(entry for entry in self._entries if entry.parentId == parentId)

    def getLabel(self, id: str) -> str | None:
        return self._labels.get(id)

    def appendLabelChange(self, targetId: str, label: str | None) -> str:
        selected_target = _string(targetId, "targetId")
        if selected_target not in self._by_id:
            raise ValueError(f"Entry {selected_target} not found")
        if label is not None:
            _string(label, "label")
        entry = LabelEntry(
            id=self._entry_id(),
            parentId=self._leaf_id,
            timestamp=_timestamp(),
            targetId=selected_target,
            label=label,
        )
        self._append_entry(entry)
        return entry.id

    def getBranch(self, fromId: str | None = None) -> tuple[SessionEntry, ...]:
        current_id = self._leaf_id if fromId is None else fromId
        branch: list[SessionEntry] = []
        seen: set[str] = set()
        while current_id is not None and current_id not in seen:
            seen.add(current_id)
            current = self._by_id.get(current_id)
            if current is None:
                break
            branch.append(current)
            current_id = current.parentId
        branch.reverse()
        return tuple(branch)

    def buildContextEntries(self) -> tuple[SessionEntry, ...]:
        branch = self.getBranch()
        latest: CompactionEntry | None = None
        for entry in branch:
            if isinstance(entry, CompactionEntry):
                latest = entry
        if latest is None:
            return branch

        compaction_index = branch.index(latest)
        context_entries: list[SessionEntry] = [latest]
        found_first_kept = False
        for entry in branch[:compaction_index]:
            if entry.id == latest.firstKeptEntryId:
                found_first_kept = True
            if found_first_kept:
                context_entries.append(entry)
        context_entries.extend(branch[compaction_index + 1 :])
        return tuple(context_entries)

    def buildSessionContext(self) -> SessionContext:
        branch = self.getBranch()
        thinking_level = "off"
        model: Mapping[str, str] | None = None
        messages: list[AgentMessage] = []
        for entry in branch:
            if isinstance(entry, ThinkingLevelChangeEntry):
                thinking_level = entry.thinkingLevel
            elif isinstance(entry, ModelChangeEntry):
                model = MappingProxyType(
                    {"provider": entry.provider, "modelId": entry.modelId}
                )
            elif isinstance(entry, SessionMessageEntry) and isinstance(
                entry.message, AssistantMessage
            ):
                model = MappingProxyType(
                    {
                        "provider": entry.message.provider,
                        "modelId": entry.message.model,
                    }
                )
        for entry in self.buildContextEntries():
            if isinstance(entry, SessionMessageEntry):
                messages.append(entry.message)
            elif isinstance(entry, CompactionEntry):
                parsed = _parse_datetime(entry.timestamp)
                timestamp = (
                    0 if parsed is None else int(parsed.timestamp() * 1000)
                )
                messages.append(
                    UserMessage(
                        content=(
                            "The conversation history before this point was "
                            "compacted into the following summary:\n<summary>\n"
                            f"{entry.summary}\n</summary>"
                        ),
                        timestamp=timestamp,
                    )
                )
        return SessionContext(
            messages=tuple(messages), thinkingLevel=thinking_level, model=model
        )

    def getHeader(self) -> SessionHeader:
        return self._header

    def getEntries(self) -> tuple[SessionEntry, ...]:
        return tuple(self._entries)

    def getTree(self) -> tuple[SessionTreeNode, ...]:
        entries_by_id = {entry.id: entry for entry in self._entries}
        children: dict[str, list[str]] = {entry.id: [] for entry in self._entries}
        root_ids: list[str] = []
        for entry in self._entries:
            if entry.parentId is None or entry.parentId == entry.id:
                root_ids.append(entry.id)
            elif entry.parentId in children:
                children[entry.parentId].append(entry.id)
            else:
                root_ids.append(entry.id)

        def timestamp_key(entry_id: str) -> float:
            parsed = _parse_datetime(entries_by_id[entry_id].timestamp)
            return parsed.timestamp() if parsed is not None else 0.0

        for siblings in children.values():
            siblings.sort(key=timestamp_key)

        rebuilt: dict[str, SessionTreeNode] = {}
        for root_id in root_ids:
            stack = [(root_id, False)]
            while stack:
                entry_id, expanded = stack.pop()
                if entry_id in rebuilt:
                    continue
                if not expanded:
                    stack.append((entry_id, True))
                    stack.extend(
                        (child_id, False)
                        for child_id in reversed(children[entry_id])
                    )
                    continue
                entry = entries_by_id[entry_id]
                rebuilt[entry_id] = SessionTreeNode(
                    entry=entry,
                    children=tuple(
                        rebuilt[child_id] for child_id in children[entry_id]
                    ),
                    label=self._labels.get(entry_id),
                    labelTimestamp=self._label_timestamps.get(entry_id),
                )
        return tuple(rebuilt[root_id] for root_id in root_ids)

    def branch(self, entryId: str) -> None:
        if entryId not in self._by_id:
            raise ValueError(f"Entry {entryId} not found")
        self._leaf_id = entryId

    def resetLeaf(self) -> None:
        self._leaf_id = None

    def branchWithSummary(
        self,
        targetId: str | None,
        summary: str,
        details: object | None = None,
        fromHook: bool = False,
    ) -> str:
        if targetId is not None and targetId not in self._by_id:
            raise ValueError(f"Entry {targetId} not found")
        if type(fromHook) is not bool:
            raise TypeError("fromHook must be a bool")
        self._leaf_id = targetId
        entry = BranchSummaryEntry(
            id=self._entry_id(),
            parentId=targetId,
            timestamp=_timestamp(),
            fromId=targetId or "root",
            summary=_string(summary, "summary"),
            details=details,
            fromHook=fromHook,
        )
        self._append_entry(entry)
        return entry.id

    def createBranchedSession(self, leafId: str) -> str | None:
        path = self.getBranch(leafId)
        if not path:
            raise ValueError(f"Entry {leafId} not found")
        previous_file = self._session_file
        retained: list[SessionEntry] = []
        parent_id: str | None = None
        for entry in path:
            if isinstance(entry, LabelEntry):
                continue
            copied = replace(entry, parentId=parent_id)
            retained.append(copied)
            parent_id = copied.id
        retained_ids = {entry.id for entry in retained}
        label_entries: list[SessionEntry] = []
        used_ids = set(retained_ids)
        for target_id, label in self._labels.items():
            if target_id not in retained_ids:
                continue
            label_entry = LabelEntry(
                id=_legacy_entry_id(used_ids),
                parentId=parent_id,
                timestamp=self._label_timestamps[target_id],
                targetId=target_id,
                label=label,
            )
            label_entries.append(label_entry)
            parent_id = label_entry.id
        identifier = _uuid7()
        timestamp = _timestamp()
        self._header = SessionHeader(
            id=identifier,
            timestamp=timestamp,
            cwd=self._cwd,
            parentSession=previous_file if self._persist else None,
        )
        self._entries = retained + label_entries
        self._rebuild_indexes()
        self._flushed = False
        if not self._persist:
            self._session_file = None
            return None
        filename_timestamp = timestamp.replace(":", "-").replace(".", "-")
        self._session_file = os.path.join(
            self._session_dir, f"{filename_timestamp}_{identifier}.jsonl"
        )
        if any(
            isinstance(entry, SessionMessageEntry)
            and isinstance(entry.message, AssistantMessage)
            for entry in self._entries
        ):
            _rewrite_values(
                self._session_file,
                (_header_value(self._header), *map(_json_value, self._entries)),
            )
            self._flushed = True
        return self._session_file

    def _rebuild_indexes(self) -> None:
        self._by_id.clear()
        self._labels.clear()
        self._label_timestamps.clear()
        self._leaf_id = None
        for entry in self._entries:
            self._index_entry(entry)

    def _index_entry(self, entry: SessionEntry) -> None:
        self._by_id[entry.id] = entry
        self._leaf_id = entry.id
        if isinstance(entry, LabelEntry):
            if entry.label:
                self._labels[entry.targetId] = entry.label
                self._label_timestamps[entry.targetId] = entry.timestamp
            else:
                self._labels.pop(entry.targetId, None)
                self._label_timestamps.pop(entry.targetId, None)

    def _entry_id(self) -> str:
        for _ in range(100):
            identifier = uuid.uuid4().hex[:8]
            if identifier not in self._by_id:
                return identifier
        return str(uuid.uuid4())

    def _append_entry(self, entry: SessionEntry) -> None:
        self._entries.append(entry)
        self._index_entry(entry)
        if not self._persist or self._session_file is None:
            return
        has_assistant = any(
            isinstance(item, SessionMessageEntry)
            and isinstance(item.message, AssistantMessage)
            for item in self._entries
        )
        if not self._flushed:
            if not has_assistant:
                return
            with open(self._session_file, "xb", buffering=0) as output:
                _write_bytes(output, _header_json(self._header))
                for item in self._entries:
                    _write_bytes(output, _entry_json(item))
            self._flushed = True
            return
        with open(self._session_file, "ab", buffering=0) as output:
            _write_bytes(output, _entry_json(entry))
