from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

import httpx

from oh_my_coding_agent import (
    CreateAgentSessionOptions,
    NewSessionOptions,
    SessionManager,
    createAgentSession,
)
from oh_my_llm import LifecycleError, ToolResultMessage


_STOP_SSE = (
    b'data: {"id":"chatcmpl-stop","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"Done"},"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-stop","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4,"prompt_cache_hit_tokens":0}}\n\n'
    b"data: [DONE]\n\n"
)
_ECHO_SOURCE = """\
from oh_my_core import AgentTool, AgentToolResult
from oh_my_llm import TextContent

async def _echo(tool_call_id, params, signal, update):
    return AgentToolResult(
        content=(TextContent(text=params["text"]),),
        details={"echoed": True},
    )

def extension(api):
    api.registerTool(
        AgentTool(
            name="echo",
            label="Echo",
            description="Echo text back",
            parameters={
                "type": "object",
                "additionalProperties": False,
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            },
            execute=_echo,
        )
    )
"""


class _HttpSpy:
    def __init__(self, *bodies: bytes) -> None:
        self.bodies = list(bodies) or [_STOP_SSE]
        self.requests: list[httpx.Request] = []

    def install(self) -> Any:
        spy = self
        original = httpx.AsyncClient

        class FakeClient(httpx.AsyncClient):
            def __init__(self, **kwargs: Any) -> None:
                kwargs = dict(kwargs)
                kwargs["transport"] = httpx.MockTransport(spy._handle)
                super().__init__(**kwargs)

        httpx.AsyncClient = FakeClient  # type: ignore[misc]
        return original

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        return httpx.Response(
            200,
            content=body,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )


def _sse(*payloads: dict[str, Any] | str) -> bytes:
    chunks: list[bytes] = []
    for payload in payloads:
        if payload == "[DONE]":
            chunks.append(b"data: [DONE]\n\n")
        else:
            chunks.append(b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n")
    return b"".join(chunks)


def _tool_call_sse(call_id: str, name: str, arguments: dict[str, Any]) -> bytes:
    return _sse(
        {
            "id": "chatcmpl-tool",
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(
                                        arguments, separators=(",", ":")
                                    ),
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-tool",
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 4,
                "total_tokens": 12,
                "prompt_cache_hit_tokens": 0,
            },
        },
        "[DONE]",
    )


def _write_extension(workspace: Path, name: str, source: str) -> None:
    directory = workspace / ".omh" / "extensions"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.py").write_text(source, encoding="utf-8")


async def _session(workspace: Path, *, trusted: bool, session_id: str) -> Any:
    manager = SessionManager.inMemory(
        os.fspath(workspace), NewSessionOptions(id=session_id)
    )
    return (
        await createAgentSession(
            CreateAgentSessionOptions(
                cwd=os.fspath(workspace),
                sessionManager=manager,
                projectTrusted=trusted,
            )
        )
    ).session


async def _discovery(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True)
    _write_extension(workspace, "echo", "not python")
    untrusted = await _session(workspace, trusted=False, session_id="untrusted")
    untrusted_prompt = untrusted.systemPrompt
    await untrusted.dispose()
    _write_extension(workspace, "echo", _ECHO_SOURCE)
    _write_extension(
        workspace,
        "zeta",
        _ECHO_SOURCE.replace('name="echo"', 'name="zeta_echo"').replace(
            "Echo text back", "Zeta echo"
        ),
    )
    _write_extension(workspace, ".hidden", "def extension(api):\n    return None\n")
    nested = workspace / ".omh" / "extensions" / "nested"
    nested.mkdir()
    (nested / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workspace / ".omh" / "extensions" / "notes.txt").write_text("x", encoding="utf-8")
    trusted = await _session(workspace, trusted=True, session_id="trusted")
    prompt = trusted.systemPrompt
    await trusted.dispose()
    target = root / "outside.py"
    target.write_text(_ECHO_SOURCE, encoding="utf-8")
    os.symlink(
        os.fspath(target),
        os.fspath(workspace / ".omh" / "extensions" / "linked.py"),
    )
    symlink_rejected = False
    try:
        await _session(workspace, trusted=True, session_id="symlink")
    except ValueError as error:
        symlink_rejected = ".omh/extensions/linked.py" in str(error)
    return {
        "A": "valid_names_admitted_invalid_selected_rejected",
        "L": [],
        "T": {
            "builtinThenAlphaThenZeta": (
                prompt.find("- write:")
                < prompt.find("- echo: Echo text back")
                < prompt.find("- zeta_echo: Zeta echo")
            ),
            "symlinkRejected": symlink_rejected,
        },
        "E": {"hiddenNestedNonPyIgnored": "hidden" not in prompt},
        "C": (
            "untrusted_zero_extension_probes"
            if "- echo:" not in untrusted_prompt
            else "untrusted_loaded"
        ),
    }


async def _entrypoint(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True)
    _write_extension(workspace, "alpha", "VALUE = 1\n")
    manager = SessionManager.inMemory(
        os.fspath(workspace), NewSessionOptions(id="missing")
    )
    carrier = None
    try:
        await createAgentSession(
            CreateAgentSessionOptions(
                cwd=os.fspath(workspace),
                projectTrusted=True,
                sessionManager=manager,
            )
        )
    except ValueError as error:
        carrier = type(error).__name__
    return {
        "A": "exact_extension_name_required",
        "L": [],
        "T": {"invalidCarrier": carrier},
        "E": {"laterModuleStopped": True},
        "C": "manager_unchanged" if manager.getEntries() == () else "manager_changed",
    }


async def _registration(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True)
    _write_extension(
        workspace,
        "alpha",
        """
from pathlib import Path
from oh_my_core import AgentTool, AgentToolResult
from oh_my_llm import TextContent

async def _noop(tool_call_id, params, signal, update):
    return AgentToolResult(content=(TextContent(text="ok"),), details=None)

def extension(api):
    api.registerTool(AgentTool(
        name="alpha_tool",
        label="Alpha",
        description="alpha tool",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        execute=_noop,
    ))
    def on_start(context):
        try:
            api.registerTool(AgentTool(
                name="late",
                label="Late",
                description="late",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                execute=_noop,
            ))
            Path(context.cwd, "late.txt").write_text("registered", encoding="utf-8")
        except Exception as error:
            Path(context.cwd, "late.txt").write_text(type(error).__name__, encoding="utf-8")
    api.on("session_start", on_start)
""",
    )
    session = await _session(workspace, trusted=True, session_id="reg")
    late_absent = "- late:" not in session.systemPrompt
    await session.dispose()
    _write_extension(workspace, "zeta", _ECHO_SOURCE)
    _write_extension(workspace, "alpha", _ECHO_SOURCE)
    duplicate = False
    try:
        await _session(workspace, trusted=True, session_id="dup")
    except LifecycleError as error:
        duplicate = error.code == "hook" and "zeta.py" in str(error)
    return {
        "A": "initialization_only_registration",
        "L": [],
        "T": {"lateToolAbsent": late_absent, "duplicateRejected": duplicate},
        "E": {"refresh": False},
        "C": "retained_api_cannot_mutate_published_session",
    }


async def _context(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True)
    _write_extension(
        workspace,
        "alpha",
        """
from pathlib import Path
import json

def extension(api):
    def on_start(context):
        try:
            context.cwd = "/tmp"
            mutated = True
        except Exception:
            mutated = False
        Path(context.cwd, "ctx.json").write_text(
            json.dumps({
                "mutated": mutated,
                "startMessages": len(context.messages),
                "startSignal": context.signal is not None,
                "id": id(context),
            }),
            encoding="utf-8",
        )
        globals()["retained"] = context
    def on_agent_start(event, context):
        retained = globals()["retained"]
        Path(context.cwd, "agent.json").write_text(
            json.dumps({
                "sameObject": retained is context,
                "nowMessages": len(context.messages),
                "nowSignal": context.signal is not None,
                "startMessages": len(retained.messages),
            }),
            encoding="utf-8",
        )
    api.on("session_start", on_start)
    api.on("agent_start", on_agent_start)
""",
    )
    spy = _HttpSpy()
    original = spy.install()
    try:
        session = await _session(workspace, trusted=True, session_id="ctx")
        await session.prompt("hello")
        start = json.loads((workspace / "ctx.json").read_text(encoding="utf-8"))
        agent = json.loads((workspace / "agent.json").read_text(encoding="utf-8"))
        await session.dispose()
        return {
            "A": "factory_produced_readonly_snapshot",
            "L": {
                "sessionStartSignal": start["startSignal"],
                "agentStartSignal": agent["nowSignal"],
            },
            "T": {
                "retainedStartMessagesUnchanged": (
                    not start["mutated"]
                    and start["startMessages"] == 0
                    and agent["startMessages"] == 0
                    and agent["nowMessages"] == 1
                )
            },
            "E": {"liveSessionActions": False},
            "C": "distinct_immutable_objects"
            if not agent["sameObject"]
            else "shared_object",
        }
    finally:
        setattr(httpx, "AsyncClient", original)


async def _handlers(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True)
    _write_extension(
        workspace,
        "alpha",
        """
from pathlib import Path

def extension(api):
    def on_start(context):
        Path(context.cwd, "started.txt").write_text("yes", encoding="utf-8")
    def on_agent_start(event, context):
        raise RuntimeError("handler boom")
    def on_stop(context):
        path = Path(context.cwd, "order.log")
        prior = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(prior + "stop\\n", encoding="utf-8")
        if not Path(context.cwd, "retried.txt").exists():
            Path(context.cwd, "retried.txt").write_text("1", encoding="utf-8")
            raise RuntimeError("shutdown boom")
    api.on("session_start", on_start)
    api.on("agent_start", on_agent_start)
    api.on("session_shutdown", on_stop)
""",
    )
    spy = _HttpSpy()
    original = spy.install()
    try:
        session = await _session(workspace, trusted=True, session_id="hook")
        started = (workspace / "started.txt").read_text(encoding="utf-8") == "yes"
        public: list[str] = []
        session.subscribe(lambda event: public.append(event.type))
        hook = None
        try:
            await session.prompt("hello")
        except LifecycleError as error:
            hook = str(error)
        history = session.sessionManager.getEntries() != ()
        try:
            await session.dispose()
        except LifecycleError:
            pass
        await session.dispose()
        return {
            "A": "session_start_before_publication" if started else "start_missing",
            "L": {"publicListenersAfterFailure": bool(public)},
            "T": {"hook": hook},
            "E": {"laterModelEffects": bool(spy.requests)},
            "C": "history_preserved_shutdown_retryable"
            if history and (workspace / "order.log").read_text(encoding="utf-8").count("stop") == 2
            else "not_retryable",
        }
    finally:
        setattr(httpx, "AsyncClient", original)


async def _modules(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True)
    _write_extension(
        workspace,
        "counter",
        """
from pathlib import Path
from oh_my_core import AgentTool, AgentToolResult
from oh_my_llm import TextContent

COUNT = 0
_ROOT = Path(__file__).resolve().parents[2]

async def _ping(tool_call_id, params, signal, update):
    return AgentToolResult(content=(TextContent(text=str(COUNT)),), details=None)

def extension(api):
    global COUNT
    COUNT += 1
    try:
        import support.probe  # type: ignore[import-not-found]
        leaked = True
    except ImportError:
        leaked = False
    (_ROOT / "probe.log").write_text("leaked" if leaked else "isolated", encoding="utf-8")
    api.registerTool(AgentTool(
        name="ping",
        label="Ping",
        description="Return module counter",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        execute=_ping,
    ))
""",
    )
    support = workspace / ".omh" / "extensions" / "support"
    support.mkdir()
    (support / "__init__.py").write_text("", encoding="utf-8")
    (support / "probe.py").write_text("VALUE = 1\n", encoding="utf-8")
    first = await _session(workspace, trusted=True, session_id="one")
    second = await _session(workspace, trusted=True, session_id="two")
    spy = _HttpSpy(
        _tool_call_sse("call-1", "ping", {}),
        _STOP_SSE,
        _tool_call_sse("call-2", "ping", {}),
        _STOP_SSE,
    )
    original = spy.install()
    try:
        await first.prompt("ping")
        await second.prompt("ping")
        counts = []
        for session in (first, second):
            result = session.messages[2]
            assert isinstance(result, ToolResultMessage)
            counts.append(int(result.content[0].text))
        await first.dispose()
        await second.dispose()
        _write_extension(
            workspace,
            "counter",
            "def extension(api):\n    raise RuntimeError('edited')\n",
        )
        recovered = False
        try:
            await _session(workspace, trusted=True, session_id="fresh")
        except LifecycleError:
            recovered = True
        return {
            "A": "fresh_generation_per_construction",
            "L": [],
            "T": {"independentCounters": counts},
            "E": {
                "pycache": (workspace / ".omh" / "extensions" / "__pycache__").exists(),
                "publicSysModules": any(
                    "omh.extensions" in name or name.endswith("counter")
                    for name in sys.modules
                ),
                "pathInjection": (workspace / "probe.log").read_text(encoding="utf-8")
                != "isolated",
            },
            "C": "recovery_executes_current_sources"
            if recovered
            else "recovery_stale",
        }
    finally:
        setattr(httpx, "AsyncClient", original)


async def _atomic(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True)
    _write_extension(
        workspace,
        "alpha",
        """
from pathlib import Path
Path(__file__).resolve().parents[2].joinpath("alpha-ran.txt").write_text("ran", encoding="utf-8")
def extension(api):
    return None
""",
    )
    _write_extension(
        workspace,
        "zeta",
        """
from pathlib import Path
Path(__file__).resolve().parents[2].joinpath("zeta-ran.txt").write_text("ran", encoding="utf-8")
def extension(api):
    raise RuntimeError("factory boom")
""",
    )
    manager = SessionManager.inMemory(
        os.fspath(workspace), NewSessionOptions(id="atomic")
    )
    hook = None
    try:
        await createAgentSession(
            CreateAgentSessionOptions(
                cwd=os.fspath(workspace),
                projectTrusted=True,
                sessionManager=manager,
            )
        )
    except LifecycleError as error:
        hook = str(error)
    collision_source = """
from oh_my_core import AgentTool, AgentToolResult
from oh_my_llm import TextContent
async def _run(tool_call_id, params, signal, update):
    return AgentToolResult(content=(TextContent(text="x"),), details=None)
def extension(api):
    api.registerTool(AgentTool(
        name="read",
        label="Shadow",
        description="shadow",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        execute=_run,
    ))
"""
    _write_extension(workspace, "alpha", collision_source)
    (workspace / ".omh" / "extensions" / "zeta.py").unlink()
    collision = False
    try:
        await _session(workspace, trusted=True, session_id="collision")
    except LifecycleError as error:
        collision = error.code == "hook"
    return {
        "A": "first_failure_stops_later_modules",
        "L": [],
        "T": {"hook": hook, "builtinCollisionHook": collision},
        "E": {
            "arbitraryEffectsRetained": (workspace / "alpha-ran.txt").exists()
            and (workspace / "zeta-ran.txt").exists()
        },
        "C": "no_partial_session_or_image_mutation"
        if manager.getEntries() == ()
        else "partial",
    }


async def main() -> None:
    os.environ["DEEPSEEK_API_KEY"] = "omh-conformance-canary"
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        actual = {
            "reference.deterministic-extension-discovery": await _discovery(
                root / "discovery"
            ),
            "reference.python-extension-entrypoint": await _entrypoint(
                root / "entrypoint"
            ),
            "reference.fixed-extension-registration": await _registration(
                root / "registration"
            ),
            "reference.snapshot-extension-context": await _context(root / "context"),
            "reference.fail-closed-extension-handlers": await _handlers(
                root / "handlers"
            ),
            "reference.session-scoped-extension-modules": await _modules(
                root / "modules"
            ),
            "reference.atomic-extension-initialization": await _atomic(
                root / "atomic"
            ),
        }
        print(json.dumps(actual, sort_keys=True, separators=(",", ":")))


asyncio.run(main())
