from __future__ import annotations

import asyncio
import json
from typing import Any

import oh_my_core
import oh_my_llm
from oh_my_core import AgentContext, AgentEvent, AgentLoopConfig
from oh_my_llm import UserMessage, createModels, fauxAssistantMessage, fauxProvider


async def _observe() -> dict[str, object]:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    responses = tuple(fauxAssistantMessage(text) for text in ("a", "b", "c", "d"))
    faux.setResponses(responses)
    models = createModels()
    models.setProvider(faux.provider)
    signals: list[oh_my_llm.AbortSignal] = []

    async def stream_fn(
        model: Any,
        context: Any,
        options: Any,
        signal: oh_my_llm.AbortSignal,
    ) -> Any:
        signals.append(signal)
        async for event in models.streamSimple(model, context, options):
            yield event

    prior = UserMessage(content="prior", timestamp=1)
    prompt = UserMessage(content="prompt", timestamp=2)
    context = AgentContext(systemPrompt="", messages=(prior,))
    messages_before = context.messages
    config = AgentLoopConfig(model=model)

    prompt_stream = oh_my_core.agentLoop((prompt,), context, config, stream_fn)
    continuation_stream = oh_my_core.agentLoopContinue(context, config, stream_fn)
    prompt_events: list[AgentEvent] = []
    continuation_events: list[AgentEvent] = []
    barrier_entered = asyncio.Event()
    release_barrier = asyncio.Event()

    async def barrier_sink(event: AgentEvent) -> None:
        prompt_events.append(event)
        if isinstance(event, AgentEvent.TurnStart):
            barrier_entered.set()
            await release_barrier.wait()

    prompt_operation = oh_my_core.runAgentLoop(
        (prompt,), context, config, barrier_sink, stream_fn
    )
    continuation_operation = oh_my_core.runAgentLoopContinue(
        context, config, continuation_events.append, stream_fn
    )
    lazy_before_activation = faux.state.callCount == 0

    first_result, second_result = await asyncio.gather(
        prompt_stream.result(), prompt_stream.result()
    )
    consumer = prompt_stream.__aiter__()
    try:
        prompt_stream.__aiter__()
    except oh_my_llm.LifecycleError as error:
        second_consumer_code = error.code
    else:
        raise AssertionError("second EventStream consumer was admitted")
    while True:
        try:
            prompt_events.append(await consumer.__anext__())
        except StopAsyncIteration:
            break

    continuation_result = await continuation_stream.result()
    continuation_stream_events = [event async for event in continuation_stream]

    prompt_task = asyncio.create_task(prompt_operation)
    await barrier_entered.wait()
    sink_blocked_model_effect = faux.state.callCount == 2
    release_barrier.set()
    prompt_result = await prompt_task
    awaited_continuation_result = await continuation_operation

    signal_waiter = asyncio.create_task(signals[0].wait())
    waiter_started = asyncio.Event()
    asyncio.get_running_loop().call_soon(waiter_started.set)
    await waiter_started.wait()
    signal_waiter.cancel()
    try:
        await signal_waiter
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("retained normal signal waiter unexpectedly completed")

    sink_canary = RuntimeError("installed sink canary")

    def failing_sink(event: AgentEvent) -> None:
        if isinstance(event, AgentEvent.TurnStart):
            raise sink_canary

    try:
        await oh_my_core.runAgentLoop((prompt,), context, config, failing_sink, stream_fn)
    except oh_my_llm.LifecycleError as error:
        sink_failure = error
    else:
        raise AssertionError("failed sink returned a partial result")

    cancellation_started = asyncio.Event()
    cancellation_cleaned = asyncio.Event()
    cancellation_signals: list[oh_my_llm.AbortSignal] = []

    async def blocked_stream(
        model: Any,
        context: Any,
        options: Any,
        signal: oh_my_llm.AbortSignal,
    ) -> Any:
        del model, context, options
        cancellation_signals.append(signal)
        cancellation_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancellation_cleaned.set()
        if False:
            yield None

    cancelled = asyncio.create_task(
        oh_my_core.runAgentLoop(
            (prompt,), context, config, lambda event: None, blocked_stream
        )
    )
    await cancellation_started.wait()
    cancelled.cancel()
    try:
        await cancelled
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancelled loop returned normally")
    assert cancellation_cleaned.is_set()
    assert cancellation_signals[0].aborted

    assert isinstance(prompt_events[-1], AgentEvent.AgentEnd)
    prompt_stream_terminal = prompt_events[10]
    assert isinstance(prompt_stream_terminal, AgentEvent.AgentEnd)
    assert isinstance(continuation_events[-1], AgentEvent.AgentEnd)
    assert isinstance(continuation_stream_events[-1], AgentEvent.AgentEnd)

    return {
        "reference.low-level-loop-carriers": {
            "A": "four_public_entries",
            "L": {
                "promptTerminal": prompt_events[-1].type,
                "continuationTerminal": continuation_events[-1].type,
            },
            "T": {
                "streamPromptRoles": [message.role for message in first_result],
                "streamContinuationRoles": [
                    message.role for message in continuation_result
                ],
                "sinkPromptRoles": [message.role for message in prompt_result],
                "sinkContinuationRoles": [
                    message.role for message in awaited_continuation_result
                ],
            },
            "E": {"modelCalls": faux.state.callCount},
            "C": {"lazyBeforeActivation": lazy_before_activation},
        },
        "reference.immutable-loop-context": {
            "A": "admitted",
            "L": [],
            "T": {"continuationRoles": [message.role for message in continuation_result]},
            "E": {"callerContextMutated": context.messages is not messages_before},
            "C": "input_identity_and_value_unchanged",
        },
        "reference.managed-event-stream": {
            "A": {"firstConsumer": "admitted", "secondConsumer": second_consumer_code},
            "L": [event.type for event in prompt_events[:11]],
            "T": {
                "resultObserversShareIdentity": first_result is second_result,
                "agentEndSharesIdentity": prompt_stream_terminal.messages is first_result,
            },
            "E": {"producerActivations": 1},
            "C": "late_consumer_drained_terminal_fifo",
        },
        "reference.readonly-abort-signal": {
            "A": "factory_produced_observer",
            "L": [],
            "T": {
                "sameRunIdentity": len({id(signal) for signal in signals}) == 4,
                "normalSignalsAborted": [signal.aborted for signal in signals],
                "cancelledWaiterLocal": not signals[0].aborted,
                "publicMembers": ["aborted", "wait"],
            },
            "E": [],
            "C": "retained_normal_signal_false",
        },
        "reference.awaited-event-sink": {
            "A": "sync_or_awaitable",
            "L": "each_event_barrier",
            "T": {"failureCode": sink_failure.code, "noPartialResult": True},
            "E": {
                "modelBlockedBehindSink": sink_blocked_model_effect,
                "failedSinkStartedModel": faux.state.callCount != 4,
            },
            "C": "owned_work_settled_before_raise",
        },
        "reference.closed-lifecycle-error-carrier": {
            "A": "closed_code",
            "L": [],
            "T": {
                "class": type(sink_failure).__name__,
                "code": sink_failure.code,
                "orderedCauseCount": len(sink_failure.causes),
                "standardCause": sink_failure.__cause__ is sink_canary,
            },
            "E": [],
            "C": "classification_without_message_parsing",
        },
        "reference.managed-run-cancellation": {
            "A": "owner_managed_request",
            "L": "aborted_only_after_clean_settlement",
            "T": "terminal_classification_irreversible_before_terminal_sink",
            "E": "no_new_effect_after_cutoff",
            "C": "owned_work_settled_before_return_or_reraise",
        },
    }


def main() -> None:
    assert oh_my_llm.EventStream is not None
    assert oh_my_llm.AbortSignal is not None
    assert not hasattr(oh_my_core, "EventStream")
    assert not hasattr(oh_my_core, "AbortSignal")
    assert {
        "agentLoop",
        "agentLoopContinue",
        "runAgentLoop",
        "runAgentLoopContinue",
    } <= set(oh_my_core.__all__)
    observations = asyncio.run(asyncio.wait_for(_observe(), timeout=10.0))
    print(json.dumps(observations, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
