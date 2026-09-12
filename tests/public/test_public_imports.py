from __future__ import annotations

from importlib import metadata

import oh_my_coding_agent
import oh_my_core
import oh_my_llm
import oh_my_telemetry


def test_distribution_metadata_and_public_packages_are_available() -> None:
    distribution = metadata.distribution("omh")
    assert distribution.version == "0.3.0"
    assert distribution.metadata["License-Expression"] == "MIT"
    assert distribution.metadata["Description-Content-Type"] == "text/markdown"
    assert oh_my_llm.__all__
    assert oh_my_core.__all__
    assert oh_my_coding_agent.__all__
    assert oh_my_telemetry.__all__
    assert not hasattr(oh_my_llm, "Agent")
    assert not hasattr(oh_my_core, "Model")
    assert not hasattr(oh_my_coding_agent, "Agent")
    assert not set(oh_my_telemetry.__all__) & set(oh_my_llm.__all__)
    assert not set(oh_my_telemetry.__all__) & set(oh_my_core.__all__)
    assert not set(oh_my_telemetry.__all__) & set(oh_my_coding_agent.__all__)
