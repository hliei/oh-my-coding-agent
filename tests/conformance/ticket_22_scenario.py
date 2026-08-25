from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import contextmanager
from functools import wraps
import inspect
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Coroutine, Iterator, ParamSpec, TypeVar

import httpx

from oh_my_coding_agent import (
    AgentSessionEvent,
    CreateAgentSessionOptions,
    NewSessionOptions,
    SessionMessageEntry,
    SessionManager,
    createAgentSession,
)
from oh_my_llm import (
    AssistantMessage,
    LifecycleError,
    TextContent,
    ToolResultMessage,
    UserMessage,
    fauxAssistantMessage,
)


_FINAL_REPORT = (
    "Changed greeting() to return hello, omh. "
    "Ran `python -m unittest -q`; it passed."
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


def _isolated_api_key(
    value: str,
) -> Callable[
    [Callable[_P, Coroutine[Any, Any, _R]]],
    Callable[_P, Coroutine[Any, Any, _R]],
]:
    def decorate(
        operation: Callable[_P, Coroutine[Any, Any, _R]],
    ) -> Callable[_P, Coroutine[Any, Any, _R]]:
        @wraps(operation)
        async def isolated(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            previous = os.environ.get("DEEPSEEK_API_KEY")
            os.environ["DEEPSEEK_API_KEY"] = value
            try:
                return await operation(*args, **kwargs)
            finally:
                if previous is None:
                    os.environ.pop("DEEPSEEK_API_KEY", None)
                else:
                    os.environ["DEEPSEEK_API_KEY"] = previous

        return isolated

    return decorate


def _sse(*payloads: dict[str, Any] | str) -> bytes:
    chunks: list[bytes] = []
    for payload in payloads:
        if payload == "[DONE]":
            chunks.append(b"data: [DONE]\n\n")
        else:
            chunks.append(
                b"data: "
                + json.dumps(payload, separators=(",", ":")).encode("utf-8")
                + b"\n\n"
            )
    return b"".join(chunks)


def _tool_call_sse(call_id: str, name: str, arguments: dict[str, Any]) -> bytes:
    return _sse(
        {
            "id": f"chatcmpl-{call_id}",
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
            "id": f"chatcmpl-{call_id}",
            "model": "deepseek-v4-flash",
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
            ],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 4,
                "total_tokens": 12,
                "prompt_cache_hit_tokens": 0,
            },
        },
        "[DONE]",
    )


def _text_sse(
    text: str, *, prompt_tokens: int = 8, completion_tokens: int = 4
) -> bytes:
    return _sse(
        {
            "id": "chatcmpl-report",
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": text},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-report",
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "prompt_cache_hit_tokens": 0,
            },
        },
        "[DONE]",
    )


class _ScriptedTransport(httpx.AsyncBaseTransport):
    def __init__(self, *responses: bytes) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("unexpected Provider request")
        return httpx.Response(
            200,
            content=self._responses.pop(0),
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )


def _install_transport(
    transport: httpx.AsyncBaseTransport,
) -> type[httpx.AsyncClient]:
    original = httpx.AsyncClient

    class FakeClient(original):  # type: ignore[valid-type,misc]
        def __init__(self, **kwargs: Any) -> None:
            kwargs = dict(kwargs)
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    httpx.AsyncClient = FakeClient  # type: ignore[misc]
    return original


@contextmanager
def _use_transport(transport: httpx.AsyncBaseTransport) -> Iterator[None]:
    original = _install_transport(transport)
    try:
        yield
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]


def _write_repository(workspace: Path) -> None:
    workspace.mkdir(parents=True)
    (workspace / "INSTRUCTIONS.md").write_text(
        "Change only app.py. The named check is `python -m unittest -q`.\n",
        encoding="utf-8",
    )
    (workspace / "app.py").write_text(
        'def greeting() -> str:\n    return "hello"\n', encoding="utf-8"
    )
    (workspace / "test_app.py").write_text(
        "import unittest\n\n"
        "from app import greeting\n\n\n"
        "class GreetingTest(unittest.TestCase):\n"
        "    def test_greeting(self) -> None:\n"
        '        self.assertEqual(greeting(), "hello, omh")\n',
        encoding="utf-8",
    )


def _selected_event(event: AgentSessionEvent) -> str | None:
    if isinstance(event, AgentSessionEvent.ToolExecutionStart):
        return f"tool_execution_start:{event.toolName}"
    if isinstance(event, AgentSessionEvent.ToolExecutionEnd):
        status = "failure" if event.isError else "ok"
        return f"tool_execution_end:{event.toolName}:{status}"
    if isinstance(event, AgentSessionEvent.AgentEnd):
        return "agent_end"
    if isinstance(event, AgentSessionEvent.AgentSettled):
        return "agent_settled"
    return None


@_isolated_api_key("omh-conformance-canary")
async def run_golden_journey(root: Path) -> dict[str, object]:
    workspace = root / "project"
    _write_repository(workspace)
    transport = _ScriptedTransport(
        _tool_call_sse("read-instructions", "read", {"path": "INSTRUCTIONS.md"}),
        _tool_call_sse("read-target", "read", {"path": "app.py"}),
        _tool_call_sse(
            "edit-target",
            "edit",
            {
                "path": "app.py",
                "edits": [
                    {
                        "oldText": '    return "hello"',
                        "newText": '    return "hello, omh"',
                    }
                ],
            },
        ),
        _tool_call_sse(
            "named-check", "bash", {"command": "python -m unittest -q"}
        ),
        _text_sse(_FINAL_REPORT),
    )
    with _use_transport(transport):
        manager = SessionManager.create(
            os.fspath(workspace),
            os.fspath(root / "sessions"),
            NewSessionOptions(id="programmatic-golden"),
        )
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    sessionManager=manager,
                    projectTrusted=False,
                )
            )
        ).session
        lifecycle: list[str] = []

        def observe(event: AgentSessionEvent) -> None:
            selected = _selected_event(event)
            if selected is not None:
                lifecycle.append(selected)

        session.subscribe(observe)
        await session.prompt(
            "Read INSTRUCTIONS.md and app.py. Make greeting return hello, omh, "
            "then run the repository's named check and report the result."
        )
        final = session.messages[-1]
        assert isinstance(final, AssistantMessage)
        assert len(final.content) == 1
        content = final.content[0]
        assert isinstance(content, TextContent)
        changed = (workspace / "app.py").read_text(encoding="utf-8")
        check_index = lifecycle.index("tool_execution_start:bash")
        mutation_index = lifecycle.index("tool_execution_end:edit:ok")
        result: dict[str, object] = {
            "A": {
                "model": session.model.id,
                "projectTrusted": False,
                "workspace": "existing_repository",
            },
            "L": lifecycle,
            "T": {
                "check": "python -m unittest -q",
                "finalReport": content.text,
                "stopReason": final.stopReason,
            },
            "E": {
                "checkAfterFinalMutation": check_index > mutation_index,
                "fileChanged": 'return "hello, omh"' in changed,
                "networkRequests": 0,
                "providerRequests": len(transport.requests),
            },
            "C": {
                "persistent": manager.isPersisted(),
                "treeEntries": len(manager.getEntries()),
            },
        }
        await session.dispose()
        return result


async def _run_verification_case(
    root: Path,
    *,
    responses: tuple[bytes, ...],
    session_id: str,
    prompt: str = (
        "Change greeting as requested and use `python -m unittest -q` "
        "as the named verification check."
    ),
) -> tuple[Any, list[AgentSessionEvent], _ScriptedTransport]:
    workspace = root / "project"
    _write_repository(workspace)
    transport = _ScriptedTransport(*responses)
    with _use_transport(transport):
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    sessionManager=SessionManager.inMemory(
                        os.fspath(workspace), NewSessionOptions(id=session_id)
                    ),
                    projectTrusted=False,
                )
            )
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)
        await session.prompt(prompt)
        return session, events, transport


def _edit_response(call_id: str, replacement: str = "hello, omh") -> bytes:
    return _tool_call_sse(
        call_id,
        "edit",
        {
            "path": "app.py",
            "edits": [
                {
                    "oldText": '    return "hello"',
                    "newText": f'    return "{replacement}"',
                }
            ],
        },
    )


def _tool_trace(events: list[AgentSessionEvent]) -> list[str]:
    trace: list[str] = []
    for event in events:
        if isinstance(event, AgentSessionEvent.ToolExecutionEnd):
            trace.append(f"{event.toolName}:{'failure' if event.isError else 'ok'}")
    return trace


def _bash_exit(events: list[AgentSessionEvent]) -> int:
    event = next(
        item
        for item in events
        if isinstance(item, AgentSessionEvent.ToolExecutionEnd)
        and item.toolName == "bash"
    )
    details = event.result.details
    if isinstance(details, Mapping) and details.get("code") == "nonzero_exit":
        exit_code = details.get("exitCode")
        assert isinstance(exit_code, int)
        return exit_code
    return 0


def _verification_status(session: Any) -> str:
    final = session.messages[-1]
    assert isinstance(final, AssistantMessage)
    text = "".join(
        content.text
        for content in final.content
        if isinstance(content, TextContent)
    ).lower()
    if "it passed" in text:
        return "verified"
    if "failed" in text:
        return "failed"
    return "unverified"


@_isolated_api_key("omh-conformance-canary")
async def run_verification_matrix(root: Path) -> dict[str, object]:
    no_check, no_check_events, _ = await _run_verification_case(
        root / "no-check",
        session_id="no-check",
        responses=(
            _edit_response("unchecked-edit"),
            _text_sse(
                "Changed greeting(), but no named check ran; modification remains "
                "unverified."
            ),
        ),
    )
    unavailable, unavailable_events, _ = await _run_verification_case(
        root / "unavailable",
        session_id="unavailable",
        prompt=(
            "Change greeting as requested and use `./CHECK` as the named "
            "verification check."
        ),
        responses=(
            _edit_response("unavailable-edit"),
            _tool_call_sse(
                "unavailable-check",
                "bash",
                {"command": "./CHECK"},
            ),
            _text_sse(
                "Changed greeting(), but the named check was unavailable; "
                "modification remains unverified."
            ),
        ),
    )
    failed, failed_events, _ = await _run_verification_case(
        root / "failed",
        session_id="failed",
        responses=(
            _edit_response("failed-edit", "wrong"),
            _tool_call_sse(
                "failed-check", "bash", {"command": "python -m unittest -q"}
            ),
            _text_sse(
                "Changed greeting(), but `python -m unittest -q` failed; "
                "modification is not verified."
            ),
        ),
    )
    recovered, recovered_events, _ = await _run_verification_case(
        root / "recovered",
        session_id="recovered",
        responses=(
            _tool_call_sse(
                "invalid-edit",
                "edit",
                {"path": "app.py", "edits": []},
            ),
            _edit_response("corrected-edit"),
            _tool_call_sse(
                "recovered-check", "bash", {"command": "python -m unittest -q"}
            ),
            _text_sse(_FINAL_REPORT),
        ),
    )
    unrelated, unrelated_events, _ = await _run_verification_case(
        root / "unrelated",
        session_id="unrelated",
        responses=(
            _edit_response("unrelated-edit"),
            _tool_call_sse(
                "unrelated-command", "bash", {"command": "printf unrelated"}
            ),
            _text_sse(
                "Changed greeting(), but only an unrelated command ran; the "
                "modification remains unverified."
            ),
        ),
    )
    traces = {
        "noCheck": [item.split(":", 1)[0] for item in _tool_trace(no_check_events)],
        "unavailableCheck": [
            item.split(":", 1)[0] for item in _tool_trace(unavailable_events)
        ],
        "failedCheck": [item.split(":", 1)[0] for item in _tool_trace(failed_events)],
        "recoveredToolFailure": _tool_trace(recovered_events),
        "unrelatedSuccessfulCommand": [
            item.split(":", 1)[0] for item in _tool_trace(unrelated_events)
        ],
    }
    same_run = sum(
        isinstance(event, AgentSessionEvent.AgentStart) for event in recovered_events
    ) == 1
    all_idle = all(
        session.isIdle
        for session in (no_check, unavailable, failed, recovered, unrelated)
    )
    await asyncio.gather(
        no_check.dispose(),
        unavailable.dispose(),
        failed.dispose(),
        recovered.dispose(),
        unrelated.dispose(),
    )
    return {
        "A": "named_check_required_after_final_mutation",
        "L": traces,
        "T": {
            "noCheck": _verification_status(no_check),
            "unavailableCheck": _verification_status(unavailable),
            "failedCheck": _verification_status(failed),
            "recoveredToolFailure": _verification_status(recovered),
            "unrelatedSuccessfulCommand": _verification_status(unrelated),
        },
        "E": {
            "failedCheckExit": _bash_exit(failed_events),
            "noCheckCommands": sum(
                isinstance(event, AgentSessionEvent.ToolExecutionStart)
                and event.toolName == "bash"
                for event in no_check_events
            ),
            "recoverableFailureWasProviderFailure": any(
                isinstance(event, AgentSessionEvent.MessageEnd)
                and isinstance(event.message, AssistantMessage)
                and event.message.stopReason == "error"
                for event in recovered_events
            ),
            "recoveredCheckExit": _bash_exit(recovered_events),
            "unavailableCheckExit": _bash_exit(unavailable_events),
            "unrelatedCommandExit": _bash_exit(unrelated_events),
        },
        "C": {
            "allRunsSettled": all_idle,
            "recoveredInSameRun": same_run,
        },
    }


class _FailureBarrierTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.cancel_started = asyncio.Event()
        self.cancelled_requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = len(self.requests)
        if index == 1:
            return httpx.Response(
                503, json={"error": {"message": "provider unavailable"}}, request=request
            )
        if index == 2:
            self.cancel_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled_requests += 1
                raise
        return httpx.Response(
            200,
            content=_text_sse("Reuse completed without claiming a modification."),
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )


@_isolated_api_key("omh-conformance-canary")
async def run_failure_boundaries(root: Path) -> dict[str, object]:
    workspace = root / "project"
    _write_repository(workspace)
    transport = _FailureBarrierTransport()
    with _use_transport(transport):
        session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    sessionManager=SessionManager.inMemory(
                        os.fspath(workspace), NewSessionOptions(id="boundaries")
                    ),
                    projectTrusted=False,
                )
            )
        ).session
        events: list[AgentSessionEvent] = []
        session.subscribe(events.append)
        await session.prompt("provider failure")

        cancelled_prompt = asyncio.create_task(session.prompt("cancel this run"))
        await transport.cancel_started.wait()
        before_busy = len(transport.requests)
        busy_code = ""
        try:
            await session.prompt("must be rejected while busy")
        except LifecycleError as error:
            busy_code = error.code
        after_busy = len(transport.requests)
        await session.abort()
        await cancelled_prompt
        await session.prompt("reuse after cancellation")

        assistants = [
            message
            for message in session.messages
            if isinstance(message, AssistantMessage)
        ]
        stop_reasons = [message.stopReason for message in assistants]
        result: dict[str, object] = {
            "A": {
                "busy": busy_code,
                "cancel": "admitted",
                "providerFailure": "admitted",
                "reuse": "admitted",
            },
            "L": [
                event.type
                for event in events
                if isinstance(event, AgentSessionEvent.AgentSettled)
            ],
            "T": {
                "cancel": stop_reasons[1],
                "providerFailure": stop_reasons[0],
                "reuse": stop_reasons[2],
            },
            "E": {
                "busyExtraRequests": after_busy - before_busy,
                "cancelledRequests": transport.cancelled_requests,
                "providerRequests": len(transport.requests),
            },
            "C": {
                "historyStopReasons": stop_reasons,
                "idle": session.isIdle,
            },
        }
        await session.dispose()
        return result


class _AutomaticCompactionTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = len(self.requests)
        if index == 1:
            return httpx.Response(
                400,
                json={"error": {"code": "context_length_exceeded"}},
                request=request,
            )
        text = "automatic checkpoint" if index == 2 else "continued after overflow"
        return httpx.Response(
            200,
            content=_text_sse(text),
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )


async def _run_branch_and_fork(root: Path) -> dict[str, object]:
    workspace = root / "source-project"
    _write_repository(workspace)
    manager = SessionManager.create(
        os.fspath(workspace),
        os.fspath(root / "source-sessions"),
        NewSessionOptions(id="tree-source"),
    )
    seed_transport = _ScriptedTransport(
        _text_sse("first answer"), _text_sse("abandoned answer")
    )
    with _use_transport(seed_transport):
        seed = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace), sessionManager=manager
                )
            )
        ).session
        await seed.prompt("first path")
        await seed.prompt("old continuation")
        await seed.dispose()

    message_entries = [
        entry for entry in manager.getEntries() if isinstance(entry, SessionMessageEntry)
    ]
    first_assistant = next(
        entry
        for entry in message_entries
        if isinstance(entry.message, AssistantMessage)
    )
    source_file = manager.getSessionFile()
    assert source_file is not None
    source_prefix = Path(source_file).read_bytes()
    manager.branch(first_assistant.id)

    branch_transport = _ScriptedTransport(_text_sse("new branch answer"))
    with _use_transport(branch_transport):
        branch = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace), sessionManager=manager
                )
            )
        ).session
        await branch.prompt("new continuation")
        await branch.dispose()

    source_after_branch = Path(source_file).read_bytes()
    source_entries = manager.getEntries()
    source_leaf = manager.getLeafId()
    target_workspace = root / "fork-project"
    target_workspace.mkdir()
    forked = SessionManager.forkFrom(
        source_file,
        os.fspath(target_workspace),
        os.fspath(root / "fork-sessions"),
        NewSessionOptions(id="tree-fork"),
    )
    fork_preserved = forked.getEntries() == source_entries
    source_before_fork_prompt = Path(source_file).read_bytes()
    fork_transport = _ScriptedTransport(_text_sse("fork answer"))
    with _use_transport(fork_transport):
        fork_session = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(target_workspace), sessionManager=forked
                )
            )
        ).session
        await fork_session.prompt("fork continuation")
        await fork_session.dispose()

    return {
        "branchChildren": len(manager.getChildren(first_assistant.id)),
        "divergentLeaves": source_leaf != forked.getLeafId(),
        "forkIndependentlyAppended": len(forked.getEntries())
        == len(source_entries) + 2,
        "forkSourceEntriesPreserved": fork_preserved,
        "sourcePrefixPreserved": source_after_branch.startswith(source_prefix),
        "sourceUnchangedAfterFork": Path(source_file).read_bytes()
        == source_before_fork_prompt,
    }


async def _run_compactions(root: Path) -> dict[str, object]:
    root.mkdir(parents=True)
    manual_workspace = root / "manual-project"
    manual_workspace.mkdir()
    manual_manager = SessionManager.create(
        os.fspath(manual_workspace),
        os.fspath(root / "manual-sessions"),
        NewSessionOptions(id="manual-compaction"),
    )
    manual_manager.appendMessage(UserMessage(content="old request", timestamp=1))
    manual_manager.appendMessage(fauxAssistantMessage("old answer"))
    kept_id = manual_manager.appendMessage(
        UserMessage(content="x" * 80_000, timestamp=2)
    )
    manual_manager.appendMessage(fauxAssistantMessage("recent answer"))
    manual_before = len(manual_manager.getEntries())
    manual_transport = _ScriptedTransport(_text_sse("manual checkpoint"))
    with _use_transport(manual_transport):
        manual = (
            await createAgentSession(
                CreateAgentSessionOptions(sessionManager=manual_manager)
            )
        ).session
        manual_events: list[AgentSessionEvent] = []
        manual.subscribe(manual_events.append)
        manual_result = await manual.compact("preserve the verified outcome")
        manual_context = manual.messages == manual_manager.buildSessionContext().messages
        manual_retained = (
            len(manual_manager.getEntries()) == manual_before + 1
            and manual_result.firstKeptEntryId == kept_id
        )
        manual_file = manual.sessionFile
        assert manual_file is not None
        await manual.dispose()
    recovered_manual = SessionManager.open(manual_file)
    recovered_compacted_context = (
        recovered_manual.buildSessionContext() == manual_manager.buildSessionContext()
    )

    threshold_manager = SessionManager.inMemory(os.fspath(root / "threshold"))
    threshold_manager.appendMessage(UserMessage(content="old request", timestamp=1))
    threshold_manager.appendMessage(fauxAssistantMessage("old answer"))
    threshold_manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
    threshold_manager.appendMessage(fauxAssistantMessage("recent answer"))
    threshold_before = len(threshold_manager.getEntries())
    threshold_transport = _ScriptedTransport(
        _text_sse("threshold source", prompt_tokens=114_999, completion_tokens=1),
        _text_sse("threshold checkpoint"),
    )
    with _use_transport(threshold_transport):
        threshold = (
            await createAgentSession(
                CreateAgentSessionOptions(sessionManager=threshold_manager)
            )
        ).session
        threshold_events: list[AgentSessionEvent] = []
        threshold.subscribe(threshold_events.append)
        await threshold.prompt("trigger threshold compaction")
        threshold_context = (
            threshold.messages == threshold_manager.buildSessionContext().messages
        )
        threshold_retained = (
            len(threshold_manager.getEntries()) == threshold_before + 3
        )
        await threshold.dispose()

    automatic_manager = SessionManager.inMemory(os.fspath(root / "automatic"))
    automatic_manager.appendMessage(UserMessage(content="old request", timestamp=1))
    automatic_manager.appendMessage(fauxAssistantMessage("old answer"))
    automatic_manager.appendMessage(UserMessage(content="x" * 80_000, timestamp=2))
    automatic_manager.appendMessage(fauxAssistantMessage("recent answer"))
    automatic_before = len(automatic_manager.getEntries())
    automatic_transport = _AutomaticCompactionTransport()
    with _use_transport(automatic_transport):
        automatic = (
            await createAgentSession(
                CreateAgentSessionOptions(sessionManager=automatic_manager)
            )
        ).session
        automatic_events: list[AgentSessionEvent] = []
        automatic.subscribe(automatic_events.append)
        await automatic.prompt("continue after overflow")
        will_retry = [
            event.willRetry
            for event in automatic_events
            if isinstance(event, AgentSessionEvent.AgentEnd)
        ]
        automatic_context = (
            isinstance(automatic.messages[0], UserMessage)
            and "automatic checkpoint" in str(automatic.messages[0].content)
            and any(
                isinstance(message, AssistantMessage)
                and message.stopReason == "error"
                for message in automatic_manager.buildSessionContext().messages
            )
            and all(
                not isinstance(message, AssistantMessage)
                or message.stopReason != "error"
                for message in automatic.messages
            )
        )
        automatic_retained = len(automatic_manager.getEntries()) == automatic_before + 4
        await automatic.dispose()

    return {
        "automaticRequestCount": len(automatic_transport.requests),
        "automaticReasons": [
            event.reason
            for event in (*threshold_events, *automatic_events)
            if isinstance(event, AgentSessionEvent.CompactionStart)
        ],
        "contextsRebuilt": manual_context and threshold_context and automatic_context,
        "explicitEvents": [event.type for event in manual_events],
        "overflowContinuationCount": sum(will_retry),
        "recoveredCompactedContext": recovered_compacted_context,
        "thresholdRequestCount": len(threshold_transport.requests),
        "treesRetained": manual_retained and threshold_retained and automatic_retained,
        "willRetry": will_retry,
    }


async def _run_memory_and_skill(root: Path) -> dict[str, object]:
    memory_workspace = root / "memory-project"
    memory_workspace.mkdir(parents=True)
    discovery_dir = root / "discovery"
    files_before_memory = set(root.rglob("*"))
    memory_manager = SessionManager.inMemory(
        os.fspath(memory_workspace), NewSessionOptions(id="memory-only")
    )
    memory_transport = _ScriptedTransport(_text_sse("memory answer"))
    with _use_transport(memory_transport):
        memory = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(memory_workspace), sessionManager=memory_manager
                )
            )
        ).session
        await memory.prompt("remember this only in memory")
        memory_entries = len(memory_manager.getEntries())
        memory_file = memory_manager.getSessionFile()
        await memory.dispose()
    discovered = await SessionManager.list(
        os.fspath(memory_workspace), os.fspath(discovery_dir)
    )
    files_after_memory = set(root.rglob("*"))
    public_parameters = {
        *inspect.signature(SessionManager.inMemory).parameters,
        *inspect.signature(CreateAgentSessionOptions).parameters,
    }

    skill_workspace = root / "skill-project"
    skill_workspace.mkdir(parents=True)
    skill_dir = skill_workspace / ".omh" / "skills" / "journey"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: journey\n"
        "description: Require the journey marker\n"
        "---\n\n"
        "Include SKILL_MARKER in the response.\n",
        encoding="utf-8",
    )
    untrusted = (
        await createAgentSession(
            CreateAgentSessionOptions(
                cwd=os.fspath(skill_workspace),
                sessionManager=SessionManager.inMemory(os.fspath(skill_workspace)),
                projectTrusted=False,
            )
        )
    ).session
    untrusted_has_no_skill = "Require the journey marker" not in untrusted.systemPrompt
    await untrusted.dispose()

    skill_transport = _ScriptedTransport(_text_sse("SKILL_MARKER observed"))
    with _use_transport(skill_transport):
        trusted = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(skill_workspace),
                    sessionManager=SessionManager.inMemory(os.fspath(skill_workspace)),
                    projectTrusted=True,
                )
            )
        ).session
        await trusted.prompt("/skill:journey")
        request = json.loads(skill_transport.requests[0].content)
        user_content = request["messages"][-1]["content"]
        final = trusted.messages[-1]
        skill_influenced = (
            "Include SKILL_MARKER" in user_content
            and isinstance(final, AssistantMessage)
            and isinstance(final.content[0], TextContent)
            and "SKILL_MARKER" in final.content[0].text
        )
        await trusted.dispose()

    return {
        "arbitraryPersistenceAdapter": any(
            "adapter" in parameter.lower() for parameter in public_parameters
        ),
        "inMemoryDiscovered": any(item.id == "memory-only" for item in discovered),
        "inMemoryEntries": memory_entries,
        "inMemoryFile": memory_file,
        "inMemoryFilesystemEffects": files_after_memory != files_before_memory,
        "skillInfluencedPrompt": skill_influenced,
        "untrustedCoreHasNoSkill": untrusted_has_no_skill,
    }


@_isolated_api_key("omh-conformance-canary")
async def run_session_topology(root: Path) -> dict[str, object]:
    topology = await _run_branch_and_fork(root / "topology")
    compaction = await _run_compactions(root / "compaction")
    memory_skill = await _run_memory_and_skill(root / "modes")
    return {
        "A": {
            "automaticCompaction": "admitted",
            "branch": "admitted",
            "explicitCompaction": "admitted",
            "fork": "admitted",
            "inMemory": "admitted",
            "skill": "trusted_only",
        },
        "L": {
            "automatic": compaction["willRetry"],
            "automaticReasons": compaction["automaticReasons"],
            "explicit": compaction["explicitEvents"],
        },
        "T": {
            "branchChildren": topology["branchChildren"],
            "compactionContextsRebuilt": compaction["contextsRebuilt"],
            "compactionTreesRetained": compaction["treesRetained"],
            "forkSourceEntriesPreserved": topology["forkSourceEntriesPreserved"],
            "recoveredCompactedContext": compaction["recoveredCompactedContext"],
            "skillInfluencedPrompt": memory_skill["skillInfluencedPrompt"],
        },
        "E": {
            "automaticRequestCount": compaction["automaticRequestCount"],
            "inMemoryDiscovered": memory_skill["inMemoryDiscovered"],
            "inMemoryFile": memory_skill["inMemoryFile"],
            "inMemoryFilesystemEffects": memory_skill[
                "inMemoryFilesystemEffects"
            ],
            "overflowContinuationCount": compaction["overflowContinuationCount"],
            "sourcePrefixPreserved": topology["sourcePrefixPreserved"],
            "sourceUnchangedAfterFork": topology["sourceUnchangedAfterFork"],
            "thresholdRequestCount": compaction["thresholdRequestCount"],
        },
        "C": {
            "arbitraryPersistenceAdapter": memory_skill[
                "arbitraryPersistenceAdapter"
            ],
            "divergentLeaves": topology["divergentLeaves"],
            "forkIndependentlyAppended": topology["forkIndependentlyAppended"],
            "inMemoryEntries": memory_skill["inMemoryEntries"],
            "untrustedCoreHasNoSkill": memory_skill["untrustedCoreHasNoSkill"],
        },
    }


@_isolated_api_key("initial-key")
async def run_recovery_continuity(root: Path) -> dict[str, object]:
    workspace = root / "project"
    _write_repository(workspace)
    manager = SessionManager.create(
        os.fspath(workspace),
        os.fspath(root / "sessions"),
        NewSessionOptions(id="recovery-source"),
    )
    initial_transport = _ScriptedTransport(_text_sse("initial completion"))
    with _use_transport(initial_transport):
        initial = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    sessionManager=manager,
                    projectTrusted=False,
                )
            )
        ).session
        await initial.prompt("record the initial conversation")
        session_file = initial.sessionFile
        assert session_file is not None
        manager.appendMessage(
            UserMessage(content="interrupted request: do not replay", timestamp=3)
        )
        prefix_ids = tuple(entry.id for entry in manager.getEntries())
        persisted_prefix = Path(session_file).read_bytes()
        await initial.dispose()

    with Path(session_file).open("ab") as output:
        output.write(b'{"partial":\n')
    (workspace / "INSTRUCTIONS.md").write_text(
        "CURRENT_INSTRUCTIONS: inspect only; do not replay prior effects.\n",
        encoding="utf-8",
    )
    discovered = await SessionManager.list(
        os.fspath(workspace), manager.getSessionDir()
    )
    selected_info = next(
        item for item in discovered if item.id == "recovery-source"
    )
    reopened_manager = SessionManager.open(selected_info.path)
    recovered_prefix = tuple(entry.id for entry in reopened_manager.getEntries())
    os.environ["DEEPSEEK_API_KEY"] = "rebound-key"
    recovery_transport = _ScriptedTransport(
        _tool_call_sse("read-current", "read", {"path": "INSTRUCTIONS.md"}),
        _text_sse(
            "Read CURRENT_INSTRUCTIONS. The correction completed without replaying "
            "the interrupted effect."
        ),
    )
    with _use_transport(recovery_transport):
        recovered = (
            await createAgentSession(
                CreateAgentSessionOptions(
                    cwd=os.fspath(workspace),
                    sessionManager=reopened_manager,
                    projectTrusted=False,
                )
            )
        ).session
        events: list[AgentSessionEvent] = []
        recovered.subscribe(events.append)
        requests_before_prompt = len(recovery_transport.requests)
        await recovered.prompt("continue with the current repository instructions")
        final = recovered.messages[-1]
        assert isinstance(final, AssistantMessage)
        result_messages = [
            message
            for message in recovered.messages
            if isinstance(message, ToolResultMessage)
        ]
        refreshed = any(
            isinstance(content, TextContent)
            and "CURRENT_INSTRUCTIONS" in content.text
            for message in result_messages
            for content in message.content
        )
        active_roles = [message.role for message in recovered.messages]
        selected_lifecycle = [
            selected_event
            for event in events
            if (selected_event := _selected_event(event)) is not None
            and selected_event.startswith("tool_execution_")
        ]
        await recovered.dispose()
        before_disposed_prompt = len(recovery_transport.requests)
        disposed_code = ""
        try:
            await recovered.prompt("must reject after disposal")
        except LifecycleError as error:
            disposed_code = error.code
        disposed = (
            final.stopReason == "stop"
            and disposed_code == "disposed"
            and len(recovery_transport.requests) == before_disposed_prompt
        )

    final_reopen = SessionManager.open(session_file)
    authorization = [
        request.headers.get("authorization") for request in recovery_transport.requests
    ]
    file_bytes = Path(session_file).read_bytes()
    return {
        "A": {
            "openedById": selected_info.id,
            "openedByPath": selected_info.path == session_file,
            "projectTrusted": False,
        },
        "L": selected_lifecycle,
        "T": {
            "activeRoles": active_roles,
            "finalStopReason": final.stopReason,
            "treeEntries": len(final_reopen.getEntries()),
        },
        "E": {
            "authenticationRebound": authorization
            == ["Bearer rebound-key", "Bearer rebound-key"],
            "instructionsRefreshed": refreshed,
            "providerRequests": len(recovery_transport.requests),
            "replayedInterruptedEffects": requests_before_prompt != 0,
        },
        "C": {
            "completionDisposed": disposed,
            "malformedLineRetained": b'{"partial":\n' in file_bytes,
            "parseablePrefixPreserved": (
                recovered_prefix == prefix_ids
                and file_bytes.startswith(persisted_prefix)
                and tuple(entry.id for entry in final_reopen.getEntries())[: len(prefix_ids)]
                == prefix_ids
            ),
        },
    }


async def main() -> None:
    with tempfile.TemporaryDirectory() as raw_root:
        actual = {
            "reference.programmatic-product-journey": await run_golden_journey(
                Path(raw_root) / "golden"
            ),
            "reference.require-named-check-for-success": (
                await run_verification_matrix(Path(raw_root) / "verification")
            ),
            "reference.programmatic-failure-boundaries": (
                await run_failure_boundaries(Path(raw_root) / "boundaries")
            ),
            "reference.programmatic-session-topology": (
                await run_session_topology(Path(raw_root) / "topology")
            ),
            "reference.programmatic-recovery-continuity": (
                await run_recovery_continuity(Path(raw_root) / "recovery")
            ),
        }
        print(json.dumps(actual, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    asyncio.run(main())
