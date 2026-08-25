from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeAlias, final

from oh_my_llm import JSONValue, TextContent, Tool
from oh_my_llm._values import _PublicValueRecord, _bool, _sequence, _snapshot_json, _string


class _AgentToolOwnerCleanupError(Exception):
    __slots__ = ("cause",)

    def __init__(self, cause: BaseException) -> None:
        super().__init__("Agent Tool owner cleanup failed")
        self.cause = cause


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class AgentToolResult(_PublicValueRecord):
    content: tuple[TextContent, ...]
    details: JSONValue
    terminate: bool | None = None

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        object.__setattr__(
            self,
            "content",
            _sequence(self.content, (TextContent,), type_name, "content"),
        )
        object.__setattr__(
            self,
            "details",
            _snapshot_json(self.details, type_name, "details"),
        )
        if self.terminate is not None:
            object.__setattr__(
                self,
                "terminate",
                _bool(self.terminate, type_name, "terminate"),
            )


AgentToolUpdateCallback: TypeAlias = Callable[[AgentToolResult], None]


class _ActiveAbortSignal(Protocol):
    @property
    def aborted(self) -> bool: ...

    def wait(self) -> Awaitable[None]: ...


_AgentToolExecute: TypeAlias = Callable[
    [str, dict[str, object], _ActiveAbortSignal, AgentToolUpdateCallback],
    Awaitable[AgentToolResult],
]
_PrepareArguments: TypeAlias = Callable[[dict[str, object]], dict[str, object]]


@final
@dataclass(eq=False, frozen=True, slots=True, kw_only=True)
class AgentTool(Tool):
    label: str
    execute: _AgentToolExecute
    prepareArguments: _PrepareArguments | None = None
    executionMode: Literal["sequential", "parallel"] | None = None

    __eq__ = object.__eq__
    __hash__ = object.__hash__

    def __post_init__(self) -> None:
        Tool.__post_init__(self)
        type_name = type(self).__name__
        object.__setattr__(self, "label", _string(self.label, type_name, "label"))
        if not callable(self.execute):
            raise TypeError(f"{type_name}.execute: must be callable")
        if self.prepareArguments is not None and not callable(self.prepareArguments):
            raise TypeError(f"{type_name}.prepareArguments: must be callable or None")
        if self.executionMode not in (None, "sequential", "parallel"):
            raise ValueError(
                f"{type_name}.executionMode: must be sequential, parallel, or None"
            )

    def __init_subclass__(cls) -> None:
        raise TypeError("AgentTool is sealed")
