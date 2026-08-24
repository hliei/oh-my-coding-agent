from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncIterator
import inspect
import time
from typing import Any

import pytest

import oh_my_core
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
    AssistantMessage,
    AssistantMessageEvent,
    LifecycleError,
    Model,
    SimpleStreamOptions,
    ToolResultMessage,
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


def _prepare(
    responses: tuple[AssistantMessage, ...],
    *,
    system_prompt: str = "",
    messages: tuple[Any, ...] = (),
) -> tuple[Agent, Any, Model, Any, StreamFn]:
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    faux.setResponses(responses)
    models = createModels()
    models.setProvider(faux.provider)
    stream_fn = _stream_fn(models)
    agent = Agent(
        AgentOptions(
            initialState=AgentState(
                model=model,
                systemPrompt=system_prompt,
                messages=messages,
            ),
            streamFn=stream_fn,
        )
    )
    return agent, faux, model, models, stream_fn


def test_agent_construction_requires_owned_seed_and_publishes_selected_members() -> None:
    assert {"Agent", "AgentOptions", "AgentState"} <= set(oh_my_core.__all__)
    assert tuple(inspect.signature(Agent).parameters) == ("options",)
    assert tuple(inspect.signature(AgentState).parameters) == (
        "model",
        "systemPrompt",
        "tools",
        "messages",
    )
    assert tuple(inspect.signature(AgentOptions).parameters) == (
        "initialState",
        "streamFn",
        "toolExecution",
    )
    assert not hasattr(Agent, "continue")
    assert callable(Agent.continue_)
    with pytest.raises(SyntaxError):
        ast.parse("agent.continue()")
    assert {name for name in dir(Agent) if not name.startswith("_")} == {
        "abort",
        "continue_",
        "prompt",
        "reset",
        "signal",
        "state",
        "subscribe",
        "waitForIdle",
    }
    assert {name for name in dir(AgentState) if not name.startswith("_")} == {
        "errorMessage",
        "isStreaming",
        "messages",
        "model",
        "pendingToolCalls",
        "streamingMessage",
        "systemPrompt",
        "tools",
    }

    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    stream_fn = _stream_fn(createModels())
    seed_message = UserMessage(content="seed", timestamp=1)
    seed = AgentState(
        model=model,
        systemPrompt="sys",
        messages=(seed_message,),
        tools=(),
    )
    agent = Agent(AgentOptions(initialState=seed, streamFn=stream_fn))
    assert agent.state is not seed
    assert agent.state is agent.state
    assert agent.state.systemPrompt == "sys"
    assert agent.state.model is model
    assert agent.state.messages == (seed_message,)
    assert agent.state.messages is not seed.messages
    assert agent.state.tools == ()
    assert agent.state.isStreaming is False
    assert agent.state.streamingMessage is None
    assert agent.state.pendingToolCalls == frozenset()
    assert agent.state.errorMessage is None
    assert agent.signal is None
    agent.abort()
    assert agent.signal is None
    assert agent.state.isStreaming is False
    seed.systemPrompt = "mutated seed"
    assert agent.state.systemPrompt == "sys"
    with pytest.raises(TypeError):
        AgentState(model=model, isStreaming=True)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        AgentOptions(initialState=seed, streamFn=None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Agent("not-options")  # type: ignore[arg-type]


def test_prompt_accepts_text_one_message_and_nonempty_sequences() -> None:
    first = fauxAssistantMessage("from-text")
    second = fauxAssistantMessage("from-message")
    third = fauxAssistantMessage("from-sequence")
    fourth = fauxAssistantMessage("from-empty-text")
    agent, faux, _model, _models, _stream = _prepare((first, second, third, fourth))
    types: list[str] = []
    agent.subscribe(lambda event, signal: types.append(event.type))

    async def run() -> None:
        before = time.time_ns() // 1_000_000
        await agent.prompt("hello")
        after = time.time_ns() // 1_000_000
        user = agent.state.messages[0]
        assert type(user) is UserMessage
        assert user.content == "hello"
        assert before <= user.timestamp <= after
        assert agent.state.messages[-1] == first
        assert agent.state.isStreaming is False
        assert agent.signal is None

        agent.reset()
        message = UserMessage(content="one", timestamp=7)
        await agent.prompt(message)
        assert agent.state.messages[0] is message
        assert agent.state.messages[-1] == second

        agent.reset()
        leading = UserMessage(content="a", timestamp=8)
        trailing = UserMessage(content="b", timestamp=9)
        await agent.prompt((leading, trailing))
        assert agent.state.messages[:2] == (leading, trailing)
        assert agent.state.messages[-1] == third

        agent.reset()
        await agent.prompt("")
        empty = agent.state.messages[0]
        assert type(empty) is UserMessage
        assert empty.content == ""
        assert agent.state.messages[-1] == fourth

    asyncio.run(run())
    assert types[0] == "agent_start"
    assert types[-1] == "agent_end"
    assert faux.state.callCount == 4


def test_continue_adds_no_seed_and_requires_user_or_tool_result_tail() -> None:
    prior = UserMessage(content="prior", timestamp=1)
    continued = fauxAssistantMessage("continued")
    agent, faux, _model, _models, _stream = _prepare((continued,), messages=(prior,))

    async def run() -> None:
        await agent.continue_()
        assert agent.state.messages[0] is prior
        assert agent.state.messages[-1] == continued
        with pytest.raises(ValueError, match="tail"):
            await agent.continue_()

    asyncio.run(run())
    assert faux.state.callCount == 1

    tool_tail = ToolResultMessage(
        toolCallId="call",
        toolName="tool",
        content=(),
        details={},
        isError=False,
        timestamp=2,
    )
    from_tool = fauxAssistantMessage("from-tool")
    tool_agent, tool_faux, _model, _models, _stream = _prepare(
        (from_tool,), messages=(tool_tail,)
    )

    async def continue_from_tool() -> None:
        await tool_agent.continue_()
        assert tool_agent.state.messages[0] is tool_tail
        assert tool_agent.state.messages[-1] == from_tool

    asyncio.run(continue_from_tool())
    assert tool_faux.state.callCount == 1


def test_invalid_and_busy_prompts_reject_before_run_identity_or_effect() -> None:
    agent, faux, _model, _models, _stream = _prepare((fauxAssistantMessage("ok"),))
    idle_messages = agent.state.messages
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"count": 0}

    busy_faux = fauxProvider()
    model = busy_faux.getModel()
    assert model is not None
    busy_faux.setResponses((fauxAssistantMessage("busy-ok"),))
    busy_models = createModels()
    busy_models.setProvider(busy_faux.provider)

    async def gated_stream(
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

    busy_agent = Agent(
        AgentOptions(initialState=AgentState(model=model), streamFn=gated_stream)
    )
    events: list[AgentEvent] = []
    busy_agent.subscribe(lambda event, signal: events.append(event))

    async def run() -> None:
        coro = agent.prompt("lazy")
        assert inspect.iscoroutine(coro)
        assert faux.state.callCount == 0
        coro.close()

        with pytest.raises(TypeError):
            await agent.prompt(123)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            await agent.prompt((UserMessage(content="x", timestamp=1), "nope"))  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            await agent.prompt(())
        with pytest.raises(ValueError, match="tail"):
            await agent.prompt(fauxAssistantMessage("assistant-only"))
        with pytest.raises(ValueError, match="tail"):
            await agent.continue_()
        assert agent.state.messages is idle_messages
        assert agent.state.isStreaming is False
        assert agent.signal is None
        assert faux.state.callCount == 0

        first = asyncio.create_task(busy_agent.prompt("one"))
        await started.wait()
        captured_signal = busy_agent.signal
        assert busy_agent.state.isStreaming is True
        assert captured_signal is not None
        with pytest.raises(LifecycleError) as busy_prompt:
            await busy_agent.prompt("two")
        with pytest.raises(LifecycleError) as busy_continue:
            await busy_agent.continue_()
        assert busy_prompt.value.code == "busy"
        assert busy_continue.value.code == "busy"
        assert busy_agent.signal is captured_signal
        assert events[0].type == "agent_start"
        assert calls["count"] == 1
        release.set()
        await first
        assert busy_agent.state.isStreaming is False

    asyncio.run(run())


def test_idle_assignment_is_atomic_and_busy_mutation_fails_without_change() -> None:
    agent, _faux, _model, _models, stream_fn = _prepare((fauxAssistantMessage("busy"),))
    replacement = UserMessage(content="replaced", timestamp=3)
    original_messages = agent.state.messages
    agent.state.systemPrompt = "next"
    agent.state.messages = (replacement,)
    assert agent.state.systemPrompt == "next"
    assert agent.state.messages == (replacement,)
    assert agent.state.messages is not original_messages
    with pytest.raises(TypeError):
        agent.state.messages = (replacement, object())  # type: ignore[assignment]
    assert agent.state.messages == (replacement,)
    with pytest.raises(AttributeError):
        agent.state.isStreaming = True  # type: ignore[misc]
    with pytest.raises(AttributeError):
        agent.state.errorMessage = "nope"  # type: ignore[misc]
    assert agent.state.isStreaming is False

    started = asyncio.Event()
    release = asyncio.Event()
    busy_faux = fauxProvider()
    model = busy_faux.getModel()
    assert model is not None
    busy_faux.setResponses((fauxAssistantMessage("busy"),))
    busy_models = createModels()
    busy_models.setProvider(busy_faux.provider)

    async def gated_stream(
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
            initialState=AgentState(model=model, systemPrompt="keep"),
            streamFn=gated_stream,
        )
    )
    keep_prompt = busy_agent.state.systemPrompt
    keep_model = busy_agent.state.model
    keep_tools = busy_agent.state.tools

    async def run() -> None:
        task = asyncio.create_task(busy_agent.prompt("go"))
        await started.wait()
        held_messages = busy_agent.state.messages
        with pytest.raises(LifecycleError) as error:
            busy_agent.state.systemPrompt = "mutated"
        assert error.value.code == "busy"
        with pytest.raises(LifecycleError) as model_error:
            busy_agent.state.model = model
        assert model_error.value.code == "busy"
        with pytest.raises(LifecycleError) as tools_error:
            busy_agent.state.tools = ()
        assert tools_error.value.code == "busy"
        with pytest.raises(LifecycleError) as messages_error:
            busy_agent.state.messages = (replacement,)
        assert messages_error.value.code == "busy"
        assert busy_agent.state.systemPrompt == keep_prompt
        assert busy_agent.state.model is keep_model
        assert busy_agent.state.tools is keep_tools
        assert busy_agent.state.messages is held_messages
        release.set()
        await task

    asyncio.run(run())
    del stream_fn


def test_idle_reset_clears_messages_and_error_and_busy_reset_does_not_mutate() -> None:
    agent, _faux, model, _models, _stream = _prepare((fauxAssistantMessage("done"),))
    agent.state.systemPrompt = "keep-prompt"

    async def run() -> None:
        await agent.prompt("hello")
        assert agent.state.messages
        agent.reset()
        assert agent.state.messages == ()
        assert agent.state.errorMessage is None
        assert agent.state.systemPrompt == "keep-prompt"
        assert agent.state.model is model
        assert agent.state.tools == ()

        started = asyncio.Event()
        release = asyncio.Event()
        busy_faux = fauxProvider()
        busy_model = busy_faux.getModel()
        assert busy_model is not None
        busy_faux.setResponses((fauxAssistantMessage("busy"),))
        busy_models = createModels()
        busy_models.setProvider(busy_faux.provider)
        prior = UserMessage(content="prior", timestamp=1)

        async def gated_stream(
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
                initialState=AgentState(model=busy_model, messages=(prior,)),
                streamFn=gated_stream,
            )
        )
        held = busy_agent.state.messages
        task = asyncio.create_task(busy_agent.continue_())
        await started.wait()
        with pytest.raises(LifecycleError) as error:
            busy_agent.reset()
        assert error.value.code == "busy"
        assert busy_agent.state.messages is held
        release.set()
        await task

    asyncio.run(run())


def test_state_reduction_precedes_listeners_and_streaming_holds_through_agent_end() -> None:
    agent, _faux, _model, _models, _stream = _prepare((fauxAssistantMessage("hello"),))
    observations: list[tuple[str, bool, object, bool]] = []

    def listener(event: AgentEvent, signal: AbortSignal) -> None:
        observations.append(
            (
                event.type,
                agent.state.isStreaming,
                agent.state.streamingMessage,
                agent.signal is signal,
            )
        )
        if event.type == "message_start" and isinstance(event, AgentEvent.MessageStart):
            if type(event.message) is AssistantMessage:
                assert agent.state.streamingMessage is event.message
        if event.type == "message_end" and isinstance(event, AgentEvent.MessageEnd):
            assert agent.state.streamingMessage is None
            assert agent.state.messages[-1] is event.message
        if event.type == "agent_end":
            assert agent.state.isStreaming is True
            assert agent.signal is signal

    started = asyncio.Event()
    idle_task: asyncio.Task[None] | None = None

    def wrapped(event: AgentEvent, signal: AbortSignal) -> None:
        nonlocal idle_task
        if event.type == "agent_start":
            idle_task = asyncio.create_task(agent.waitForIdle())
            started.set()
        original(event, signal)
        if event.type == "agent_end":
            assert idle_task is not None
            assert not idle_task.done()

    original = listener
    agent.subscribe(wrapped)

    async def run() -> None:
        await agent.prompt("hi")
        await started.wait()
        assert idle_task is not None
        await idle_task

    asyncio.run(run())
    assert observations[0][0] == "agent_start"
    assert observations[0][1] is True
    assert observations[-1][0] == "agent_end"
    assert observations[-1][1] is True
    assert agent.state.isStreaming is False
    assert agent.signal is None


def test_each_subscription_is_an_independent_ordered_record() -> None:
    agent, _faux, _model, _models, _stream = _prepare(
        (fauxAssistantMessage("one"), fauxAssistantMessage("two"))
    )
    order: list[str] = []

    def make_listener(label: str) -> Any:
        def listen(event: AgentEvent, signal: AbortSignal) -> None:
            del signal
            if event.type == "agent_start":
                order.append(label)

        return listen

    shared = make_listener("shared")
    first = agent.subscribe(shared)
    second = agent.subscribe(shared)
    late = make_listener("late")

    def mutator(event: AgentEvent, signal: AbortSignal) -> None:
        del signal
        if event.type == "agent_start":
            order.append("mutator")
            agent.subscribe(late)
            second()

    agent.subscribe(mutator)

    async def run() -> None:
        await agent.prompt("first")
        first()
        first()
        await agent.prompt("second")

    asyncio.run(run())
    assert order == ["shared", "shared", "mutator", "mutator", "late"]


def test_ordinary_model_failure_commits_error_assistant_and_allows_reuse() -> None:
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

    async def run() -> None:
        await agent.prompt("fail")
        error = agent.state.messages[-1]
        assert type(error) is AssistantMessage
        assert error.stopReason == "error"
        assert error.errorMessage == "Model stream failed"
        assert secret not in error.errorMessage
        assert agent.state.errorMessage == "Model stream failed"
        assert agent.state.isStreaming is False
        await agent.prompt("again")
        assert agent.state.messages[-1] == recovered
        assert agent.state.errorMessage is None

    asyncio.run(run())
    assert calls["count"] == 2


def test_agent_history_is_reduction_suffix_equal_to_low_level_run() -> None:
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
    agent = Agent(
        AgentOptions(initialState=AgentState(model=model), streamFn=stream_fn)
    )
    agent.subscribe(lambda event, signal: high_events.append(event.type))

    async def run() -> None:
        result = await runAgentLoop(
            (prompt,),
            AgentContext(systemPrompt="", messages=()),
            AgentLoopConfig(model=model),
            lambda event: low_events.append(event.type),
            stream_fn,
        )
        await agent.prompt(prompt)
        suffix = agent.state.messages[-len(result) :]
        assert suffix == result
        assert suffix[-1] == response
        assert low_events == high_events

    asyncio.run(run())
    assert faux.state.callCount == 2


def test_wait_for_idle_captures_only_the_current_run() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    faux = fauxProvider()
    model = faux.getModel()
    assert model is not None
    first_response = fauxAssistantMessage("first")
    second_response = fauxAssistantMessage("second")
    faux.setResponses((first_response, second_response))
    models = createModels()
    models.setProvider(faux.provider)

    async def gated_stream(
        model: Model,
        context: Any,
        options: SimpleStreamOptions | None,
        signal: AbortSignal,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del signal
        started.set()
        await release.wait()
        async for event in models.streamSimple(model, context, options):
            yield event

    agent = Agent(
        AgentOptions(initialState=AgentState(model=model), streamFn=gated_stream)
    )

    async def run() -> None:
        await agent.waitForIdle()
        first = asyncio.create_task(agent.prompt("one"))
        await started.wait()
        waiter = asyncio.create_task(agent.waitForIdle())
        await asyncio.sleep(0)
        assert not waiter.done()
        release.set()
        await first
        await waiter
        await agent.prompt("two")
        assert agent.state.messages[-1] == second_response

    asyncio.run(run())
