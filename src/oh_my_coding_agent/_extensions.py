from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import inspect
import os
import re
import stat
import traceback
import types
from typing import Any, Final, Literal, Protocol, cast, final

from oh_my_core import AgentMessage, AgentTool
from oh_my_llm import AbortSignal, LifecycleError, Model

from ._resource_state import ExtensionDiagnostic


_TOKEN = object()
_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_BUILTIN_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {"read", "bash", "edit", "write"}
)
_SESSION_EVENTS: Final[frozenset[str]] = frozenset(
    {"session_start", "session_shutdown"}
)
_AGENT_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "agent_start",
        "agent_end",
        "turn_start",
        "turn_end",
        "message_start",
        "message_update",
        "message_end",
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_end",
        "agent_settled",
    }
)
_EVENT_TYPES: Final[frozenset[str]] = _SESSION_EVENTS | _AGENT_EVENTS
_Handler = Callable[..., Any]


class _NamedEvent(Protocol):
    type: str


@final
class ExtensionAPI:
    __slots__ = ("_closed", "_extension", "_runtime")

    def __init__(self, *args: object, _token: object = None, **kwargs: object) -> None:
        del args, kwargs
        if _token is not _TOKEN:
            raise TypeError("ExtensionAPI values are factory-produced")
        self._runtime: ExtensionRuntime | None = None
        self._extension: _LoadedExtension | None = None
        self._closed = True

    def __init_subclass__(cls) -> None:
        raise TypeError("ExtensionAPI is sealed")

    def on(self, eventType: str, handler: object) -> None:
        self._ensure_open()
        if type(eventType) is not str or eventType not in _EVENT_TYPES:
            raise ValueError("ExtensionAPI.on: unknown event type")
        if not callable(handler):
            raise TypeError("ExtensionAPI.on: handler must be callable")
        assert self._extension is not None
        self._extension.handlers.append(
            _RegisteredHandler(event_type=eventType, callback=cast(_Handler, handler))
        )

    def registerTool(self, tool: AgentTool) -> None:
        self._ensure_open()
        if type(tool) is not AgentTool:
            raise TypeError("ExtensionAPI.registerTool: must be an AgentTool")
        assert self._runtime is not None
        assert self._extension is not None
        if tool.name in _BUILTIN_TOOL_NAMES or tool.name in self._runtime.tool_names:
            raise ValueError(f'Extension Tool "{tool.name}" is already registered')
        self._runtime.tools.append(tool)
        self._extension.tools.append(tool)

    def _bind(self, runtime: ExtensionRuntime, extension: _LoadedExtension) -> None:
        self._runtime = runtime
        self._extension = extension
        self._closed = False

    def _close(self) -> None:
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("ExtensionAPI is closed")


@final
class ExtensionContext:
    __slots__ = ("_cwd", "_messages", "_model", "_session_id", "_signal")

    def __init__(self, *args: object, _token: object = None, **kwargs: object) -> None:
        del args, kwargs
        if _token is not _TOKEN:
            raise TypeError("ExtensionContext values are factory-produced")
        self._cwd = ""
        self._session_id = ""
        self._model: Model | None = None
        self._messages: tuple[AgentMessage, ...] = ()
        self._signal: AbortSignal | None = None

    def __init_subclass__(cls) -> None:
        raise TypeError("ExtensionContext is sealed")

    @property
    def cwd(self) -> str:
        return self._cwd

    @property
    def sessionId(self) -> str:
        return self._session_id

    @property
    def model(self) -> Model:
        assert self._model is not None
        return self._model

    @property
    def messages(self) -> tuple[AgentMessage, ...]:
        return self._messages

    @property
    def signal(self) -> AbortSignal | None:
        return self._signal


def snapshot_extension_context(
    *,
    cwd: str,
    session_id: str,
    model: Model,
    messages: Sequence[AgentMessage],
    signal: AbortSignal | None,
) -> ExtensionContext:
    context = ExtensionContext(_token=_TOKEN)
    context._cwd = cwd
    context._session_id = session_id
    context._model = model
    context._messages = tuple(messages)
    context._signal = signal
    return context


def _fresh_context(context: ExtensionContext) -> ExtensionContext:
    return snapshot_extension_context(
        cwd=context.cwd,
        session_id=context.sessionId,
        model=context.model,
        messages=context.messages,
        signal=context.signal,
    )


@dataclass
class _RegisteredHandler:
    event_type: str
    callback: _Handler
    resolved: bool = False


@dataclass
class _LoadedExtension:
    name: str
    relative: str
    path: str
    module: types.ModuleType
    handlers: list[_RegisteredHandler] = field(default_factory=list)
    tools: list[AgentTool] = field(default_factory=list)
    started: bool = False
    start_attempted: bool = False


class ExtensionRuntime:
    __slots__ = ("_loaded", "diagnostics", "tools")

    def __init__(self) -> None:
        self._loaded: list[_LoadedExtension] = []
        self.tools: list[AgentTool] = []
        self.diagnostics: list[ExtensionDiagnostic] = []

    @property
    def tool_names(self) -> set[str]:
        return {tool.name for tool in self.tools}

    def tool_summaries(self) -> tuple[tuple[str, str], ...]:
        return tuple((tool.name, tool.description) for tool in self.tools)

    def release(self) -> None:
        self._loaded.clear()
        self.tools.clear()
        self.diagnostics.clear()

    async def start(self, context: ExtensionContext) -> None:
        for extension in list(self._loaded):
            if not await self._start_one(extension, context):
                continue
            if not await self._discover_one(extension, context):
                continue
            extension.started = True

    async def _start_one(
        self, extension: _LoadedExtension, context: ExtensionContext
    ) -> bool:
        extension.start_attempted = True
        try:
            for handler in extension.handlers:
                if handler.event_type != "session_start":
                    continue
                await _invoke_session_handler(
                    handler.callback, _fresh_context(context)
                )
        except asyncio.CancelledError as error:
            if _owner_cancelled():
                await self._raise_after_shutdown(context, error, re_raise=True)
            else:
                await self._omit(extension, context, error, "session_start")
            return False
        except Exception as error:
            await self._omit(extension, context, error, "session_start")
            return False
        return True

    async def _discover_one(
        self, extension: _LoadedExtension, context: ExtensionContext
    ) -> bool:
        try:
            await _collect_extension_resources(extension)
        except asyncio.CancelledError as error:
            if _owner_cancelled():
                await self._raise_after_shutdown(context, error, re_raise=True)
            else:
                await self._omit(extension, context, error, "resources_discover")
            return False
        except Exception as error:
            await self._omit(extension, context, error, "resources_discover")
            return False
        return True

    async def _omit(
        self,
        extension: _LoadedExtension,
        context: ExtensionContext,
        error: BaseException,
        event_type: Literal[
            "session_start", "resources_discover", "session_shutdown"
        ],
    ) -> None:
        self.diagnostics.append(
            _lifecycle_diagnostic(extension.path, event_type, error)
        )
        _drop_tools(self, extension)
        if extension.start_attempted:
            shutdown_error = await self._shutdown_one(extension, context)
            if shutdown_error is not None:
                self.diagnostics.append(
                    _lifecycle_diagnostic(
                        extension.path, "session_shutdown", shutdown_error
                    )
                )
        if extension in self._loaded:
            self._loaded.remove(extension)

    async def shutdown(
        self,
        context: ExtensionContext,
        *,
        diagnostics: list[ExtensionDiagnostic] | None = None,
    ) -> list[BaseException]:
        failures: list[BaseException] = []
        owner_cancellation: asyncio.CancelledError | None = None
        for extension in reversed(self._loaded):
            if not extension.start_attempted:
                continue
            for handler in reversed(extension.handlers):
                if handler.event_type != "session_shutdown" or handler.resolved:
                    continue
                try:
                    await _invoke_session_handler(
                        handler.callback, _fresh_context(context)
                    )
                except asyncio.CancelledError as error:
                    if _owner_cancelled():
                        _uncancel_owner()
                        owner_cancellation = error
                    else:
                        _record_shutdown_failure(
                            failures, diagnostics, extension.path, error
                        )
                except BaseException as error:
                    _record_shutdown_failure(
                        failures, diagnostics, extension.path, error
                    )
                else:
                    handler.resolved = True
        if owner_cancellation is not None:
            raise owner_cancellation
        return failures

    async def _shutdown_one(
        self, extension: _LoadedExtension, context: ExtensionContext
    ) -> BaseException | None:
        first: BaseException | None = None
        owner_cancellation: asyncio.CancelledError | None = None
        for handler in reversed(extension.handlers):
            if handler.event_type != "session_shutdown" or handler.resolved:
                continue
            try:
                await _invoke_session_handler(
                    handler.callback, _fresh_context(context)
                )
            except asyncio.CancelledError as error:
                if _owner_cancelled():
                    _uncancel_owner()
                    owner_cancellation = error
                if first is None:
                    first = error
            except BaseException as error:
                if first is None:
                    first = error
            else:
                handler.resolved = True
        if owner_cancellation is not None:
            raise owner_cancellation
        return first

    async def dispatch(
        self, event: _NamedEvent, context: ExtensionContext
    ) -> None:
        event_type = event.type
        if event_type not in _AGENT_EVENTS:
            return
        for extension in self._loaded:
            for handler in extension.handlers:
                if handler.event_type != event_type:
                    continue
                try:
                    await _invoke_agent_handler(
                        handler.callback, event, _fresh_context(context)
                    )
                except asyncio.CancelledError as error:
                    if _owner_cancelled():
                        raise
                    raise LifecycleError(
                        "hook",
                        "Python Extension handler failed",
                        causes=(error,),
                    ) from error
                except BaseException as error:
                    if isinstance(error, LifecycleError) and error.code == "hook":
                        raise
                    raise LifecycleError(
                        "hook",
                        "Python Extension handler failed",
                        causes=(error,),
                    ) from error

    async def _raise_after_shutdown(
        self,
        context: ExtensionContext,
        error: BaseException,
        *,
        re_raise: bool,
    ) -> None:
        shutdown_errors = await self.shutdown(context)
        if re_raise:
            if shutdown_errors:
                error.__cause__ = shutdown_errors[0]
            raise error
        raise LifecycleError(
            "hook",
            "Python Extension handler failed",
            causes=(error, *shutdown_errors),
        ) from error


def snapshot_extension_sources(
    cwd: str, project_trusted: bool
) -> tuple[tuple[str, str, str], ...]:
    if not project_trusted:
        return ()
    return _snapshot_sources(cwd)


async def load_extensions(
    cwd: str,
    project_trusted: bool,
    *,
    snapshots: tuple[tuple[str, str, str], ...] | None = None,
) -> ExtensionRuntime:
    selected = (
        snapshot_extension_sources(cwd, project_trusted)
        if snapshots is None
        else snapshots
    )
    runtime = ExtensionRuntime()
    for index, snapshot in enumerate(selected):
        await _admit_extension(runtime, cwd, index, snapshot)
    return runtime


def _snapshot_sources(cwd: str) -> tuple[tuple[str, str, str], ...]:
    omh = os.path.join(cwd, ".omh")
    omh_kind = _lstat_kind(omh, label='Project configuration ".omh"')
    if omh_kind is None:
        return ()
    if omh_kind != "dir":
        raise ValueError('Project configuration ".omh" is invalid')
    extensions_dir = os.path.join(omh, "extensions")
    kind = _lstat_kind(
        extensions_dir, label='Python Extension directory ".omh/extensions"'
    )
    if kind is None:
        return ()
    if kind != "dir":
        raise ValueError('Python Extension directory ".omh/extensions" is invalid')
    names = _direct_names(
        extensions_dir, 'Python Extension directory ".omh/extensions"'
    )
    selected: list[tuple[str, str, str]] = []
    for filename in names:
        if not filename.endswith(".py"):
            continue
        stem = filename[: -len(".py")]
        relative = f".omh/extensions/{filename}"
        path = os.path.join(extensions_dir, filename)
        file_kind = _lstat_kind(path, label=f'Python Extension "{relative}"')
        if file_kind != "file" or not _NAME_PATTERN.fullmatch(stem):
            raise ValueError(f'Python Extension "{relative}" is invalid')
        selected.append((stem, relative, path))
    selected.sort(key=lambda item: item[1])
    snapshots: list[tuple[str, str, str]] = []
    for stem, relative, path in selected:
        snapshots.append((stem, relative, _read_utf8(path, relative)))
        confirm = _lstat_kind(path, label=f'Python Extension "{relative}"')
        if confirm != "file":
            raise ValueError(f'Python Extension "{relative}" is invalid')
    return tuple(snapshots)


async def _admit_extension(
    runtime: ExtensionRuntime,
    cwd: str,
    index: int,
    snapshot: tuple[str, str, str],
) -> None:
    name, relative, source = snapshot
    path = os.path.abspath(os.path.join(cwd, relative))
    module = types.ModuleType(f"_omh_ext_{id(runtime)}_{index}_{name}")
    module.__file__ = path
    module.__package__ = None
    module.__loader__ = None
    try:
        compiled = compile(source, relative, "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as error:
        runtime.diagnostics.append(_load_diagnostic(path, "load", error))
        return
    try:
        exec(compiled, module.__dict__)
    except Exception as error:
        runtime.diagnostics.append(_load_diagnostic(path, "load", error))
        return
    entrypoint = module.__dict__.get("extension")
    if not callable(entrypoint):
        runtime.diagnostics.append(
            ExtensionDiagnostic.Load(
                path=path,
                phase="load",
                message="'extension' is not a callable factory",
            )
        )
        return
    loaded = _LoadedExtension(
        name=name, relative=relative, path=path, module=module
    )
    api = ExtensionAPI(_token=_TOKEN)
    api._bind(runtime, loaded)
    try:
        result = entrypoint(api)
        if inspect.isawaitable(result):
            await result
        elif result is not None:
            raise TypeError("extension() must return None or an awaitable")
    except asyncio.CancelledError as error:
        api._close()
        _drop_tools(runtime, loaded)
        if _owner_cancelled():
            raise
        runtime.diagnostics.append(_load_diagnostic(path, "factory", error))
    except Exception as error:
        api._close()
        _drop_tools(runtime, loaded)
        runtime.diagnostics.append(_load_diagnostic(path, "factory", error))
    else:
        api._close()
        runtime._loaded.append(loaded)


async def _collect_extension_resources(extension: _LoadedExtension) -> None:
    del extension


def _drop_tools(runtime: ExtensionRuntime, extension: _LoadedExtension) -> None:
    owned = {id(tool) for tool in extension.tools}
    runtime.tools[:] = [tool for tool in runtime.tools if id(tool) not in owned]
    extension.tools.clear()


def _raw_message(error: BaseException) -> str:
    text = str(error)
    if text:
        return text
    return type(error).__name__


def _raw_stack(error: BaseException) -> str | None:
    if error.__traceback__ is None:
        return None
    return "".join(
        traceback.format_exception(
            type(error), error, error.__traceback__, chain=False
        )
    )


def _load_diagnostic(
    path: str, phase: Literal["load", "factory"], error: BaseException
) -> ExtensionDiagnostic:
    return ExtensionDiagnostic.Load(
        path=path, phase=phase, message=_raw_message(error)
    )


def _lifecycle_diagnostic(
    path: str,
    event_type: Literal["session_start", "resources_discover", "session_shutdown"],
    error: BaseException,
) -> ExtensionDiagnostic:
    return ExtensionDiagnostic.Lifecycle(
        path=path,
        eventType=event_type,
        message=_raw_message(error),
        stack=_raw_stack(error),
    )


def _record_shutdown_failure(
    failures: list[BaseException],
    diagnostics: list[ExtensionDiagnostic] | None,
    path: str,
    error: BaseException,
) -> None:
    failures.append(error)
    if diagnostics is not None:
        diagnostics.append(
            _lifecycle_diagnostic(path, "session_shutdown", error)
        )


async def _invoke_session_handler(
    handler: _Handler, context: ExtensionContext
) -> None:
    await _settle_handler(handler(context))


async def _invoke_agent_handler(
    handler: _Handler, event: _NamedEvent, context: ExtensionContext
) -> None:
    await _settle_handler(handler(event, context))


async def _settle_handler(result: object) -> None:
    if inspect.isawaitable(result):
        settled = await result
        if settled is not None:
            raise TypeError(
                "Python Extension handler must return None or an awaitable"
            )
        return
    if result is not None:
        raise TypeError("Python Extension handler must return None or an awaitable")


def _owner_cancelled() -> bool:
    owner = asyncio.current_task()
    return owner is not None and owner.cancelling() > 0


def _uncancel_owner() -> None:
    owner = asyncio.current_task()
    if owner is not None and owner.cancelling():
        owner.uncancel()


def _read_utf8(path: str, relative: str) -> str:
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError as error:
        raise ValueError(f'Python Extension "{relative}" is invalid') from error
    except OSError as error:
        raise LifecycleError(
            "cleanup",
            f'Python Extension "{relative}" could not be read',
            causes=(error,),
        ) from error
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f'Python Extension "{relative}" is invalid')
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f'Python Extension "{relative}" is invalid') from error


def _direct_names(path: str, label: str) -> list[str]:
    try:
        return [
            entry.name
            for entry in os.scandir(path)
            if not entry.name.startswith(".")
        ]
    except OSError as error:
        raise LifecycleError(
            "cleanup",
            f"{label} could not be read",
            causes=(error,),
        ) from error


def _lstat_kind(path: str, *, label: str) -> str | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise LifecycleError(
            "cleanup",
            f"{label} could not be read",
            causes=(error,),
        ) from error
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if stat.S_ISDIR(info.st_mode):
        return "dir"
    if stat.S_ISREG(info.st_mode):
        return "file"
    return "other"
