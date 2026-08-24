from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, TypeAlias, final


ModelsErrorCode: TypeAlias = Literal[
    "model_source",
    "model_validation",
    "provider",
    "stream",
    "auth",
    "oauth",
]
_MODELS_ERROR_CODES = frozenset(
    {
        "model_source",
        "model_validation",
        "provider",
        "stream",
        "auth",
        "oauth",
    }
)

LifecycleErrorCode: TypeAlias = Literal[
    "consumer",
    "busy",
    "reentrant",
    "listener",
    "event_sink",
    "hook",
    "cleanup",
    "closing",
    "disposed",
    "disposal",
]

_CAUSE_REQUIRED = frozenset({"listener", "event_sink", "cleanup", "disposal"})
_CAUSE_FORBIDDEN = frozenset({"consumer", "busy", "reentrant", "closing", "disposed"})
_ALL_CODES = frozenset(
    {
        "consumer",
        "busy",
        "reentrant",
        "listener",
        "event_sink",
        "hook",
        "cleanup",
        "closing",
        "disposed",
        "disposal",
    }
)


@final
class ModelsError(RuntimeError):
    __slots__ = ("_code",)

    def __init__(
        self,
        code: ModelsErrorCode,
        message: str,
        *,
        cause: BaseException | None = None,
    ) -> None:
        if code not in _MODELS_ERROR_CODES:
            raise ValueError("ModelsError.code: unknown code")
        if type(message) is not str:
            raise TypeError("ModelsError.message: must be a string")
        super().__init__(message)
        self._code = code
        if cause is not None:
            self.__cause__ = cause

    @property
    def code(self) -> ModelsErrorCode:
        return self._code


@final
class LifecycleError(RuntimeError):
    __slots__ = ("_causes", "_code")

    def __init__(
        self,
        code: LifecycleErrorCode,
        message: str,
        *,
        causes: Sequence[BaseException] = (),
    ) -> None:
        if code not in _ALL_CODES:
            raise ValueError("LifecycleError.code: unknown code")
        if type(message) is not str:
            raise TypeError("LifecycleError.message: must be a string")
        if type(causes) not in (list, tuple):
            raise TypeError("LifecycleError.causes: must be a list or tuple")
        owned_causes = tuple(causes)
        if any(not isinstance(cause, BaseException) for cause in owned_causes):
            raise TypeError("LifecycleError.causes: must contain exceptions")
        if code in _CAUSE_REQUIRED and not owned_causes:
            raise ValueError(f"LifecycleError.causes: {code} requires a cause")
        if code in _CAUSE_FORBIDDEN and owned_causes:
            raise ValueError(f"LifecycleError.causes: {code} does not accept causes")
        super().__init__(message)
        self._code = code
        self._causes = owned_causes
        if owned_causes:
            self.__cause__ = owned_causes[0]

    @property
    def code(self) -> LifecycleErrorCode:
        return self._code

    @property
    def causes(self) -> tuple[BaseException, ...]:
        return self._causes
