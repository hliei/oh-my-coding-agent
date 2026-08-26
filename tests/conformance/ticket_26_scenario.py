from __future__ import annotations

import hashlib
from importlib import metadata
import importlib.util
import inspect
import json
from types import ModuleType
from typing import Callable, cast, get_type_hints

import oh_my_coding_agent
import oh_my_core
import oh_my_llm
import oh_my_llm.providers.deepseek as deepseek


def _public_members(value: type[object]) -> list[str]:
    return sorted(name for name in vars(value) if not name.startswith("_"))


def _signature(value: object) -> str | None:
    try:
        return str(inspect.signature(cast(Callable[..., object], value)))
    except (TypeError, ValueError):
        return None


def _module_signatures(module: ModuleType) -> dict[str, str]:
    signatures: dict[str, str] = {}
    for name in module.__all__:
        value = getattr(module, name)
        signature = _signature(value)
        if signature is not None:
            signatures[f"{module.__name__}.{name}"] = signature
    return signatures


def _member_signatures(public_types: tuple[type[object], ...]) -> dict[str, str]:
    signatures: dict[str, str] = {}
    for public_type in public_types:
        for name in _public_members(public_type):
            value = getattr(public_type, name)
            if isinstance(value, property):
                continue
            signature = _signature(value)
            if signature is not None:
                signatures[f"{public_type.__module__}.{public_type.__name__}.{name}"] = (
                    signature
                )
    return signatures


def _is_importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


def main() -> None:
    distribution = metadata.distribution("omh")
    roots = (oh_my_llm, oh_my_core, oh_my_coding_agent)
    public_types = (
        oh_my_llm.Models,
        oh_my_llm.MutableModels,
        oh_my_llm.Provider,
        oh_my_llm.FauxProviderHandle,
        oh_my_llm.AbortSignal,
        oh_my_llm.EventStream,
        oh_my_core.Agent,
        oh_my_coding_agent.SessionManager,
        oh_my_coding_agent.AgentSession,
        oh_my_coding_agent.ExtensionAPI,
        oh_my_coding_agent.ExtensionContext,
    )
    excluded_paths = (
        "pi_ai",
        "pi_agent_core",
        "pi_coding_agent",
        "oh_my_llm.compat",
        "oh_my_llm.node",
        "oh_my_core.compat",
        "oh_my_core.rpc_entry",
        "oh_my_coding_agent.rpc_entry",
    )
    excluded_names = {
        "oh_my_llm": (
            "AbortController",
            "createAssistantMessageEventStream",
            "createFauxCore",
            "fauxThinking",
            "registerProvider",
            "stream",
            "complete",
        ),
        "oh_my_core": (
            "AbortSignal",
            "EventStream",
            "Model",
            "QueueMode",
            "run_agent_loop",
        ),
        "oh_my_coding_agent": (
            "Agent",
            "AgentSessionConfig",
            "ReadonlySessionManager",
            "SessionId",
            "SessionPath",
            "main",
            "parseArgs",
        ),
    }
    assert get_type_hints(deepseek.deepseekProvider)["return"] is oh_my_llm.Provider
    signatures = _module_signatures(deepseek)
    for root in roots:
        signatures.update(_module_signatures(root))
    signatures.update(_member_signatures(public_types))
    root_names = {root.__name__: sorted(root.__all__) for root in roots}
    child_names = {"oh_my_llm.providers.deepseek": sorted(deepseek.__all__)}
    members = {
        f"{value.__module__}.{value.__name__}": _public_members(value)
        for value in public_types
    }
    surface = {
        "rootNames": root_names,
        "childNames": child_names,
        "members": members,
        "signatures": dict(sorted(signatures.items())),
    }
    surface_sha256 = hashlib.sha256(
        json.dumps(surface, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    actual = {
        "reference.public-surface-closure": {
            "A": {
                "distribution": distribution.metadata["Name"],
                "version": distribution.version,
                "requiresPython": distribution.metadata["Requires-Python"],
                "requirements": sorted(distribution.requires or ()),
                "consoleScripts": sorted(
                    f"{entry.name}={entry.value}"
                    for entry in distribution.entry_points
                    if entry.group == "console_scripts"
                ),
                "importPaths": [
                    "oh_my_llm",
                    "oh_my_llm.providers.deepseek",
                    "oh_my_core",
                    "oh_my_coding_agent",
                ],
            },
            "L": [],
            "T": {
                "rootNameCounts": {
                    name: len(names) for name, names in root_names.items()
                },
                "memberCounts": {
                    name: len(names) for name, names in members.items()
                },
                "sessionVersion": oh_my_coding_agent.CURRENT_SESSION_VERSION,
                "signatureCount": len(signatures),
                "surfaceSha256": surface_sha256,
            },
            "E": {
                "deepseekImplementationNames": sorted(
                    name
                    for name in vars(deepseek)
                    if not name.startswith("_") and name not in deepseek.__all__
                ),
                "excludedImportPathsPresent": [
                    name for name in excluded_paths if _is_importable(name)
                ],
                "excludedRootNamesPresent": {
                    root.__name__: [
                        name
                        for name in excluded_names[root.__name__]
                        if hasattr(root, name)
                    ]
                    for root in roots
                },
            },
            "C": "closed_installed_public_surface",
        }
    }
    print(json.dumps(actual, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
