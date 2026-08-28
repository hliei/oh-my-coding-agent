from __future__ import annotations

import asyncio

from oh_my_llm import Context, UserMessage, createModels, fauxAssistantMessage, fauxProvider


def test_faux_model_completes_one_deterministic_text_response() -> None:
    async def run() -> None:
        faux = fauxProvider()
        response = fauxAssistantMessage("public hello")
        faux.setResponses((response,))
        models = createModels()
        models.setProvider(faux.provider)
        model = models.getModel("faux", "faux-1")
        assert model is not None
        events = []
        context = Context(messages=(UserMessage(content="hello", timestamp=1),))
        async for event in models.streamSimple(model, context):
            events.append(event)

        assert [event.type for event in events] == [
            "start",
            "text_start",
            "text_delta",
            "text_end",
            "done",
        ]
        assert events[-1].message == response  # type: ignore[union-attr]
        assert faux.state.callCount == 1
        assert faux.getPendingResponseCount() == 0

    asyncio.run(run())
