from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from oh_my_coding_agent import (
    CreateAgentSessionOptions,
    NewSessionOptions,
    SessionManager,
    createAgentSession,
)
from oh_my_llm import createModels
from oh_my_llm.providers.deepseek import deepseekProvider


def test_in_memory_product_session_can_be_created_and_disposed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "public-test-placeholder")
    manager = SessionManager.inMemory(
        os.fspath(tmp_path),
        NewSessionOptions(id="public-smoke"),
    )
    models = createModels()
    models.setProvider(deepseekProvider())
    model = models.getModel("deepseek", "deepseek-v4-flash")
    assert model is not None

    async def run() -> None:
        result = await createAgentSession(
            CreateAgentSessionOptions(
                cwd=os.fspath(tmp_path),
                model=model,
                sessionManager=manager,
                projectTrusted=False,
            )
        )
        assert result.session.sessionManager is manager
        assert result.session.sessionId == "public-smoke"
        assert result.session.sessionFile is None
        assert result.session.isIdle is True
        await result.session.dispose()
        assert result.session.isIdle is True

    asyncio.run(run())
