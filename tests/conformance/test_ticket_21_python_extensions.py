from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Any, cast

import httpx
import pytest

import oh_my_coding_agent
from oh_my_coding_agent import (
    AgentSessionEvent,
    CreateAgentSessionOptions,
    ExtensionAPI,
    ExtensionContext,
    NewSessionOptions,
    SessionManager,
    createAgentSession,
)
from oh_my_llm import LifecycleError, TextContent, ToolResultMessage


ROOT = Path(__file__).parents[2]
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

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = self

        class FakeClient(httpx.AsyncClient):
            def __init__(self, **kwargs: Any) -> None:
                kwargs = dict(kwargs)
                kwargs["transport"] = httpx.MockTransport(spy._handle)
                super().__init__(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

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


def _write_extension(workspace: Path, name: str, source: str) -> Path:
    directory = workspace / ".omh" / "extensions"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return path


def _invalid(name: str) -> str:
    return rf'Python Extension "\.omh/extensions/{name}\.py" is invalid'


async def _create(
    workspace: Path,
    *,
    trusted: bool | None = False,
    session_id: str = "ext",
    persist: bool = False,
    tmp_path: Path | None = None,
    manager: SessionManager | None = None,
) -> Any:
    if manager is None:
        if persist:
            assert tmp_path is not None
            manager = SessionManager.create(
                os.fspath(workspace),
                os.fspath(tmp_path / "sessions"),
                NewSessionOptions(id=session_id),
            )
        else:
            manager = SessionManager.inMemory(
                os.fspath(workspace), NewSessionOptions(id=session_id)
            )
    options = CreateAgentSessionOptions(
        cwd=os.fspath(workspace),
        sessionManager=manager,
        projectTrusted=False if trusted is None else trusted,
    )
    return (await createAgentSession(options)).session


def test_extension_public_surface_is_closed() -> None:
    assert "ExtensionAPI" in oh_my_coding_agent.__all__
    assert "ExtensionContext" in oh_my_coding_agent.__all__
    assert set(name for name in dir(ExtensionAPI) if not name.startswith("_")) == {
        "on",
        "registerTool",
    }
    with pytest.raises(TypeError, match="factory-produced"):
        ExtensionAPI()
    with pytest.raises(TypeError, match="factory-produced"):
        ExtensionContext()
    with pytest.raises(TypeError):

        class SubAPI(ExtensionAPI):  # type: ignore[misc]
            pass


def test_untrusted_construction_does_not_probe_extensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    spy = _HttpSpy()
    spy.install(monkeypatch)
    workspace = tmp_path / "project"
    workspace.mkdir()
    _write_extension(workspace, "echo", "this is not python")
    session = asyncio.run(_create(workspace, trusted=False))
    asyncio.run(session.prompt("inspect"))
    names = [
        tool["function"]["name"]
        for tool in json.loads(spy.requests[0].content)["tools"]
    ]
    assert names == ["read", "bash", "edit", "write"]
    assert "- echo:" not in session.systemPrompt
    asyncio.run(session.dispose())


def test_trusted_discovery_rejects_invalid_names_symlinks_and_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    workspace = tmp_path / "project"
    workspace.mkdir()
    _write_extension(workspace, "echo", _ECHO_SOURCE)
    _write_extension(workspace, "BadName", "def extension(api):\n    return None\n")
    with pytest.raises(ValueError, match=_invalid("BadName")):
        asyncio.run(_create(workspace, trusted=True))

    (workspace / ".omh" / "extensions" / "BadName.py").unlink()
    _write_extension(workspace, ".hidden", "def extension(api):\n    return None\n")
    (workspace / ".omh" / "extensions" / "notes.txt").write_text("nope", encoding="utf-8")
    nested = workspace / ".omh" / "extensions" / "nested"
    nested.mkdir()
    (nested / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    session = asyncio.run(_create(workspace, trusted=True, session_id="ok"))
    assert "- echo: Echo text back" in session.systemPrompt
    asyncio.run(session.dispose())

    target = tmp_path / "outside.py"
    target.write_text(_ECHO_SOURCE, encoding="utf-8")
    os.symlink(
        os.fspath(target),
        os.fspath(workspace / ".omh" / "extensions" / "linked.py"),
    )
    with pytest.raises(ValueError, match=_invalid("linked")):
        asyncio.run(_create(workspace, trusted=True, session_id="symlink"))
    (workspace / ".omh" / "extensions" / "linked.py").unlink()

    _write_extension(workspace, "broken", "def extension(api):\n    return None\n")
    (workspace / ".omh" / "extensions" / "broken.py").write_bytes(
        b"\xef\xbb\xbfdef extension(api):\n    return None\n"
    )
    with pytest.raises(ValueError, match=_invalid("broken")):
        asyncio.run(_create(workspace, trusted=True, session_id="bom"))

    (workspace / ".omh" / "extensions" / "broken.py").unlink()
    linked_dir = tmp_path / "ext-dir"
    linked_dir.mkdir()
    (workspace / ".omh" / "extensions").rename(tmp_path / "extensions-real")
    os.symlink(
        os.fspath(linked_dir),
        os.fspath(workspace / ".omh" / "extensions"),
    )
    with pytest.raises(
        ValueError, match=r'Python Extension directory "\.omh/extensions" is invalid'
    ):
        asyncio.run(_create(workspace, trusted=True, session_id="extdir"))


def test_invalid_entrypoint_and_syntax_reject_the_whole_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    workspace = tmp_path / "project"
    workspace.mkdir()
    _write_extension(workspace, "alpha", "not valid python ???")
    manager = SessionManager.inMemory(
        os.fspath(workspace), NewSessionOptions(id="syntax")
    )
    with pytest.raises(ValueError, match=_invalid("alpha")):
        asyncio.run(
            createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    projectTrusted=True,
                    sessionManager=manager,
                )
            )
        )
    assert manager.getEntries() == ()

    _write_extension(workspace, "alpha", "VALUE = 1\n")
    with pytest.raises(ValueError, match=_invalid("alpha")):
        asyncio.run(_create(workspace, trusted=True, session_id="missing"))

    _write_extension(workspace, "alpha", "extension = 1\n")
    with pytest.raises(ValueError, match=_invalid("alpha")):
        asyncio.run(_create(workspace, trusted=True, session_id="noncallable"))

    _write_extension(
        workspace,
        "alpha",
        "def setup(api):\n    return None\n",
    )
    with pytest.raises(ValueError, match=_invalid("alpha")):
        asyncio.run(_create(workspace, trusted=True, session_id="alias"))


def test_builtin_collision_and_duplicate_extension_tools_reject_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    workspace = tmp_path / "project"
    workspace.mkdir()
    _write_extension(
        workspace,
        "alpha",
        """
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
""",
    )
    manager = SessionManager.inMemory(
        os.fspath(workspace), NewSessionOptions(id="collision")
    )
    with pytest.raises(LifecycleError) as collision:
        asyncio.run(
            createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    projectTrusted=True,
                    sessionManager=manager,
                )
            )
        )
    assert collision.value.code == "hook"
    assert str(collision.value) == (
        'Python Extension ".omh/extensions/alpha.py" initialization failed'
    )
    assert manager.getEntries() == ()

    _write_extension(workspace, "alpha", _ECHO_SOURCE)
    _write_extension(workspace, "zeta", _ECHO_SOURCE)
    with pytest.raises(LifecycleError) as duplicate:
        asyncio.run(_create(workspace, trusted=True, session_id="dup"))
    assert duplicate.value.code == "hook"
    assert str(duplicate.value) == (
        'Python Extension ".omh/extensions/zeta.py" initialization failed'
    )


def test_extension_tools_follow_builtins_and_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    workspace = tmp_path / "project"
    workspace.mkdir()
    _write_extension(
        workspace,
        "zeta",
        _ECHO_SOURCE.replace('name="echo"', 'name="zeta_echo"').replace(
            "Echo text back", "Zeta echo"
        ),
    )
    _write_extension(workspace, "alpha", _ECHO_SOURCE)
    spy = _HttpSpy(
        _tool_call_sse("call-echo", "echo", {"text": "hi"}),
        _STOP_SSE,
    )
    spy.install(monkeypatch)
    session = asyncio.run(_create(workspace, trusted=True))
    names = None
    events: list[str] = []
    session.subscribe(lambda event: events.append(event.type))
    asyncio.run(session.prompt("use echo"))
    names = [
        tool["function"]["name"]
        for tool in json.loads(spy.requests[0].content)["tools"]
    ]
    assert names == ["read", "bash", "edit", "write", "echo", "zeta_echo"]
    prompt = session.systemPrompt
    echo_at = prompt.index("- echo: Echo text back")
    zeta_at = prompt.index("- zeta_echo: Zeta echo")
    write_at = prompt.index("- write: Create or overwrite files")
    assert write_at < echo_at < zeta_at
    result = session.messages[2]
    assert isinstance(result, ToolResultMessage)
    content = result.content
    assert isinstance(content, tuple)
    assert isinstance(content[0], TextContent)
    assert content[0].text == "hi"
    asyncio.run(session.dispose())


def test_session_private_modules_ignore_edits_and_do_not_share_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    workspace = tmp_path / "project"
    workspace.mkdir()
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
    return AgentToolResult(
        content=(TextContent(text=str(COUNT)),),
        details=None,
    )

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
    def on_start(context):
        Path(context.cwd, "start.log").write_text(context.sessionId, encoding="utf-8")
    api.on("session_start", on_start)
""",
    )
    support = workspace / ".omh" / "extensions" / "support"
    support.mkdir()
    (support / "__init__.py").write_text("", encoding="utf-8")
    (support / "probe.py").write_text("VALUE = 1\n", encoding="utf-8")
    first = asyncio.run(_create(workspace, trusted=True, session_id="one"))
    second = asyncio.run(_create(workspace, trusted=True, session_id="two"))
    assert (workspace / "probe.log").read_text(encoding="utf-8") == "isolated"
    assert not (workspace / ".omh" / "extensions" / "__pycache__").exists()
    assert not any(
        name.endswith("counter") or "omh.extensions" in name
        for name in sys.modules
    )
    spy = _HttpSpy(
        _tool_call_sse("call-ping-1", "ping", {}),
        _STOP_SSE,
        _tool_call_sse("call-ping-2", "ping", {}),
        _STOP_SSE,
    )
    spy.install(monkeypatch)
    asyncio.run(first.prompt("ping"))
    first_result = first.messages[2]
    assert isinstance(first_result, ToolResultMessage)
    first_text = first_result.content[0].text
    asyncio.run(second.prompt("ping"))
    second_result = second.messages[2]
    assert isinstance(second_result, ToolResultMessage)
    assert first_text == "1"
    assert second_result.content[0].text == "1"
    _write_extension(
        workspace,
        "counter",
        "def extension(api):\n    raise RuntimeError('edited')\n",
    )
    asyncio.run(first.dispose())
    asyncio.run(second.dispose())
    with pytest.raises(LifecycleError) as recovered:
        asyncio.run(
            _create(
                workspace,
                trusted=True,
                persist=True,
                tmp_path=tmp_path,
                session_id="fresh",
            )
        )
    assert recovered.value.code == "hook"
    assert "counter.py" in str(recovered.value)


def test_initialization_failure_leaves_arbitrary_effects_and_no_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    workspace = tmp_path / "project"
    workspace.mkdir()
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
    with pytest.raises(LifecycleError) as failed:
        asyncio.run(
            createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    projectTrusted=True,
                    sessionManager=manager,
                )
            )
        )
    assert failed.value.code == "hook"
    assert str(failed.value) == (
        'Python Extension ".omh/extensions/zeta.py" initialization failed'
    )
    assert isinstance(failed.value.causes[0], RuntimeError)
    assert (workspace / "alpha-ran.txt").read_text(encoding="utf-8") == "ran"
    assert (workspace / "zeta-ran.txt").read_text(encoding="utf-8") == "ran"
    assert manager.getEntries() == ()


def test_registrations_freeze_and_session_start_precedes_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    workspace = tmp_path / "project"
    workspace.mkdir()
    _write_extension(
        workspace,
        "alpha",
        """
from pathlib import Path
from oh_my_core import AgentTool, AgentToolResult
from oh_my_llm import TextContent

RETAINED = []

async def _noop(tool_call_id, params, signal, update):
    return AgentToolResult(content=(TextContent(text="ok"),), details=None)

def extension(api):
    RETAINED.append(api)
    api.registerTool(AgentTool(
        name="alpha_tool",
        label="Alpha",
        description="alpha tool",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        execute=_noop,
    ))
    def on_start(context):
        Path(context.cwd, "started.txt").write_text("yes", encoding="utf-8")
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
    session = asyncio.run(_create(workspace, trusted=True, persist=True, tmp_path=tmp_path))
    assert (workspace / "started.txt").read_text(encoding="utf-8") == "yes"
    assert (workspace / "late.txt").read_text(encoding="utf-8") != "registered"
    assert "- late:" not in session.systemPrompt
    session_file = session.sessionFile
    assert session_file is not None
    assert not Path(session_file).exists()
    asyncio.run(session.dispose())

    _write_extension(
        workspace,
        "alpha",
        """
def extension(api):
    def on_start(context):
        raise RuntimeError("start failed")
    api.on("session_start", on_start)
""",
    )
    manager = SessionManager.inMemory(
        os.fspath(workspace), NewSessionOptions(id="start-fail")
    )
    with pytest.raises(LifecycleError) as failed:
        asyncio.run(
            createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    projectTrusted=True,
                    sessionManager=manager,
                )
            )
        )
    assert failed.value.code == "hook"
    assert str(failed.value) == "Python Extension handler failed"
    assert manager.getEntries() == ()


def test_handlers_run_before_listeners_and_receive_immutable_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    spy = _HttpSpy()
    spy.install(monkeypatch)
    workspace = tmp_path / "project"
    workspace.mkdir()
    _write_extension(
        workspace,
        "alpha",
        """
from pathlib import Path
import json

def _log(context, event_type, extra=""):
    path = Path(context.cwd, "events.log")
    prior = path.read_text(encoding="utf-8") if path.exists() else ""
    payload = {
        "event": event_type,
        "cwd": context.cwd,
        "sessionId": context.sessionId,
        "model": f"{context.model.provider}/{context.model.id}",
        "messages": len(context.messages),
        "signal": context.signal is not None,
        "extra": extra,
    }
    path.write_text(prior + json.dumps(payload) + "\\n", encoding="utf-8")

def extension(api):
    def on_start(context):
        _log(context, "session_start")
        assert context.signal is None
        try:
            context.cwd = "/tmp"
            mutated = True
        except Exception:
            mutated = False
        Path(context.cwd, "mutable.log").write_text(
            "mutated" if mutated else "frozen", encoding="utf-8"
        )
        Path(context.cwd, "retained.json").write_text(
            json.dumps({"messages": len(context.messages), "id": id(context)}),
            encoding="utf-8",
        )
        globals()["retained"] = context
    def on_agent_start(event, context):
        _log(context, "agent_start", type(event).__name__)
        retained = globals()["retained"]
        Path(context.cwd, "stale.json").write_text(
            json.dumps({
                "sameObject": retained is context,
                "startMessages": len(retained.messages),
                "nowMessages": len(context.messages),
                "startSignal": retained.signal is not None,
                "nowSignal": context.signal is not None,
            }),
            encoding="utf-8",
        )
        globals()["first_agent"] = context
    def on_agent_start_again(event, context):
        _log(context, "agent_start_again")
        Path(context.cwd, "distinct.json").write_text(
            json.dumps({"distinct": globals()["first_agent"] is not context}),
            encoding="utf-8",
        )
    api.on("session_start", on_start)
    api.on("agent_start", on_agent_start)
    api.on("agent_start", on_agent_start_again)
""",
    )
    _write_extension(
        workspace,
        "zeta",
        """
from pathlib import Path

def extension(api):
    def on_agent_start(event, context):
        path = Path(context.cwd, "events.log")
        prior = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(prior + '{"event":"zeta_agent_start"}\\n', encoding="utf-8")
    api.on("agent_start", on_agent_start)
""",
    )
    session = asyncio.run(_create(workspace, trusted=True))
    public: list[str] = []
    session.subscribe(lambda event: public.append(event.type))
    asyncio.run(session.prompt("hello"))
    lines = [
        json.loads(line)
        for line in (workspace / "events.log").read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert [row["event"] for row in lines[:4]] == [
        "session_start",
        "agent_start",
        "agent_start_again",
        "zeta_agent_start",
    ]
    assert public[0] == "agent_start"
    assert (workspace / "mutable.log").read_text(encoding="utf-8") == "frozen"
    stale = json.loads((workspace / "stale.json").read_text(encoding="utf-8"))
    assert stale["sameObject"] is False
    assert stale["startMessages"] == 0
    assert stale["nowMessages"] == 1
    assert stale["startSignal"] is False
    assert stale["nowSignal"] is True
    distinct = json.loads((workspace / "distinct.json").read_text(encoding="utf-8"))
    assert distinct["distinct"] is True
    start = json.loads((workspace / "events.log").read_text(encoding="utf-8").splitlines()[0])
    assert start["cwd"] == os.fspath(workspace)
    assert start["sessionId"] == session.sessionId
    assert start["model"] == "deepseek/deepseek-v4-flash"
    assert start["signal"] is False
    asyncio.run(session.dispose())


def test_handler_failure_stops_later_progression_and_preserves_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    spy = _HttpSpy()
    spy.install(monkeypatch)
    workspace = tmp_path / "project"
    workspace.mkdir()
    _write_extension(
        workspace,
        "alpha",
        """
from pathlib import Path

def extension(api):
    def on_agent_start(event, context):
        Path(context.cwd, "alpha-start.txt").write_text("ran", encoding="utf-8")
        raise RuntimeError("handler boom")
    def on_turn_start(event, context):
        Path(context.cwd, "alpha-turn.txt").write_text("ran", encoding="utf-8")
    api.on("agent_start", on_agent_start)
    api.on("turn_start", on_turn_start)
""",
    )
    _write_extension(
        workspace,
        "zeta",
        """
from pathlib import Path

def extension(api):
    def on_agent_start(event, context):
        Path(context.cwd, "zeta-start.txt").write_text("ran", encoding="utf-8")
    api.on("agent_start", on_agent_start)
""",
    )
    session = asyncio.run(_create(workspace, trusted=True))
    public: list[str] = []
    session.subscribe(lambda event: public.append(event.type))
    with pytest.raises(LifecycleError) as failed:
        asyncio.run(session.prompt("hello"))
    assert failed.value.code == "hook"
    assert str(failed.value) == "Python Extension handler failed"
    assert isinstance(failed.value.causes[0], RuntimeError)
    assert (workspace / "alpha-start.txt").read_text(encoding="utf-8") == "ran"
    assert not (workspace / "zeta-start.txt").exists()
    assert not (workspace / "alpha-turn.txt").exists()
    assert public == []
    assert spy.requests == []
    history = session.sessionManager.getEntries()
    assert any(
        type(getattr(entry, "message", None)).__name__ == "UserMessage"
        for entry in history
    )
    asyncio.run(session.dispose())


def test_shutdown_runs_reverse_and_retries_unresolved_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    workspace = tmp_path / "project"
    workspace.mkdir()
    _write_extension(
        workspace,
        "alpha",
        """
from pathlib import Path

def extension(api):
    def on_start(context):
        Path(context.cwd, "order.log").write_text("alpha-start\\n", encoding="utf-8")
    def on_stop(context):
        path = Path(context.cwd, "order.log")
        path.write_text(path.read_text(encoding="utf-8") + "alpha-stop\\n", encoding="utf-8")
    api.on("session_start", on_start)
    api.on("session_shutdown", on_stop)
""",
    )
    _write_extension(
        workspace,
        "zeta",
        """
from pathlib import Path

FAILS = {"count": 0}

def extension(api):
    def on_start(context):
        path = Path(context.cwd, "order.log")
        path.write_text(path.read_text(encoding="utf-8") + "zeta-start\\n", encoding="utf-8")
    def on_stop(context):
        FAILS["count"] += 1
        path = Path(context.cwd, "order.log")
        path.write_text(path.read_text(encoding="utf-8") + f"zeta-stop-{FAILS['count']}\\n", encoding="utf-8")
        if FAILS["count"] == 1:
            raise RuntimeError("shutdown boom")
    api.on("session_start", on_start)
    api.on("session_shutdown", on_stop)
""",
    )
    session = asyncio.run(_create(workspace, trusted=True))
    with pytest.raises(LifecycleError) as first:
        asyncio.run(session.dispose())
    assert first.value.code == "disposal"
    order = (workspace / "order.log").read_text(encoding="utf-8").splitlines()
    assert order == ["alpha-start", "zeta-start", "zeta-stop-1", "alpha-stop"]
    asyncio.run(session.dispose())
    order = (workspace / "order.log").read_text(encoding="utf-8").splitlines()
    assert order == [
        "alpha-start",
        "zeta-start",
        "zeta-stop-1",
        "alpha-stop",
        "zeta-stop-2",
    ]


def test_async_factory_cancellation_exposes_no_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    workspace = tmp_path / "project"
    workspace.mkdir()
    _write_extension(
        workspace,
        "alpha",
        """
import asyncio
from pathlib import Path

async def extension(api):
    Path(__file__).resolve().parents[2].joinpath("entered.txt").write_text("yes", encoding="utf-8")
    await asyncio.Event().wait()
""",
    )
    manager = SessionManager.inMemory(
        os.fspath(workspace), NewSessionOptions(id="cancel")
    )

    async def cancel_factory() -> None:
        operation = asyncio.create_task(
            createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    projectTrusted=True,
                    sessionManager=manager,
                )
            )
        )
        for _ in range(50):
            if (workspace / "entered.txt").exists():
                break
            await asyncio.sleep(0)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation

    asyncio.run(cancel_factory())
    assert manager.getEntries() == ()
    assert (workspace / "entered.txt").read_text(encoding="utf-8") == "yes"


def test_ticket_21_matrix_records_extension_obligations() -> None:
    matrix = json.loads((ROOT / "conformance/obligation-matrix.json").read_text())
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    required = {
        "omh-v0.deterministic-extension-discovery": (
            "reference.deterministic-extension-discovery"
        ),
        "omh-v0.python-extension-entrypoint": "reference.python-extension-entrypoint",
        "omh-v0.fixed-extension-registration": "reference.fixed-extension-registration",
        "omh-v0.snapshot-extension-context": "reference.snapshot-extension-context",
        "omh-v0.fail-closed-extension-handlers": (
            "reference.fail-closed-extension-handlers"
        ),
        "omh-v0.session-scoped-extension-modules": (
            "reference.session-scoped-extension-modules"
        ),
        "omh-v0.atomic-extension-initialization": (
            "reference.atomic-extension-initialization"
        ),
    }
    rows = {row["id"]: row for row in matrix["obligations"]}
    cases = {case["id"]: case for case in corpus["cases"]}
    assert required.keys() <= rows.keys()
    assert set(required.values()) <= cases.keys()
    for obligation, corpus_case in required.items():
        assert rows[obligation]["corpusCase"] == corpus_case
        assert rows[obligation]["executableCases"] == [
            "ticket-21-extensions",
            "ticket-21-installed",
        ]
        assert cases[corpus_case]["obligation"] == obligation
    trust = rows["omh-v0.explicit-project-resource-trust"]
    assert "ticket-21-extensions" in trust["executableCases"]
    builtin = rows["omh-v0.builtin-tool-registry"]
    assert "ticket-21-extensions" in builtin["executableCases"]
