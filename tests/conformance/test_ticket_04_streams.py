from __future__ import annotations

import asyncio
from collections.abc import Iterator
import inspect

import pytest

import oh_my_core
import oh_my_llm
from oh_my_core import AgentContext, AgentLoopConfig
from oh_my_llm import UserMessage, createModels, fauxAssistantMessage, fauxProvider


def test_abort_signal_is_factory_produced_read_only_and_waiter_local() -> None:
    assert "AbortSignal" in oh_my_llm.__all__
    assert not hasattr(oh_my_core, "AbortSignal")
    assert tuple(inspect.signature(oh_my_llm.AbortSignal).parameters) == ()

    with pytest.raises(TypeError, match="factory-produced"):
        oh_my_llm.AbortSignal()

    assert set(name for name in dir(oh_my_llm.AbortSignal) if not name.startswith("_")) == {
        "aborted",
        "wait",
    }

    async def observe() -> None:
        faux = fauxProvider()
        model = faux.getModel()
        assert model is not None
        faux.setResponses((fauxAssistantMessage("done"),))
        models = createModels()
        models.setProvider(faux.provider)
        started = asyncio.Event()
        release = asyncio.Event()
        captured: list[oh_my_llm.AbortSignal] = []

        async def stream_fn(model, context, options, signal):  # type: ignore[no-untyped-def]
            captured.append(signal)
            started.set()
            await release.wait()
            async for event in models.streamSimple(model, context, options):
                yield event

        operation = asyncio.create_task(
            oh_my_core.runAgentLoop(
                (UserMessage(content="go", timestamp=1),),
                AgentContext(systemPrompt="", messages=()),
                AgentLoopConfig(model=model),
                lambda event: None,
                stream_fn,
            )
        )
        await started.wait()
        signal = captured[0]
        first = asyncio.create_task(signal.wait())
        second = asyncio.create_task(signal.wait())
        await asyncio.sleep(0)

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not signal.aborted
        assert not second.done()

        release.set()
        await operation
        assert not signal.aborted
        assert not second.done()
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second

    asyncio.run(observe())


def test_agent_loop_activates_once_and_retains_fifo_for_a_late_consumer() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    response = fauxAssistantMessage("done")
    faux.setResponses((response,))
    models = createModels()
    models.setProvider(faux.provider)
    prompt = UserMessage(content="go", timestamp=1)

    async def stream_fn(model, context, options, signal):  # type: ignore[no-untyped-def]
        del signal
        async for event in models.streamSimple(model, context, options):
            yield event

    stream = oh_my_core.agentLoop(
        (prompt,),
        AgentContext(systemPrompt="", messages=()),
        AgentLoopConfig(model=model),
        stream_fn,
    )
    assert type(stream) is oh_my_llm.EventStream
    assert faux.state.callCount == 0

    async def observe() -> None:
        first, second = await asyncio.gather(stream.result(), stream.result())
        assert first is second
        assert first == (prompt, response)
        assert faux.state.callCount == 1

        events = [event async for event in stream]
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
        assert isinstance(events[-1], oh_my_core.AgentEvent.AgentEnd)
        assert events[-1].messages is first

    asyncio.run(observe())


def test_event_stream_rejects_a_second_consumer_before_delivery() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    faux.setResponses((fauxAssistantMessage("done"),))
    models = createModels()
    models.setProvider(faux.provider)

    async def stream_fn(model, context, options, signal):  # type: ignore[no-untyped-def]
        del signal
        async for event in models.streamSimple(model, context, options):
            yield event

    stream = oh_my_core.agentLoop(
        (UserMessage(content="go", timestamp=1),),
        AgentContext(systemPrompt="", messages=()),
        AgentLoopConfig(model=model),
        stream_fn,
    )

    async def observe() -> None:
        consumer = stream.__aiter__()
        with pytest.raises(oh_my_llm.LifecycleError) as rejected:
            stream.__aiter__()
        assert rejected.value.code == "consumer"
        event_types: list[str] = []
        while True:
            try:
                event_types.append((await consumer.__anext__()).type)
            except StopAsyncIteration:
                break
        assert event_types[-1] == "agent_end"

    asyncio.run(observe())


def test_prompt_and_continuation_pairs_share_terminal_projection_and_laziness() -> None:
    assert {
        "agentLoop",
        "agentLoopContinue",
        "runAgentLoop",
        "runAgentLoopContinue",
    } <= set(oh_my_core.__all__)

    prior = UserMessage(content="continue", timestamp=1)
    context = AgentContext(systemPrompt="", messages=(prior,))
    messages_before = context.messages

    stream_faux = fauxProvider()
    stream_model = stream_faux.getModel()
    assert stream_model is not None
    stream_response = fauxAssistantMessage("stream result")
    stream_faux.setResponses((stream_response,))
    stream_models = createModels()
    stream_models.setProvider(stream_faux.provider)

    async def stream_fn(model, working, options, signal):  # type: ignore[no-untyped-def]
        del signal
        async for event in stream_models.streamSimple(model, working, options):
            yield event

    stream = oh_my_core.agentLoopContinue(
        context,
        AgentLoopConfig(model=stream_model),
        stream_fn,
    )
    assert stream_faux.state.callCount == 0

    sink_faux = fauxProvider()
    sink_model = sink_faux.getModel()
    assert sink_model is not None
    sink_response = fauxAssistantMessage("sink result")
    sink_faux.setResponses((sink_response,))
    sink_models = createModels()
    sink_models.setProvider(sink_faux.provider)

    async def sink_fn(model, working, options, signal):  # type: ignore[no-untyped-def]
        del signal
        async for event in sink_models.streamSimple(model, working, options):
            yield event

    sink_events: list[oh_my_core.AgentEvent] = []
    operation = oh_my_core.runAgentLoopContinue(
        context,
        AgentLoopConfig(model=sink_model),
        sink_events.append,
        sink_fn,
    )
    assert inspect.iscoroutine(operation)
    assert sink_faux.state.callCount == 0

    async def observe() -> None:
        stream_result = await stream.result()
        sink_result = await operation
        assert stream_result == (stream_response,)
        assert sink_result == (sink_response,)
        assert context.messages is messages_before
        assert context.messages == (prior,)
        assert isinstance(sink_events[-1], oh_my_core.AgentEvent.AgentEnd)
        assert sink_events[-1].messages is sink_result

    asyncio.run(observe())


def test_cancelling_one_result_waiter_aborts_owned_work_after_cleanup() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    prompt = UserMessage(content="go", timestamp=1)
    started = asyncio.Event()
    cleanup = asyncio.Event()
    release = asyncio.Event()
    captured: list[oh_my_llm.AbortSignal] = []

    async def stream_fn(model, context, options, signal):  # type: ignore[no-untyped-def]
        del model, context, options
        captured.append(signal)
        started.set()
        try:
            await release.wait()
        finally:
            cleanup.set()
        if False:
            yield

    stream = oh_my_core.agentLoop(
        (prompt,),
        AgentContext(systemPrompt="", messages=()),
        AgentLoopConfig(model=model),
        stream_fn,
    )

    async def observe() -> None:
        events_task = asyncio.create_task(_collect_events(stream))
        cancelled_waiter = asyncio.create_task(stream.result())
        surviving_waiter = asyncio.create_task(stream.result())
        await started.wait()

        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter
        assert cleanup.is_set()
        result = await surviving_waiter
        events = await events_task
        assert captured[0].aborted
        assert result[0] is prompt
        assert isinstance(result[-1], oh_my_llm.AssistantMessage)
        assert result[-1].stopReason == "aborted"
        assert isinstance(events[-1], oh_my_core.AgentEvent.AgentEnd)
        assert events[-1].messages is result

    asyncio.run(observe())


async def _collect_events(
    stream: oh_my_llm.EventStream[oh_my_core.AgentEvent, tuple[oh_my_core.AgentMessage, ...]],
) -> list[oh_my_core.AgentEvent]:
    return [event async for event in stream]


def test_sink_is_an_awaited_barrier_and_failure_closes_model_work() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    response = fauxAssistantMessage("done")
    faux.setResponses((response,))
    models = createModels()
    models.setProvider(faux.provider)
    prompt = UserMessage(content="go", timestamp=1)
    sink_entered = asyncio.Event()
    release_sink = asyncio.Event()

    async def stream_fn(model, context, options, signal):  # type: ignore[no-untyped-def]
        del signal
        async for event in models.streamSimple(model, context, options):
            yield event

    async def blocking_sink(event: oh_my_core.AgentEvent) -> None:
        if isinstance(event, oh_my_core.AgentEvent.TurnStart):
            sink_entered.set()
            await release_sink.wait()

    async def prove_barrier() -> None:
        operation = asyncio.create_task(
            oh_my_core.runAgentLoop(
                (prompt,),
                AgentContext(systemPrompt="", messages=()),
                AgentLoopConfig(model=model),
                blocking_sink,
                stream_fn,
            )
        )
        await sink_entered.wait()
        assert faux.state.callCount == 0
        release_sink.set()
        assert await operation == (prompt, response)

    asyncio.run(prove_barrier())

    cleanup = asyncio.Event()
    captured: list[oh_my_llm.AbortSignal] = []
    canary = RuntimeError("sink canary")
    faux.appendResponses((response,))

    async def open_stream(model, context, options, signal):  # type: ignore[no-untyped-def]
        captured.append(signal)
        try:
            async for event in models.streamSimple(model, context, options):
                yield event
        finally:
            cleanup.set()

    async def failing_sink(event: oh_my_core.AgentEvent) -> None:
        if isinstance(event, oh_my_core.AgentEvent.MessageUpdate):
            raise canary

    async def prove_failure_settlement() -> None:
        with pytest.raises(oh_my_llm.LifecycleError) as failed:
            await oh_my_core.runAgentLoop(
                (prompt,),
                AgentContext(systemPrompt="", messages=()),
                AgentLoopConfig(model=model),
                failing_sink,
                open_stream,
            )
        assert failed.value.code == "event_sink"
        assert failed.value.causes == (canary,)
        assert failed.value.__cause__ is canary
        assert cleanup.is_set()
        assert captured[0].aborted

    asyncio.run(prove_failure_settlement())


def test_close_context_exit_and_consumer_cancellation_settle_owned_work() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    prompt = UserMessage(content="go", timestamp=1)

    dormant_calls: list[str] = []

    async def dormant_fn(model, context, options, signal):  # type: ignore[no-untyped-def]
        del model, context, options, signal
        dormant_calls.append("started")
        if False:
            yield

    dormant = oh_my_core.agentLoop(
        (prompt,),
        AgentContext(systemPrompt="", messages=()),
        AgentLoopConfig(model=model),
        dormant_fn,
    )
    asyncio.run(dormant.aclose())
    assert dormant_calls == []

    async def prove_context_exit() -> None:
        started = asyncio.Event()
        cleaned = asyncio.Event()
        release = asyncio.Event()
        captured: list[oh_my_llm.AbortSignal] = []

        async def blocked_fn(model, context, options, signal):  # type: ignore[no-untyped-def]
            del model, context, options
            captured.append(signal)
            started.set()
            try:
                await release.wait()
            finally:
                cleaned.set()
            if False:
                yield

        stream = oh_my_core.agentLoop(
            (prompt,),
            AgentContext(systemPrompt="", messages=()),
            AgentLoopConfig(model=model),
            blocked_fn,
        )
        async with stream:
            await started.wait()
        assert cleaned.is_set()
        assert captured[0].aborted
        result = await stream.result()
        assert isinstance(result[-1], oh_my_llm.AssistantMessage)
        assert result[-1].stopReason == "aborted"

    asyncio.run(prove_context_exit())

    async def prove_consumer_cancellation() -> None:
        started = asyncio.Event()
        cleaned = asyncio.Event()
        release = asyncio.Event()

        async def blocked_fn(model, context, options, signal):  # type: ignore[no-untyped-def]
            del model, context, options, signal
            started.set()
            try:
                await release.wait()
            finally:
                cleaned.set()
            if False:
                yield

        stream = oh_my_core.agentLoop(
            (prompt,),
            AgentContext(systemPrompt="", messages=()),
            AgentLoopConfig(model=model),
            blocked_fn,
        )
        consumer = asyncio.create_task(_collect_events(stream))
        await started.wait()
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert cleaned.is_set()
        result = await stream.result()
        assert isinstance(result[-1], oh_my_llm.AssistantMessage)
        assert result[-1].stopReason == "aborted"

    asyncio.run(prove_consumer_cancellation())


def test_cancelling_awaited_loop_waiter_aborts_after_stream_cleanup() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    prompt = UserMessage(content="go", timestamp=1)

    async def observe() -> None:
        started = asyncio.Event()
        cleaned = asyncio.Event()
        release = asyncio.Event()
        captured: list[oh_my_llm.AbortSignal] = []

        async def blocked_fn(model, context, options, signal):  # type: ignore[no-untyped-def]
            del model, context, options
            captured.append(signal)
            started.set()
            try:
                await release.wait()
            finally:
                cleaned.set()
            if False:
                yield

        operation = asyncio.create_task(
            oh_my_core.runAgentLoop(
                (prompt,),
                AgentContext(systemPrompt="", messages=()),
                AgentLoopConfig(model=model),
                lambda event: None,
                blocked_fn,
            )
        )
        await started.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert cleaned.is_set()
        assert captured[0].aborted

    asyncio.run(observe())


def test_cancelling_close_waiter_still_joins_cleanup_before_reraising() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None

    async def observe() -> None:
        started = asyncio.Event()
        cleanup_started = asyncio.Event()
        allow_cleanup = asyncio.Event()
        cleaned = asyncio.Event()

        async def blocked_fn(model, context, options, signal):  # type: ignore[no-untyped-def]
            del model, context, options, signal
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await allow_cleanup.wait()
                cleaned.set()
            if False:
                yield

        stream = oh_my_core.agentLoop(
            (UserMessage(content="go", timestamp=1),),
            AgentContext(systemPrompt="", messages=()),
            AgentLoopConfig(model=model),
            blocked_fn,
        )
        result_waiter = asyncio.create_task(stream.result())
        await started.wait()
        close_waiter = asyncio.create_task(stream.aclose())
        await cleanup_started.wait()
        close_waiter.cancel()
        cancellation_turn = asyncio.Event()
        asyncio.get_running_loop().call_soon(cancellation_turn.set)
        await cancellation_turn.wait()
        assert not close_waiter.done()
        allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await close_waiter
        assert cleaned.is_set()
        result = await result_waiter
        assert isinstance(result[-1], oh_my_llm.AssistantMessage)
        assert result[-1].stopReason == "aborted"

    asyncio.run(observe())


def test_stream_function_failure_settles_one_error_terminal_value() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    prompt = UserMessage(content="go", timestamp=1)

    async def broken_fn(model, context, options, signal):  # type: ignore[no-untyped-def]
        del model, context, options, signal
        raise RuntimeError("PRIVATE_STREAM_CANARY")
        if False:
            yield

    stream = oh_my_core.agentLoop(
        (prompt,),
        AgentContext(systemPrompt="", messages=()),
        AgentLoopConfig(model=model),
        broken_fn,
    )

    async def observe() -> None:
        events_task = asyncio.create_task(_collect_events(stream))
        result = await stream.result()
        events = await events_task
        assert isinstance(result[-1], oh_my_llm.AssistantMessage)
        assert result[-1].stopReason == "error"
        assert result[-1].errorMessage is not None
        assert "PRIVATE_STREAM_CANARY" not in result[-1].errorMessage
        assert isinstance(events[-1], oh_my_core.AgentEvent.AgentEnd)
        assert events[-1].messages is result
        assert sum(isinstance(event, oh_my_core.AgentEvent.AgentEnd) for event in events) == 1

    asyncio.run(observe())


def test_context_body_stays_primary_over_close_cleanup_failure() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None

    async def observe() -> None:
        started = asyncio.Event()
        cleanup_canary = RuntimeError("cleanup canary")
        body_canary = RuntimeError("body canary")

        async def broken_cleanup(model, context, options, signal):  # type: ignore[no-untyped-def]
            del model, context, options, signal
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                raise cleanup_canary
            if False:
                yield

        stream = oh_my_core.agentLoop(
            (UserMessage(content="go", timestamp=1),),
            AgentContext(systemPrompt="", messages=()),
            AgentLoopConfig(model=model),
            broken_cleanup,
        )
        result_waiter = asyncio.create_task(stream.result())
        with pytest.raises(RuntimeError, match="body canary") as failed:
            async with stream:
                await started.wait()
                raise body_canary
        assert failed.value is body_canary
        assert isinstance(failed.value.__cause__, oh_my_llm.LifecycleError)
        assert failed.value.__cause__.code == "cleanup"
        assert failed.value.__cause__.causes == (cleanup_canary,)
        with pytest.raises(oh_my_llm.LifecycleError) as result_failed:
            await result_waiter
        assert result_failed.value is failed.value.__cause__

    asyncio.run(observe())


def test_stream_factories_validate_static_dependencies_without_iterating_inputs() -> None:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    input_effects: list[str] = []

    def prompt_generator() -> Iterator[UserMessage]:
        input_effects.append("iterated")
        yield UserMessage(content="go", timestamp=1)

    async def stream_fn(model, context, options, signal):  # type: ignore[no-untyped-def]
        del model, context, options, signal
        if False:
            yield

    with pytest.raises(TypeError, match="list or tuple"):
        oh_my_core.agentLoop(
            prompt_generator(),  # type: ignore[arg-type]
            AgentContext(systemPrompt="", messages=()),
            AgentLoopConfig(model=model),
            stream_fn,
        )
    assert input_effects == []

    with pytest.raises(TypeError, match="streamFn"):
        oh_my_core.agentLoop(
            (UserMessage(content="go", timestamp=1),),
            AgentContext(systemPrompt="", messages=()),
            AgentLoopConfig(model=model),
            None,  # type: ignore[arg-type]
        )
