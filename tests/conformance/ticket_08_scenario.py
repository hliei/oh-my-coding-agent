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
    runAgentLoop,
)
from oh_my_llm import (
    AbortSignal,
    AssistantMessageEvent,
    AssistantMessageTextDeltaEvent,
    LifecycleError,
    Model,
    UserMessage,
    createModels,
    fauxAssistantMessage,
    fauxProvider,
)


async def _clean_cancellation() -> dict[str, object]:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    faux.setResponses((fauxAssistantMessage("confirmed"),))
    models = createModels()
    models.setProvider(faux.provider)
    blocked = asyncio.Event()
    cleaned = asyncio.Event()
    event_types: list[str] = []

    async def stream_fn(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del options, signal
        try:
            async for event in models.streamSimple(model, context, None):
                yield event
                if isinstance(event, AssistantMessageTextDeltaEvent):
                    blocked.set()
                    await asyncio.Event().wait()
        finally:
            cleaned.set()

    agent = Agent(
        AgentOptions(initialState=AgentState(model=model), streamFn=stream_fn)
    )
    agent.subscribe(lambda event, signal: event_types.append(event.type))
    operation = asyncio.create_task(agent.prompt("go"))
    await blocked.wait()
    agent.abort()
    await operation
    terminal = agent.state.messages[-1]
    return {
        "A": "admitted",
        "L": {
            "cleanupBeforeEnd": cleaned.is_set(),
            "agentEndCount": event_types.count("agent_end"),
        },
        "T": {
            "stopReason": getattr(terminal, "stopReason", None),
            "errorMessage": getattr(terminal, "errorMessage", None),
        },
        "E": {"modelCalls": faux.state.callCount},
        "C": {
            "idle": agent.state.isStreaming is False,
            "signalCleared": agent.signal is None,
        },
    }


async def _fail_closed_carriers() -> dict[str, object]:
    listener_faux = fauxProvider()
    listener_model = listener_faux.getModel()
    assert listener_model is not None
    listener_attempts: list[str] = []

    async def never_stream(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del model, context, options, signal
        if False:
            yield

    listener_agent = Agent(
        AgentOptions(
            initialState=AgentState(model=listener_model),
            streamFn=never_stream,
        )
    )

    def first_listener(event: AgentEvent, signal: AbortSignal) -> None:
        del event, signal
        listener_attempts.append("first")
        raise RuntimeError("listener")

    def second_listener(event: AgentEvent, signal: AbortSignal) -> None:
        del event, signal
        listener_attempts.append("second")

    listener_agent.subscribe(first_listener)
    listener_agent.subscribe(second_listener)
    listener_code: str | None = None
    try:
        await listener_agent.prompt("listener")
    except LifecycleError as error:
        listener_code = error.code

    sink_faux = fauxProvider()
    sink_model = sink_faux.getModel()
    assert sink_model is not None
    sink_events: list[str] = []

    def failing_sink(event: AgentEvent) -> None:
        sink_events.append(event.type)
        raise RuntimeError("sink")

    sink_code: str | None = None
    try:
        await runAgentLoop(
            (UserMessage(content="sink", timestamp=1),),
            AgentContext(systemPrompt="", messages=()),
            AgentLoopConfig(model=sink_model),
            failing_sink,
            never_stream,
        )
    except LifecycleError as error:
        sink_code = error.code

    cleanup_faux = fauxProvider()
    cleanup_model = cleanup_faux.getModel()
    assert cleanup_model is not None
    cleanup_started = asyncio.Event()
    cleanup_canary = RuntimeError("cleanup")

    async def broken_cleanup(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del model, context, options, signal
        cleanup_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise cleanup_canary
        if False:
            yield

    cleanup_agent = Agent(
        AgentOptions(
            initialState=AgentState(model=cleanup_model),
            streamFn=broken_cleanup,
        )
    )
    cleanup_operation = asyncio.create_task(cleanup_agent.prompt("cleanup"))
    await cleanup_started.wait()
    cleanup_agent.abort()
    cleanup_code: str | None = None
    try:
        await cleanup_operation
    except LifecycleError as error:
        cleanup_code = error.code

    model_faux = fauxProvider()
    model = model_faux.getModel()
    assert model is not None

    async def ordinary_model_error(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del model, context, options, signal
        raise RuntimeError("private model error")
        if False:
            yield

    model_agent = Agent(
        AgentOptions(
            initialState=AgentState(model=model),
            streamFn=ordinary_model_error,
        )
    )
    await model_agent.prompt("model")
    model_terminal = model_agent.state.messages[-1]

    unsettled_faux = fauxProvider()
    unsettled_model = unsettled_faux.getModel()
    assert unsettled_model is not None
    unsettled_started = asyncio.Event()
    unsettled_cleanup = asyncio.Event()
    allow_settlement = asyncio.Event()

    async def unsettled_then_clean(
        model: Model,
        context: Any,
        options: object | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del model, context, options, signal
        unsettled_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            unsettled_cleanup.set()
            await allow_settlement.wait()
        if False:
            yield

    unsettled_agent = Agent(
        AgentOptions(
            initialState=AgentState(model=unsettled_model),
            streamFn=unsettled_then_clean,
        )
    )
    unsettled_operation = asyncio.create_task(unsettled_agent.prompt("held"))
    await unsettled_started.wait()
    unsettled_agent.abort()
    await unsettled_cleanup.wait()
    busy_before_release = unsettled_agent.state.isStreaming
    allow_settlement.set()
    await unsettled_operation

    return {
        "A": "admitted",
        "L": {
            "listenerAttempts": listener_attempts,
            "sinkAgentEnd": "agent_end" in sink_events,
        },
        "T": {
            "listener": listener_code,
            "eventSink": sink_code,
            "cleanup": cleanup_code,
            "model": getattr(model_terminal, "stopReason", None),
        },
        "E": {"listenerModelCalls": listener_faux.state.callCount},
        "C": {
            "listenerIdle": listener_agent.state.isStreaming is False,
            "cleanupIdle": cleanup_agent.state.isStreaming is False,
            "unconfirmedBusy": busy_before_release,
            "unconfirmedIdleAfterSettlement": (
                unsettled_agent.state.isStreaming is False
            ),
        },
    }


async def main() -> None:
    print(
        json.dumps(
            {
                "reference.agent-clean-cancellation": await _clean_cancellation(),
                "reference.agent-fail-closed-lifecycle": (
                    await _fail_closed_carriers()
                ),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
