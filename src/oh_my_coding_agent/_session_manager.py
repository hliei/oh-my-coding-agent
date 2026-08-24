from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import inspect
import json
import os
from pathlib import Path
import secrets
import time
from typing import Any, Literal, TypeAlias, final
from urllib.parse import unquote, urlparse
import uuid

from oh_my_core import AgentMessage
from oh_my_llm import TextContent


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


def _header_json(header: SessionHeader) -> bytes:
    value: dict[str, object] = {
        "type": header.type,
        "version": header.version,
        "id": header.id,
        "timestamp": header.timestamp,
        "cwd": header.cwd,
    }
    if header.parentSession is not None:
        value["parentSession"] = header.parentSession
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def _read_header(path: str) -> SessionHeader:
    data = Path(path).read_bytes()
    if not data:
        raise ValueError(f"Session file is empty: {path}")
    parsed: object | None = None
    for physical_line in data.splitlines():
        if not physical_line.strip():
            continue
        try:
            parsed = json.loads(physical_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        break
    if not isinstance(parsed, dict) or parsed.get("type") != "session":
        raise ValueError(f"Session file is not a valid omh session: {path}")
    identifier = parsed.get("id")
    timestamp = parsed.get("timestamp")
    cwd = parsed.get("cwd")
    parent = parsed.get("parentSession")
    if type(identifier) is not str or type(timestamp) is not str or type(cwd) is not str:
        raise ValueError(f"Session file is not a valid omh session: {path}")
    if parent is not None and type(parent) is not str:
        raise ValueError(f"Session file is not a valid omh session: {path}")
    return SessionHeader(
        id=identifier,
        timestamp=timestamp,
        cwd=cwd,
        parentSession=parent,
    )


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
    _uses_default_dir: bool

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
        "_uses_default_dir",
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
        uses_default_dir: bool,
    ) -> SessionManager:
        manager = cls(_token=_MANAGER_TOKEN)
        manager._cwd = cwd
        manager._session_dir = session_dir
        manager._persist = persist
        manager._uses_default_dir = uses_default_dir
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
        uses_default = sessionDir is None
        directory = (
            _default_session_dir(resolved_cwd)
            if sessionDir is None
            else _normalize_path(sessionDir, "SessionManager.create.sessionDir")
        )
        os.makedirs(directory, exist_ok=True)
        manager = cls._blank(resolved_cwd, directory, True, uses_default)
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
        os.makedirs(directory, exist_ok=True)
        if not os.path.exists(resolved_path):
            manager = cls._blank(override or os.getcwd(), directory, True, False)
            manager._start_new(_uuid7(), None)
            manager._session_file = resolved_path
            return manager
        if os.path.getsize(resolved_path) == 0:
            manager = cls._blank(override or os.getcwd(), directory, True, False)
            manager._start_new(_uuid7(), None)
            manager._session_file = resolved_path
            Path(resolved_path).write_bytes(_header_json(manager._header))
            manager._flushed = True
            return manager
        header = _read_header(resolved_path)
        manager = cls._blank(override or _resolve_path(header.cwd, "SessionHeader.cwd"), directory, True, False)
        manager._header = header
        manager._session_file = resolved_path
        manager._flushed = True
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
        candidates = sorted(
            Path(directory).glob("*.jsonl") if os.path.isdir(directory) else (),
            key=lambda item: item.stat().st_mtime_ns,
            reverse=True,
        )
        for candidate in candidates:
            try:
                header = _read_header(os.fspath(candidate))
            except (OSError, ValueError):
                continue
            if sessionDir is not None and _resolve_path(header.cwd, "SessionHeader.cwd") != resolved_cwd:
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
        manager = cls._blank(resolved_cwd, "", False, False)
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
        _read_header(source)
        identifier = _session_id(options)
        directory = (
            _default_session_dir(target)
            if sessionDir is None
            else _normalize_path(sessionDir, "SessionManager.forkFrom.sessionDir")
        )
        os.makedirs(directory, exist_ok=True)
        manager = cls._blank(target, directory, True, sessionDir is None)
        manager._start_new(identifier, source)
        assert manager._session_file is not None
        with open(manager._session_file, "xb") as output:
            output.write(_header_json(manager._header))
        manager._flushed = True
        return manager

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
        return await cls._list_dir(
            directory,
            resolved_cwd if sessionDir is not None else None,
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
        candidates = tuple(
            item
            for directory in Path(root).iterdir()
            if directory.is_dir()
            for item in directory.glob("*.jsonl")
        )
        return await cls._list_paths(candidates, None, onProgress)

    @classmethod
    async def _list_dir(
        cls,
        directory: str,
        cwd_filter: str | None,
        on_progress: _Progress | None,
    ) -> tuple[SessionInfo, ...]:
        candidates = tuple(Path(directory).glob("*.jsonl")) if os.path.isdir(directory) else ()
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
            try:
                header = _read_header(os.fspath(candidate))
                header_cwd = _resolve_path(header.cwd, "SessionHeader.cwd")
                if cwd_filter is None or header_cwd == cwd_filter:
                    stat = candidate.stat()
                    created = datetime.fromisoformat(header.timestamp.replace("Z", "+00:00"))
                    results.append(
                        SessionInfo(
                            path=os.fspath(candidate),
                            id=header.id,
                            cwd=header.cwd,
                            name=None,
                            parentSessionPath=header.parentSession,
                            created=created,
                            modified=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
                            messageCount=0,
                            firstMessage="",
                            allMessagesText="",
                        )
                    )
            except (OSError, ValueError):
                pass
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
        replacement = SessionManager.open(path, self._session_dir, self._cwd)
        self._header = replacement._header
        self._session_file = replacement._session_file
        self._entries = list(replacement._entries)
        self._by_id = dict(replacement._by_id)
        self._labels = dict(replacement._labels)
        self._label_timestamps = dict(replacement._label_timestamps)
        self._leaf_id = replacement._leaf_id
        self._flushed = replacement._flushed

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
        return self._uses_default_dir

    def getSessionId(self) -> str:
        return self._header.id

    def getSessionFile(self) -> str | None:
        return self._session_file

    def appendMessage(self, message: AgentMessage) -> str:
        del message
        raise NotImplementedError("Session entry append is not implemented")

    def appendThinkingLevelChange(self, thinkingLevel: str) -> str:
        del thinkingLevel
        raise NotImplementedError("Session entry append is not implemented")

    def appendModelChange(self, provider: str, modelId: str) -> str:
        del provider, modelId
        raise NotImplementedError("Session entry append is not implemented")

    def appendCompaction(
        self,
        summary: str,
        firstKeptEntryId: str,
        tokensBefore: int,
        details: object | None = None,
        fromHook: bool = False,
    ) -> str:
        del summary, firstKeptEntryId, tokensBefore, details, fromHook
        raise NotImplementedError("Session entry append is not implemented")

    def appendCustomEntry(self, customType: str, data: object | None = None) -> str:
        del customType, data
        raise NotImplementedError("Session entry append is not implemented")

    def appendSessionInfo(self, name: str) -> str:
        del name
        raise NotImplementedError("Session entry append is not implemented")

    def getSessionName(self) -> str | None:
        return None

    def appendCustomMessageEntry(
        self,
        customType: str,
        content: str | Sequence[TextContent],
        display: bool,
        details: object | None = None,
    ) -> str:
        del customType, content, display, details
        raise NotImplementedError("Session entry append is not implemented")

    def getLeafId(self) -> str | None:
        return self._leaf_id

    def getLeafEntry(self) -> SessionEntry | None:
        return None if self._leaf_id is None else self._by_id.get(self._leaf_id)

    def getEntry(self, id: str) -> SessionEntry | None:
        return self._by_id.get(id)

    def getChildren(self, parentId: str) -> tuple[SessionEntry, ...]:
        del parentId
        return ()

    def getLabel(self, id: str) -> str | None:
        del id
        return None

    def appendLabelChange(self, targetId: str, label: str | None) -> str:
        del targetId, label
        raise NotImplementedError("Session entry append is not implemented")

    def getBranch(self, fromId: str | None = None) -> tuple[SessionEntry, ...]:
        del fromId
        return ()

    def buildContextEntries(self) -> tuple[SessionEntry, ...]:
        return self.getBranch()

    def buildSessionContext(self) -> SessionContext:
        return SessionContext(messages=(), thinkingLevel="off", model=None)

    def getHeader(self) -> SessionHeader:
        return self._header

    def getEntries(self) -> tuple[SessionEntry, ...]:
        return tuple(self._entries)

    def getTree(self) -> tuple[SessionTreeNode, ...]:
        return ()

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
        del targetId, summary, details, fromHook
        raise NotImplementedError("Session entry append is not implemented")

    def createBranchedSession(self, leafId: str) -> str | None:
        raise ValueError(f"Entry {leafId} not found")
