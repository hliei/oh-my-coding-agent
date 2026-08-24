from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
from typing import Any

from oh_my_core import (
    Agent,
    AgentContext,
    AgentEvent,
    AgentLoopConfig,
    AgentOptions,
    AgentState,
    StreamFn,
    runAgentLoop,
)
from oh_my_llm import (
    AbortSignal,
    AssistantMessageEvent,
    LifecycleError,
    Model,
    SimpleStreamOptions,
    UserMessage,
    createModels,
    fauxAssistantMessage,
    fauxProvider,
)


def _stream_fn(models: Any) -> StreamFn:
    async def stream_fn(
        model: Model,
        context: Any,
        options: SimpleStreamOptions | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del signal
        async for event in models.streamSimple(model, context, options):
            yield event

    return stream_fn


def _public_surface() -> dict[str, object]:
    return {
        "A": "admitted",
        "L": [],
        "T": {
            "exported": ["Agent", "AgentOptions", "AgentState"],
            "members": sorted(name for name in dir(Agent) if not name.startswith("_")),
            "hasContinueAlias": hasattr(Agent, "continue"),
            "continueIsCallable": callable(getattr(Agent, "continue_")),
        },
        "E": [],
        "C": "not_applicable",
    }


async def _admission() -> dict[str, object]:
    idle_faux = fauxProvider()
    idle_model = idle_faux.getModel()
    assert idle_model is not None
    idle_models = createModels()
    idle_models.setProvider(idle_faux.provider)
    idle = Agent(
        AgentOptions(
            initialState=AgentState(model=idle_model),
            streamFn=_stream_fn(idle_models),
        )
    )
    busy_faux = fauxProvider()
    busy_model = busy_faux.getModel()
    assert busy_model is not None
    busy_faux.setResponses((fauxAssistantMessage("busy-ok"),))
    busy_models = createModels()
    busy_models.setProvider(busy_faux.provider)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"count": 0}

    async def gated(
        model: Model,
        context: Any,
        options: SimpleStreamOptions | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del signal
        calls["count"] += 1
        started.set()
        await release.wait()
        async for event in busy_models.streamSimple(model, context, options):
            yield event

    busy = Agent(
        AgentOptions(initialState=AgentState(model=busy_model), streamFn=gated)
    )
    observed: dict[str, str | None] = {
        "emptySequence": None,
        "assistantTail": None,
        "carrier": None,
        "busy": None,
    }
    try:
        await idle.prompt(())
    except ValueError:
        observed["emptySequence"] = "value_error"
    try:
        await idle.prompt(fauxAssistantMessage("assistant"))
    except ValueError:
        observed["assistantTail"] = "value_error"
    try:
        await idle.prompt(123)  # type: ignore[arg-type]
    except TypeError:
        observed["carrier"] = "type_error"
    first = asyncio.create_task(busy.prompt("one"))
    await started.wait()
    try:
        await busy.prompt("two")
    except LifecycleError as error:
        observed["busy"] = error.code
    unchanged = idle.state.messages == () and idle.state.isStreaming is False
    idle_calls = idle_faux.state.callCount
    release.set()
    await first
    return {
        "A": observed,
        "L": [],
        "T": {"unchangedIdleHistory": unchanged, "busyStreamCalls": calls["count"]},
        "E": {"idleProviderCalls": idle_calls},
        "C": "later_busy_run_settled",
    }


async def _mutation_and_reset() -> dict[str, object]:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    faux.setResponses((fauxAssistantMessage("done"),))
    models = createModels()
    models.setProvider(faux.provider)
    seed = AgentState(model=model, systemPrompt="sys")
    agent = Agent(AgentOptions(initialState=seed, streamFn=_stream_fn(models)))
    same_identity = agent.state is agent.state
    seed_isolated = agent.state is not seed
    agent.state.systemPrompt = "kept"
    await agent.prompt("hello")
    had_history = len(agent.state.messages) == 2
    agent.reset()
    busy_faux = fauxProvider()
    busy_model = busy_faux.getModel()
    assert busy_model is not None
    busy_faux.setResponses((fauxAssistantMessage("busy"),))
    busy_models = createModels()
    busy_models.setProvider(busy_faux.provider)
    started = asyncio.Event()
    release = asyncio.Event()

    async def gated(
        model: Model,
        context: Any,
        options: SimpleStreamOptions | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del signal
        started.set()
        await release.wait()
        async for event in busy_models.streamSimple(model, context, options):
            yield event

    busy_agent = Agent(
        AgentOptions(
            initialState=AgentState(model=busy_model, systemPrompt="keep"),
            streamFn=gated,
        )
    )
    keep_prompt = busy_agent.state.systemPrompt
    task = asyncio.create_task(busy_agent.prompt("go"))
    await started.wait()
    busy_assign: str | None = None
    try:
        busy_agent.state.systemPrompt = "mutated"
        busy_assign = "mutated"
    except LifecycleError as error:
        busy_assign = error.code
    busy_unchanged = busy_agent.state.systemPrompt == keep_prompt
    release.set()
    await task
    return {
        "A": "idle_reset_admitted",
        "L": [],
        "T": {
            "sameStateIdentity": same_identity,
            "seedIsolated": seed_isolated,
            "hadHistory": had_history,
            "messagesAfterReset": len(agent.state.messages),
            "promptPreserved": agent.state.systemPrompt,
            "errorCleared": agent.state.errorMessage is None,
            "busyAssign": busy_assign,
            "busyUnchanged": busy_unchanged,
        },
        "E": {"providerCalls": faux.state.callCount},
        "C": "reuse_ready",
    }


async def _listeners_and_identity() -> dict[str, object]:
    prompt = UserMessage(content="same", timestamp=11)
    response = fauxAssistantMessage("shared")
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    faux.setResponses((response, response))
    models = createModels()
    models.setProvider(faux.provider)
    stream_fn = _stream_fn(models)
    low_events: list[str] = []
    high_events: list[str] = []
    streaming_at_end = False
    agent = Agent(
        AgentOptions(initialState=AgentState(model=model), streamFn=stream_fn)
    )
    agent.subscribe(lambda event, signal: high_events.append(event.type))

    def on_end(event: AgentEvent, signal: AbortSignal) -> None:
        del signal
        nonlocal streaming_at_end
        if event.type == "agent_end":
            streaming_at_end = agent.state.isStreaming is True

    agent.subscribe(on_end)
    result = await runAgentLoop(
        (prompt,),
        AgentContext(systemPrompt="", messages=()),
        AgentLoopConfig(model=model),
        lambda event: low_events.append(event.type),
        stream_fn,
    )
    await agent.prompt(prompt)
    suffix = agent.state.messages[-len(result) :]
    return {
        "A": "admitted",
        "L": {
            "streamingDuringAgentEnd": streaming_at_end,
            "idleAfterPrompt": agent.state.isStreaming is False,
        },
        "T": {
            "suffixEqualsLowLevel": suffix == result,
            "eventTypesEqual": low_events == high_events,
            "roles": [message.role for message in suffix],
        },
        "E": {"providerCalls": faux.state.callCount},
        "C": "agent_history_is_reduction_suffix",
    }


async def _distinct_listeners() -> dict[str, object]:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    faux.setResponses((fauxAssistantMessage("one"), fauxAssistantMessage("two")))
    models = createModels()
    models.setProvider(faux.provider)
    agent = Agent(
        AgentOptions(initialState=AgentState(model=model), streamFn=_stream_fn(models))
    )
    order: list[str] = []

    def mark(label: str) -> Any:
        def listen(event: AgentEvent, signal: AbortSignal) -> None:
            del signal
            if event.type == "agent_start":
                order.append(label)

        return listen

    shared = mark("shared")
    first = agent.subscribe(shared)
    second = agent.subscribe(shared)
    late = mark("late")

    def mutator(event: AgentEvent, signal: AbortSignal) -> None:
        del signal
        if event.type == "agent_start":
            order.append("mutator")
            agent.subscribe(late)
            second()

    agent.subscribe(mutator)
    await agent.prompt("first")
    first()
    first()
    await agent.prompt("second")
    return {
        "A": "duplicate_callable_registered_twice",
        "L": {"order": order},
        "T": [],
        "E": {"providerCalls": faux.state.callCount},
        "C": "unsubscribe_removed_only_own_record",
    }


async def _model_error() -> dict[str, object]:
    secret = "SECRET_CANARY"
    calls = {"count": 0}
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    recovered = fauxAssistantMessage("recovered")
    faux.setResponses((recovered,))
    models = createModels()
    models.setProvider(faux.provider)

    async def failing_then_ok(
        model: Model,
        context: Any,
        options: SimpleStreamOptions | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del signal
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError(secret)
        async for event in models.streamSimple(model, context, options):
            yield event

    agent = Agent(
        AgentOptions(initialState=AgentState(model=model), streamFn=failing_then_ok)
    )
    await agent.prompt("fail")
    error = agent.state.messages[-1]
    first_error = agent.state.errorMessage
    await agent.prompt("again")
    return {
        "A": "ordinary_model_error_admitted",
        "L": [],
        "T": {
            "stopReason": getattr(error, "stopReason", None),
            "errorMessage": getattr(error, "errorMessage", None),
            "secretLeaked": secret in str(getattr(error, "errorMessage", "")),
            "stateErrorAfterFailure": first_error,
            "recovered": agent.state.messages[-1] == recovered,
            "stateErrorAfterReuse": agent.state.errorMessage,
        },
        "E": {"streamCalls": calls["count"]},
        "C": "fresh_run_after_error",
    }


async def main() -> None:
    print(
        json.dumps(
            {
                "reference.agent-public-surface": _public_surface(),
                "reference.strict-agent-run-admission": await _admission(),
                "reference.owned-agent-state-mutation": await _mutation_and_reset(),
                "reference.agent-low-level-semantic-identity": await _listeners_and_identity(),
                "reference.distinct-listener-registrations": await _distinct_listeners(),
                "reference.agent-ordinary-model-error": await _model_error(),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
