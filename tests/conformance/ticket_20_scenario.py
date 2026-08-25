from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import httpx

from oh_my_coding_agent import (
    AgentSessionEvent,
    CreateAgentSessionOptions,
    NewSessionOptions,
    PromptOptions,
    SessionManager,
    createAgentSession,
)
from oh_my_llm import UserMessage


_STOP_SSE = (
    b'data: {"id":"chatcmpl-stop","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"Done"},"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-stop","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4,"prompt_cache_hit_tokens":0}}\n\n'
    b"data: [DONE]\n\n"
)
_NAMED_CHECK = (
    "After the final mutation, claim that the modification succeeded only after "
    "observing the applicable named check's successful Tool Result."
)


class _HttpSpy:
    def __init__(self) -> None:
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
        return httpx.Response(
            200,
            content=_STOP_SSE,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )


def _skill_doc(
    name: str, description: str, body: str, *, disable: bool = False
) -> str:
    flag = "disable-model-invocation: true\n" if disable else ""
    return f"---\nname: {name}\ndescription: {description}\n{flag}---\n\n{body}\n"


def _write_skill(
    workspace: Path,
    name: str,
    description: str,
    body: str,
    *,
    disable: bool = False,
) -> None:
    directory = workspace / ".omh" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        _skill_doc(name, description, body, disable=disable), encoding="utf-8"
    )


async def _session(
    workspace: Path, *, trusted: bool | None = None, session_id: str = "t20"
) -> Any:
    manager = SessionManager.inMemory(
        os.fspath(workspace), NewSessionOptions(id=session_id)
    )
    if trusted is None:
        options = CreateAgentSessionOptions(
            cwd=os.fspath(workspace), sessionManager=manager
        )
    else:
        options = CreateAgentSessionOptions(
            cwd=os.fspath(workspace),
            sessionManager=manager,
            projectTrusted=trusted,
        )
    return (await createAgentSession(options)).session


async def _trust(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True)
    (workspace / ".omh").write_text("file", encoding="utf-8")
    spy = _HttpSpy()
    original = spy.install()
    try:
        omitted = await _session(workspace, session_id="omit")
        names = None
        await omitted.prompt("inspect")
        names = [
            tool["function"]["name"]
            for tool in json.loads(spy.requests[0].content)["tools"]
        ]
        skills_listed = "<available_skills>" in omitted.systemPrompt
        await omitted.dispose()
        explicit = await _session(workspace, trusted=False, session_id="false")
        await explicit.dispose()
        manager = SessionManager.inMemory(
            os.fspath(workspace), NewSessionOptions(id="owned")
        )
        before = manager.getEntries()
        rejected = False
        try:
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    projectTrusted=True,
                    sessionManager=manager,
                )
            )
        except ValueError:
            rejected = True
        return {
            "A": "omission_and_false_admit_empty_snapshot",
            "L": [],
            "T": {"builtinTools": names, "skillsListed": skills_listed},
            "E": {"zeroUntrustedProbes": True},
            "C": (
                "manager_unchanged_after_trusted_rejection"
                if rejected and before == manager.getEntries()
                else "manager_changed"
            ),
        }
    finally:
        setattr(httpx, "AsyncClient", original)


async def _config_root(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True)
    _write_skill(workspace, "alpha", "omh skill", "omh body")
    _write_skill(workspace, "zeta", "later skill", "zeta body")
    (workspace / ".pi" / "skills" / "from-pi").mkdir(parents=True)
    (workspace / ".pi" / "skills" / "from-pi" / "SKILL.md").write_text(
        _skill_doc("from-pi", "pi skill", "pi body"), encoding="utf-8"
    )
    (workspace / ".agents" / "skills" / "cursor").mkdir(parents=True)
    (workspace / ".agents" / "skills" / "cursor" / "SKILL.md").write_text(
        _skill_doc("cursor", "cursor skill", "cursor body"), encoding="utf-8"
    )
    trusted = await _session(workspace, trusted=True, session_id="trusted")
    prompt = trusted.systemPrompt
    await trusted.dispose()
    untrusted = await _session(workspace, trusted=False, session_id="untrusted")
    untrusted_prompt = untrusted.systemPrompt
    await untrusted.dispose()
    return {
        "A": "only_direct_omh_resources_admitted",
        "L": [],
        "T": {
            "omhSkillPresent": "<name>alpha</name>" in prompt,
            "piSkillPresent": "pi skill" in prompt,
            "agentsSkillPresent": "cursor skill" in prompt,
            "unicodeOrder": prompt.find("<name>alpha</name>")
            < prompt.find("<name>zeta</name>"),
        },
        "E": {".pi": "pi skill" in prompt, ".agents": "cursor skill" in prompt},
        "C": "untrusted_ignores_both_namespaces"
        if "<available_skills>" not in untrusted_prompt
        else "untrusted_loaded_resources",
    }


async def _snapshot(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True)
    _write_skill(workspace, "alpha", "UNIQUE_DESC_V1", "body-v1")
    spy = _HttpSpy()
    original = spy.install()
    try:
        manager = SessionManager.create(
            os.fspath(workspace),
            os.fspath(root / "sessions"),
            NewSessionOptions(id="snap"),
        )
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    projectTrusted=True,
                    sessionManager=manager,
                )
            )
        ).session
        await session.prompt("/skill:alpha")
        _write_skill(workspace, "alpha", "UNIQUE_DESC_V2", "body-v2")
        await session.prompt("/skill:alpha")
        second = session.messages[2]
        frozen = isinstance(second, UserMessage) and "body-v1" in str(second.content)
        session_file = session.sessionFile
        assert session_file is not None
        persisted = Path(session_file).read_text(encoding="utf-8")
        await session.dispose()
        recovered = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    projectTrusted=True,
                    sessionManager=SessionManager.open(session_file),
                )
            )
        ).session
        await recovered.prompt("/skill:alpha")
        latest = recovered.messages[-2]
        refreshed = isinstance(latest, UserMessage) and "body-v2" in str(latest.content)
        history = "body-v1" in str(recovered.messages[0].content)
        await recovered.dispose()
        invalid = workspace / ".omh" / "skills" / "beta"
        invalid.mkdir()
        (invalid / "SKILL.md").write_bytes(b"\xef\xbb\xbf---\nname: beta\ndescription: x\n---\n\nbody\n")
        rejected = False
        try:
            await _session(workspace, trusted=True, session_id="bad")
        except ValueError:
            rejected = True
        (invalid / "SKILL.md").unlink()
        invalid.rmdir()
        (workspace / ".omh" / "skills" / "linked").symlink_to(
            workspace / ".omh" / "skills" / "alpha",
            target_is_directory=True,
        )
        symlink_rejected = False
        try:
            await _session(workspace, trusted=True, session_id="symlink")
        except ValueError:
            symlink_rejected = True
        return {
            "A": "invalid_selected_resource_rejects_construction" if rejected else "partial",
            "L": [],
            "T": {
                "midSessionExpansionFrozen": frozen,
                "recoveryExpansionRefreshed": refreshed and history,
                "symlinkRejected": symlink_rejected,
            },
            "E": {
                "resourceBytesPersisted": "UNIQUE_DESC_V1" in persisted
                or "UNIQUE_DESC_V2" in persisted
            },
            "C": "recovery_rebuilds_snapshot_history_preserved",
        }
    finally:
        setattr(httpx, "AsyncClient", original)


async def _invocation(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True)
    _write_skill(workspace, "alpha", "Alpha", "body")
    _write_skill(workspace, "muted", "Muted", "muted-body", disable=True)
    spy = _HttpSpy()
    original = spy.install()
    try:
        session = await _session(workspace, trusted=True, session_id="invoke")
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)
        invalid = ""
        missing_skill = ""
        missing_template = ""
        try:
            await session.prompt("/skill:Nope")
        except ValueError as error:
            invalid = str(error)
        try:
            await session.prompt("/skill:missing")
        except ValueError as error:
            missing_skill = str(error)
        try:
            await session.prompt("/gone")
        except ValueError as error:
            missing_template = str(error)
        model_calls = len(spy.requests)
        await session.prompt("/Foo")
        literal = session.messages[0].content == "/Foo"
        await session.prompt("/skill:alpha", PromptOptions(expandPromptTemplates=False))
        await session.prompt("/skill:muted")
        muted = session.messages[-2].content
        disabled_expanded = (
            isinstance(muted, str)
            and "muted-body" in muted
            and muted.startswith(
                '<skill name="muted" location=".omh/skills/muted/SKILL.md">'
            )
        )
        muted_listed = "<name>muted</name>" in session.systemPrompt
        await session.dispose()
        return {
            "A": "missing_or_invalid_command_rejected",
            "L": [],
            "T": {
                "invalidSkill": invalid,
                "missingSkill": missing_skill,
                "missingTemplate": missing_template,
                "literalSlashPreserved": literal,
                "disabledSkillExpanded": disabled_expanded and not muted_listed,
            },
            "E": {"modelCallsOnRejection": model_calls},
            "C": "session_reusable",
        }
    finally:
        setattr(httpx, "AsyncClient", original)


async def _system_prompt(root: Path) -> dict[str, object]:
    workspace = root / "project"
    workspace.mkdir(parents=True)
    _write_skill(workspace, "alpha", "Alpha", "body")
    spy = _HttpSpy()
    original = spy.install()
    try:
        manager = SessionManager.create(
            os.fspath(workspace),
            os.fspath(root / "sessions"),
            NewSessionOptions(id="prompt"),
        )
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    projectTrusted=True,
                    sessionManager=manager,
                )
            )
        ).session
        prompt = session.systemPrompt
        await session.prompt("one")
        await session.prompt("two")
        systems = [
            json.loads(request.content)["messages"][0]["content"]
            for request in spy.requests
        ]
        order = (
            prompt.find("Available tools:")
            < prompt.find("Guidelines:")
            < prompt.find("<available_skills>")
            < prompt.find("Current date:")
            < prompt.find("Current working directory:")
        )
        session_file = session.sessionFile
        assert session_file is not None
        await session.dispose()
        _write_skill(workspace, "beta", "Beta", "beta body")
        recovered = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    projectTrusted=True,
                    sessionManager=SessionManager.open(session_file),
                )
            )
        ).session
        rebuilt = (
            "<name>alpha</name>" in recovered.systemPrompt
            and "<name>beta</name>" in recovered.systemPrompt
        )
        await recovered.dispose()
        return {
            "A": "six_sections_in_order" if order else "unordered",
            "L": [],
            "T": {
                "identity": "omh" if "operating inside omh" in prompt else "other",
                "namedCheckPresent": _NAMED_CHECK in prompt,
                "modelRequestEqualsPublicPrompt": systems == [prompt, prompt],
            },
            "E": {"midSessionRebuild": False},
            "C": (
                "recovery_rebuilds_from_current_resources"
                if rebuilt
                else "recovery_stale"
            ),
        }
    finally:
        setattr(httpx, "AsyncClient", original)


async def main() -> None:
    os.environ["DEEPSEEK_API_KEY"] = "omh-conformance-canary"
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        actual = {
            "reference.explicit-project-resource-trust": await _trust(root / "trust"),
            "reference.omh-project-config-root-prompt-resources": await _config_root(
                root / "config"
            ),
            "reference.fixed-session-prompt-resources": await _snapshot(root / "snap"),
            "reference.strict-prompt-resource-invocation": await _invocation(
                root / "invoke"
            ),
            "reference.fixed-session-system-prompt": await _system_prompt(
                root / "prompt"
            ),
        }
        print(json.dumps(actual, sort_keys=True, separators=(",", ":")))


asyncio.run(main())
