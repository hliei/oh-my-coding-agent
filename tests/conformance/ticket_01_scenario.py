from __future__ import annotations

import asyncio
from importlib import metadata
import importlib.util
import inspect
import json
from typing import Any

import oh_my_coding_agent
import oh_my_core
import oh_my_llm
from oh_my_core import AgentContext, AgentEvent, AgentLoopConfig, runAgentLoop
from oh_my_llm import Context, UserMessage, createModels, fauxAssistantMessage, fauxProvider


def _is_importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


async def _complete_run() -> dict[str, object]:
    faux = fauxProvider()
    models = createModels()
    models.setProvider(faux.provider)
    model = models.getModel("faux", "faux-1")
    assert model is faux.getModel()
    assert model is not None

    response = fauxAssistantMessage("installed hello")
    faux.setResponses((response,))

    prior = UserMessage(content="prior", timestamp=1)
    prompt = UserMessage(content="hello", timestamp=2)
    context = AgentContext(systemPrompt="Be concise.", messages=(prior,))
    messages_before = context.messages
    events: list[AgentEvent] = []

    async def emit(event: AgentEvent) -> None:
        events.append(event)

    result = await runAgentLoop(
        (prompt,),
        context,
        AgentLoopConfig(model=model),
        emit,
        models.streamSimple,
    )

    assert tuple(event.type for event in events) == (
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "message_start",
        "message_update",
        "message_update",
        "message_update",
        "message_end",
        "turn_end",
        "agent_end",
    )
    assert isinstance(events[-1], AgentEvent.AgentEnd)
    assert type(events[-1]).__name__ == "AgentEnd"
    assert events[-1].messages is result
    assert result == (prompt, response)
    assert context.messages is messages_before
    assert context.messages == (prior,)
    assert faux.state.callCount == 1
    assert faux.getPendingResponseCount() == 0

    lifecycle: list[str] = []
    for event in events:
        if isinstance(event, (AgentEvent.MessageStart, AgentEvent.MessageEnd)):
            lifecycle.append(f"{event.type}:{event.message.role}")
        elif isinstance(event, AgentEvent.MessageUpdate):
            lifecycle.append(f"{event.type}:{event.assistantMessageEvent.type}")
        else:
            lifecycle.append(event.type)

    return {
        "reference.no-tool-run-trace": {
            "A": "admitted",
            "L": lifecycle,
            "T": {"classification": "ordinary", "roles": [item.role for item in result]},
            "E": {"providerCalls": faux.state.callCount},
            "C": "not_applicable",
        },
        "reference.run-result-identity": {
            "A": "admitted",
            "L": {"agentEndMessagesIsResult": events[-1].messages is result},
            "T": {"carrier": "tuple", "roles": [item.role for item in result]},
            "E": {
                "contextMessagesIdentityPreserved": context.messages is messages_before,
                "contextValueUnchanged": context.messages == (prior,),
            },
            "C": "not_applicable",
        },
    }


async def _reject_missing_dependencies_without_effects() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    prompt = UserMessage(content="hello", timestamp=2)
    context = AgentContext(systemPrompt=None, messages=())
    events: list[AgentEvent] = []

    async def emit(event: AgentEvent) -> None:
        events.append(event)

    try:
        AgentLoopConfig(model=None)  # type: ignore[arg-type]
    except TypeError:
        pass
    else:
        raise AssertionError("missing Model was admitted")

    try:
        await runAgentLoop(
            (prompt,),
            context,
            AgentLoopConfig(model=model),
            emit,
            None,  # type: ignore[arg-type]
        )
    except TypeError:
        pass
    else:
        raise AssertionError("missing stream dependency was admitted")

    assert events == []
    assert context.messages == ()
    assert faux.state.callCount == 0


def main() -> None:
    distribution = metadata.distribution("omh")
    python_range = distribution.metadata["Requires-Python"].replace(" ", "")
    assert set(python_range.split(",")) == {">=3.12", "<3.14"}
    assert {requirement.lower() for requirement in distribution.requires or ()} == {
        "httpx==0.28.1",
        "google-re2==1.1.20251105",
        "pyyaml==6.0.3",
    }

    assert inspect.signature(createModels).parameters == {}
    assert inspect.signature(fauxProvider).parameters == {}
    assert oh_my_llm.__all__
    assert oh_my_core.__all__
    assert oh_my_coding_agent.__all__ == ()
    assert not hasattr(oh_my_llm, "Agent")
    assert not hasattr(oh_my_core, "Model")
    assert not hasattr(oh_my_coding_agent, "Agent")
    excluded_llm_names = (
        "createFauxCore",
        "fauxThinking",
        "registerProvider",
        "registerFauxProvider",
        "stream",
        "complete",
    )
    for excluded_name in excluded_llm_names:
        assert not hasattr(oh_my_llm, excluded_name), excluded_name
    excluded_core_names = ("run_agent_loop", "EventStream", "AbortSignal")
    for excluded_name in excluded_core_names:
        assert not hasattr(oh_my_core, excluded_name), excluded_name

    excluded_modules = (
        "pi_ai",
        "pi_agent_core",
        "pi_coding_agent",
        "oh_my_llm.compat",
        "oh_my_llm.node",
        "oh_my_core.compat",
        "oh_my_core.rpc_entry",
        "oh_my_coding_agent.rpc_entry",
    )
    for excluded in excluded_modules:
        assert not _is_importable(excluded), excluded

    try:
        class UnknownAgentEvent(AgentEvent):
            pass
    except TypeError:
        pass
    else:
        raise AssertionError("AgentEvent admitted an external variant")

    actual: dict[str, Any] = {
        "reference.public-import-roots": {
            "A": "admitted",
            "L": [],
            "T": {
                "roots": ["oh_my_llm", "oh_my_core", "oh_my_coding_agent"],
                "distinct": len({id(oh_my_llm), id(oh_my_core), id(oh_my_coding_agent)}) == 3,
                "referenceIdentityLeak": False,
            },
            "E": [],
            "C": "not_applicable",
        },
        "reference.excluded-public-aliases": {
            "A": "rejected",
            "L": [],
            "T": {
                "absent": [
                    *excluded_llm_names,
                    *excluded_core_names,
                    *excluded_modules,
                ]
            },
            "E": [],
            "C": "not_applicable",
        },
    }
    actual.update(asyncio.run(_complete_run()))
    asyncio.run(_reject_missing_dependencies_without_effects())
    print(json.dumps(actual, sort_keys=True))


if __name__ == "__main__":
    main()
