from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from oh_my_coding_agent import (
    AgentSessionEvent,
    CreateAgentSessionOptions,
    NewSessionOptions,
    PromptOptions,
    SessionManager,
    createAgentSession,
)
from oh_my_llm import LifecycleError, UserMessage
import oh_my_coding_agent._system_prompt as system_prompt_module


ROOT = Path(__file__).parents[2]
_PINNED_DATE = "2026-08-25"
_STOP_SSE = (
    b'data: {"id":"chatcmpl-stop","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"Done"},"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-stop","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4,"prompt_cache_hit_tokens":0}}\n\n'
    b"data: [DONE]\n\n"
)
_IDENTITY = (
    "You are an expert coding assistant operating inside omh, a coding agent "
    "harness. You help users by reading files, executing commands, editing "
    "code, and writing new files."
)
_TOOL_LINES = (
    "- read: Read file contents",
    "- bash: Execute bash commands (ls, grep, find, etc.)",
    (
        "- edit: Make precise file edits with exact text replacement, including "
        "multiple disjoint edits in one call"
    ),
    "- write: Create or overwrite files",
)
_NAMED_CHECK = (
    "After the final mutation, claim that the modification succeeded only after "
    "observing the applicable named check's successful Tool Result. Otherwise "
    "report unverified or failed verification."
)


class _HttpSpy:
    def __init__(self, body: bytes = _STOP_SSE) -> None:
        self.body = body
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
        return httpx.Response(
            200,
            content=self.body,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )


class _BlockingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        return httpx.Response(
            200,
            content=_STOP_SSE,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport
) -> None:
    class FakeClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            kwargs = dict(kwargs)
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)


def _skill_doc(
    name: str,
    description: str,
    body: str,
    *,
    disable: bool | None = None,
) -> str:
    flag = ""
    if disable is not None:
        flag = f"\ndisable-model-invocation: {'true' if disable else 'false'}"
    return (
        f"---\nname: {name}\ndescription: {description}{flag}\n---\n\n{body}\n"
    )


def _template_doc(
    description: str, body: str, *, argument_hint: str | None = None
) -> str:
    hint = ""
    if argument_hint is not None:
        hint = f"\nargument-hint: {argument_hint}"
    return f"---\ndescription: {description}{hint}\n---\n\n{body}\n"


def _write_skill(
    workspace: Path,
    name: str,
    description: str,
    body: str,
    *,
    disable: bool | None = None,
) -> None:
    directory = workspace / ".omh" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        _skill_doc(name, description, body, disable=disable), encoding="utf-8"
    )


def _write_template(
    workspace: Path,
    name: str,
    description: str,
    body: str,
    *,
    argument_hint: str | None = None,
) -> None:
    directory = workspace / ".omh" / "prompts"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        _template_doc(description, body, argument_hint=argument_hint),
        encoding="utf-8",
    )


async def _create(
    workspace: Path,
    *,
    trusted: bool | None = None,
    session_id: str = "prompt-resources",
    persist: bool = False,
    tmp_path: Path | None = None,
) -> Any:
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
    return (
        await createAgentSession(
            CreateAgentSessionOptions(
                cwd=os.fspath(workspace),
                sessionManager=manager,
                projectTrusted=False if trusted is None else trusted,
            )
        )
    ).session


def test_project_trusted_defaults_false_and_rejects_non_bool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / ".omh").write_text("not-a-directory", encoding="utf-8")

    manager = SessionManager.inMemory(
        os.fspath(workspace), NewSessionOptions(id="omit-trust")
    )
    omitted = asyncio.run(
        createAgentSession(
            CreateAgentSessionOptions(
                cwd=os.fspath(workspace), sessionManager=manager
            )
        )
    ).session
    explicit_false = asyncio.run(_create(workspace, trusted=False))
    assert "Available tools:" in omitted.systemPrompt
    assert omitted.systemPrompt == explicit_false.systemPrompt
    asyncio.run(omitted.dispose())
    asyncio.run(explicit_false.dispose())

    with pytest.raises(TypeError, match="projectTrusted"):
        asyncio.run(
            createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    projectTrusted=cast(Any, "yes"),
                    sessionManager=SessionManager.inMemory(
                        os.fspath(workspace), NewSessionOptions(id="bad-trust")
                    ),
                )
            )
        )


def test_untrusted_construction_does_not_probe_project_resources_or_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    spy = _HttpSpy()
    spy.install(monkeypatch)
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / ".omh").write_bytes(b"file")
    (workspace / ".pi" / "skills" / "hidden").mkdir(parents=True)
    (workspace / ".pi" / "skills" / "hidden" / "SKILL.md").write_text(
        _skill_doc("hidden", "should not load", "pi body"), encoding="utf-8"
    )
    (workspace / ".agents" / "skills" / "nested").mkdir(parents=True)

    session = asyncio.run(_create(workspace))
    events: list[AgentSessionEvent] = []
    session.subscribe(events.append)
    asyncio.run(session.prompt("inspect"))
    body = json.loads(spy.requests[0].content)
    names = [tool["function"]["name"] for tool in body["tools"]]
    assert names == ["read", "bash", "edit", "write"]
    assert "<available_skills>" not in session.systemPrompt
    assert "pi body" not in session.systemPrompt
    asyncio.run(session.dispose())


def test_trusted_rejects_non_directory_omh_and_keeps_manager_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / ".omh").write_text("file", encoding="utf-8")
    manager = SessionManager.inMemory(
        os.fspath(workspace), NewSessionOptions(id="owned")
    )
    before = (manager.getHeader(), manager.getEntries())
    with pytest.raises(ValueError, match=r'Project configuration "\.omh" is invalid'):
        asyncio.run(
            createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    projectTrusted=True,
                    sessionManager=manager,
                )
            )
        )
    assert (manager.getHeader(), manager.getEntries()) == before


def test_trusted_discovery_order_excludes_other_roots_and_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    monkeypatch.setattr(system_prompt_module, "_local_date", lambda: _PINNED_DATE)
    spy = _HttpSpy()
    spy.install(monkeypatch)
    workspace = tmp_path / "project"
    workspace.mkdir()
    _write_skill(workspace, "zeta", "Z skill", "zeta body")
    _write_skill(workspace, "alpha", "A skill", "alpha body")
    _write_skill(
        workspace, "muted", "Muted skill", "muted body", disable=True
    )
    (workspace / ".omh" / "skills" / ".hidden").mkdir()
    (workspace / ".omh" / "skills" / ".hidden" / "SKILL.md").write_text(
        _skill_doc(".hidden", "hidden", "hidden body"), encoding="utf-8"
    )
    nested = workspace / ".omh" / "skills" / "alpha" / "nested"
    nested.mkdir()
    (nested / "SKILL.md").write_text(
        _skill_doc("nested", "nested", "nested body"), encoding="utf-8"
    )
    (workspace / ".omh" / "skills" / "notes.txt").write_text("ignore", encoding="utf-8")
    _write_template(workspace, "wrap", "Wrap things", "Hello $1")
    (workspace / ".omh" / "prompts" / "readme.txt").write_text("ignore", encoding="utf-8")
    (workspace / ".pi" / "skills" / "from-pi").mkdir(parents=True)
    (workspace / ".pi" / "skills" / "from-pi" / "SKILL.md").write_text(
        _skill_doc("from-pi", "pi skill", "pi body"), encoding="utf-8"
    )
    (workspace / ".agents" / "skills" / "cursor").mkdir(parents=True)
    (workspace / ".agents" / "skills" / "cursor" / "SKILL.md").write_text(
        _skill_doc("cursor", "cursor skill", "cursor body"), encoding="utf-8"
    )

    session = asyncio.run(_create(workspace, trusted=True))
    prompt = session.systemPrompt
    alpha_at = prompt.index("<name>alpha</name>")
    zeta_at = prompt.index("<name>zeta</name>")
    assert alpha_at < zeta_at
    assert "<name>muted</name>" not in prompt
    asyncio.run(session.prompt("/skill:muted"))
    muted = session.messages[0].content
    assert isinstance(muted, str)
    assert muted.startswith(
        '<skill name="muted" location=".omh/skills/muted/SKILL.md">'
    )
    assert "muted body" in muted
    assert "from-pi" not in prompt
    assert "cursor skill" not in prompt
    assert "hidden body" not in prompt
    assert ".omh/skills/alpha/SKILL.md" in prompt
    asyncio.run(session.dispose())

    linked = tmp_path / "linked-skill"
    linked.mkdir()
    (linked / "SKILL.md").write_text(
        _skill_doc("linked", "linked", "linked body"), encoding="utf-8"
    )
    os.symlink(
        os.fspath(linked),
        os.fspath(workspace / ".omh" / "skills" / "linked"),
    )
    with pytest.raises(ValueError, match="Skill "):
        asyncio.run(_create(workspace, trusted=True, session_id="symlink"))


def test_invalid_selected_resource_rejects_the_whole_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    workspace = tmp_path / "project"
    workspace.mkdir()
    _write_skill(workspace, "alpha", "ok", "alpha body")
    bad = workspace / ".omh" / "skills" / "beta"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_bytes(b"\xef\xbb\xbf---\nname: beta\ndescription: bom\n---\n\nbody\n")
    manager = SessionManager.inMemory(
        os.fspath(workspace), NewSessionOptions(id="atomic")
    )
    with pytest.raises(ValueError, match=r'Skill "\.omh/skills/beta/SKILL.md" is invalid'):
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

    (bad / "SKILL.md").write_text(
        "---\nname: beta\ndescription: ok\n---\n\nbeta body\n", encoding="utf-8"
    )
    prompts = workspace / ".omh" / "prompts"
    prompts.mkdir()
    (prompts / "Nope.md").write_text(
        _template_doc("bad name", "body"), encoding="utf-8"
    )
    with pytest.raises(
        ValueError, match=r'Prompt Template "\.omh/prompts/Nope.md" is invalid'
    ):
        asyncio.run(
            createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    projectTrusted=True,
                    sessionManager=manager,
                )
            )
        )

    (prompts / "Nope.md").unlink()
    (workspace / ".omh" / "skills" / "gamma").mkdir()
    (workspace / ".omh" / "skills" / "gamma" / "SKILL.md").write_text(
        "---\nname: gamma\ndescription: flag\ndisable-model-invocation: yes\n---\n\nbody\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r'Skill "\.omh/skills/gamma/SKILL.md" is invalid'):
        asyncio.run(
            createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    projectTrusted=True,
                    sessionManager=manager,
                )
            )
        )
    (workspace / ".omh" / "skills" / "gamma" / "SKILL.md").write_text(
        '---\nname: gamma\ndescription: flag\ndisable-model-invocation: "true"\n---\n\nbody\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r'Skill "\.omh/skills/gamma/SKILL.md" is invalid'):
        asyncio.run(
            createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    projectTrusted=True,
                    sessionManager=manager,
                )
            )
        )
    (workspace / ".omh" / "skills" / "gamma" / "SKILL.md").unlink()
    os.symlink(
        os.fspath(workspace / ".omh" / "skills" / "alpha" / "SKILL.md"),
        os.fspath(workspace / ".omh" / "skills" / "gamma" / "SKILL.md"),
    )
    with pytest.raises(ValueError, match=r'Skill "\.omh/skills/gamma/SKILL.md" is invalid'):
        asyncio.run(
            createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    projectTrusted=True,
                    sessionManager=manager,
                )
            )
        )
    (workspace / ".omh" / "skills" / "gamma" / "SKILL.md").unlink()
    (workspace / ".omh" / "skills" / "gamma").rmdir()
    (prompts / "linked.md").symlink_to(workspace / ".omh" / "skills" / "alpha" / "SKILL.md")
    with pytest.raises(
        ValueError, match=r'Prompt Template "\.omh/prompts/linked.md" is invalid'
    ):
        asyncio.run(
            createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    projectTrusted=True,
                    sessionManager=manager,
                )
            )
        )


def test_snapshot_ignores_mid_session_edits_and_recovery_rebuilds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    monkeypatch.setattr(system_prompt_module, "_local_date", lambda: _PINNED_DATE)
    spy = _HttpSpy()
    spy.install(monkeypatch)
    workspace = tmp_path / "project"
    workspace.mkdir()
    _write_skill(workspace, "alpha", "UNIQUE_DESC_V1", "body-v1")
    session = asyncio.run(
        _create(workspace, trusted=True, persist=True, tmp_path=tmp_path)
    )
    first_prompt = session.systemPrompt
    assert "UNIQUE_DESC_V1" in first_prompt
    asyncio.run(session.prompt("/skill:alpha"))
    first_user = session.messages[0]
    assert isinstance(first_user, UserMessage)
    assert isinstance(first_user.content, str)
    assert first_user.content.endswith("body-v1\n</skill>")
    _write_skill(workspace, "alpha", "UNIQUE_DESC_V2", "body-v2")
    asyncio.run(session.prompt("/skill:alpha extra"))
    second_user = session.messages[2]
    assert isinstance(second_user, UserMessage)
    assert isinstance(second_user.content, str)
    assert "body-v1" in second_user.content
    assert "body-v2" not in second_user.content
    assert session.systemPrompt == first_prompt
    session_file = session.sessionFile
    assert session_file is not None
    persisted = Path(session_file).read_text(encoding="utf-8")
    assert "UNIQUE_DESC_V1" not in persisted
    assert "UNIQUE_DESC_V2" not in persisted
    asyncio.run(session.dispose())

    reopened = SessionManager.open(session_file)
    recovered = asyncio.run(
        createAgentSession(
            CreateAgentSessionOptions(
                cwd=os.fspath(workspace),
                projectTrusted=True,
                sessionManager=reopened,
            )
        )
    ).session
    assert recovered.systemPrompt != first_prompt
    assert "UNIQUE_DESC_V2" in recovered.systemPrompt
    assert isinstance(recovered.messages[0].content, str)
    assert recovered.messages[0].content.endswith("body-v1\n</skill>")
    asyncio.run(recovered.prompt("/skill:alpha"))
    latest_user = recovered.messages[-2]
    assert isinstance(latest_user, UserMessage)
    assert isinstance(latest_user.content, str)
    assert latest_user.content.endswith("body-v2\n</skill>")
    asyncio.run(recovered.dispose())


def test_command_grammar_expansion_rejection_and_literal_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    spy = _HttpSpy()
    spy.install(monkeypatch)
    workspace = tmp_path / "project"
    workspace.mkdir()
    _write_skill(workspace, "alpha", "Alpha", "Use $1 literally")
    _write_template(
        workspace,
        "greet",
        "Greet",
        "Hello $1 and ${2:-world}. All: $@. Slice: ${@:2}. Zero: ${0:-def}. Keep $1. Odd: $١.",
        argument_hint="name",
    )

    session = asyncio.run(_create(workspace, trusted=True))
    events: list[AgentSessionEvent] = []
    session.subscribe(events.append)
    before = (session.messages, session.sessionManager.getEntries())

    with pytest.raises(ValueError, match="Invalid Skill invocation"):
        asyncio.run(session.prompt("/skill:"))
    with pytest.raises(ValueError, match="Invalid Skill invocation"):
        asyncio.run(session.prompt("/skill:Nope"))
    with pytest.raises(ValueError, match=r'Skill "missing" was not found'):
        asyncio.run(session.prompt("/skill:missing"))
    with pytest.raises(ValueError, match=r'Prompt Template "gone" was not found'):
        asyncio.run(session.prompt("/gone"))
    assert (session.messages, session.sessionManager.getEntries()) == before
    assert events == []
    assert spy.requests == []
    assert session.isIdle is True

    asyncio.run(session.prompt("/skill:alpha  keep  spacing"))
    skill_text = session.messages[0].content
    assert skill_text == (
        '<skill name="alpha" location=".omh/skills/alpha/SKILL.md">\n'
        "References are relative to .omh/skills/alpha.\n"
        "\n"
        "Use $1 literally\n"
        "</skill>\n"
        "\n"
        "keep  spacing"
    )
    asyncio.run(session.prompt("/greet 'a b' c"))
    assert session.messages[2].content == (
        "Hello a b and c. All: a b c. Slice: c. Zero: def. Keep a b. Odd: $١."
    )
    asyncio.run(session.prompt("/Foo"))
    assert session.messages[4].content == "/Foo"
    asyncio.run(session.prompt(" /skill:alpha"))
    assert session.messages[6].content == " /skill:alpha"
    asyncio.run(
        session.prompt("/skill:alpha", PromptOptions(expandPromptTemplates=False))
    )
    assert session.messages[8].content == "/skill:alpha"
    asyncio.run(session.dispose())


def test_fixed_six_section_prompt_stays_stable_for_one_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    monkeypatch.setattr(system_prompt_module, "_local_date", lambda: _PINNED_DATE)
    spy = _HttpSpy()
    spy.install(monkeypatch)
    workspace = tmp_path / "project"
    workspace.mkdir()
    _write_skill(workspace, "alpha", 'Say "hi" & <go>', "body")
    cwd = os.fspath(workspace)
    session = asyncio.run(_create(workspace, trusted=True))
    prompt = session.systemPrompt
    assert prompt.startswith(_IDENTITY + "\n\nAvailable tools:\n")
    for line in _TOOL_LINES:
        assert line in prompt
    assert prompt.index("Available tools:") < prompt.index("Guidelines:")
    assert prompt.index("Guidelines:") < prompt.index("<available_skills>")
    assert prompt.index("<available_skills>") < prompt.index("Current date:")
    assert _NAMED_CHECK in prompt
    assert "&amp;" in prompt and "&lt;go&gt;" in prompt
    assert prompt.endswith(
        f"Current date: {_PINNED_DATE}\nCurrent working directory: {cwd}"
    )
    asyncio.run(session.prompt("one"))
    asyncio.run(session.prompt("two"))
    systems = [
        json.loads(request.content)["messages"][0]["content"]
        for request in spy.requests
    ]
    assert systems == [prompt, prompt]
    asyncio.run(session.dispose())


def test_busy_rejection_precedes_missing_resource_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    transport = _BlockingTransport()
    _install_transport(monkeypatch, transport)
    workspace = tmp_path / "project"
    workspace.mkdir()
    session = asyncio.run(_create(workspace, trusted=True))

    async def scenario() -> None:
        first = asyncio.create_task(session.prompt("hello"))
        await transport.started.wait()
        with pytest.raises(LifecycleError) as caught:
            await session.prompt("/skill:missing")
        assert caught.value.code == "busy"
        transport.release.set()
        await first
        await session.dispose()

    asyncio.run(scenario())


def test_ticket_20_matrix_records_prompt_resource_obligations() -> None:
    matrix = json.loads((ROOT / "conformance/obligation-matrix.json").read_text())
    corpus = json.loads(
        (ROOT / "conformance/reference-observation-corpus.json").read_text()
    )
    required = {
        "omh-v0.explicit-project-resource-trust": "reference.explicit-project-resource-trust",
        "omh-v0.omh-project-config-root-prompt-resources": "reference.omh-project-config-root-prompt-resources",
        "omh-v0.fixed-session-prompt-resources": "reference.fixed-session-prompt-resources",
        "omh-v0.strict-prompt-resource-invocation": "reference.strict-prompt-resource-invocation",
        "omh-v0.fixed-session-system-prompt": "reference.fixed-session-system-prompt",
    }
    rows = {row["id"]: row for row in matrix["obligations"]}
    cases = {case["id"]: case for case in corpus["cases"]}
    assert required.keys() <= rows.keys()
    assert set(required.values()) <= cases.keys()
    for obligation, corpus_case in required.items():
        assert rows[obligation]["corpusCase"] == corpus_case
        assert rows[obligation]["executableCases"] == [
            "ticket-20-resources",
            "ticket-20-installed",
        ]
        assert cases[corpus_case]["obligation"] == obligation
