from __future__ import annotations

from dataclasses import MISSING, dataclass, fields
import os
from typing import Any, ClassVar, Literal, NoReturn, cast, final


_STATUS = frozenset({"current", "indeterminate"})
_DISCOVERY = frozenset({"enabled", "disabled"})
_RULE_DISPOSITION = frozenset({"effective", "soft_skipped"})
_RULE_STAGE = frozenset({"read", "encoding"})
_RESOURCE_KIND = frozenset({"skill", "prompt_template"})
_CANDIDATE_SOURCE = frozenset({"omh", "agents"})
_CANDIDATE_DISPOSITION = frozenset({"effective", "shadowed"})
_LOAD_PHASE = frozenset({"load", "factory"})
_LIFECYCLE_EVENT = frozenset(
    {"session_start", "resources_discover", "session_shutdown"}
)


class _PublicValueMeta(type):
    def __new__(
        mcls,
        name: str,
        bases: tuple[type[Any], ...],
        namespace: dict[str, Any],
        **kwargs: Any,
    ) -> _PublicValueMeta:
        if any(isinstance(base, _PublicValueMeta) for base in bases):
            raise TypeError(f"{bases[0].__name__} is final")
        return super().__new__(mcls, name, bases, namespace, **kwargs)

    def __call__(cls, *args: object, **kwargs: object) -> object:
        type_name = cls.__name__
        if args:
            raise TypeError(
                f"{type_name}.<constructor>: positional arguments are not admitted"
            )
        record_fields = fields(cast(Any, cls))
        init_fields = {item.name: item for item in record_fields if item.init}
        for name in kwargs:
            if name not in init_fields:
                raise TypeError(f"{type_name}.{name}: unknown field")
        for item in record_fields:
            if (
                item.init
                and item.name not in kwargs
                and item.default is MISSING
                and item.default_factory is MISSING
            ):
                raise TypeError(f"{type_name}.{item.name}: missing required field")
        return super().__call__(**kwargs)


def _fail_type(type_name: str, path: str, explanation: str) -> NoReturn:
    raise TypeError(f"{type_name}.{path}: {explanation}")


def _fail_value(type_name: str, path: str, explanation: str) -> NoReturn:
    raise ValueError(f"{type_name}.{path}: {explanation}")


def _string(value: object, type_name: str, path: str) -> str:
    if type(value) is not str:
        _fail_type(type_name, path, "must be a string")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        _fail_value(type_name, path, "must contain only Unicode scalar values")
    return value


def _optional_string(value: object, type_name: str, path: str) -> str | None:
    if value is None:
        return None
    return _string(value, type_name, path)


def _absolute_path(value: object, type_name: str, path: str) -> str:
    text = _string(value, type_name, path)
    if not os.path.isabs(text):
        _fail_value(type_name, path, "must be a lexical absolute path")
    return text


def _literal(
    value: object,
    allowed: frozenset[str],
    type_name: str,
    path: str,
) -> str:
    text = _string(value, type_name, path)
    if text not in allowed:
        _fail_value(type_name, path, "must be an admitted literal")
    return text


def _sequence(
    value: object,
    allowed: tuple[type[object], ...],
    type_name: str,
    path: str,
) -> tuple[Any, ...]:
    if type(value) not in (list, tuple):
        _fail_type(type_name, path, "must be a list or tuple")
    result: tuple[Any, ...] = tuple(cast(list[Any] | tuple[Any, ...], value))
    for index, item in enumerate(result):
        if type(item) not in allowed:
            _fail_type(type_name, f"{path}[{index}]", "has an invalid record carrier")
    return result


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectRuleResolution(metaclass=_PublicValueMeta):
    path: str
    disposition: Literal["effective", "soft_skipped"]
    validationStage: Literal["read", "encoding"] | None

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        object.__setattr__(self, "path", _absolute_path(self.path, type_name, "path"))
        object.__setattr__(
            self,
            "disposition",
            _literal(self.disposition, _RULE_DISPOSITION, type_name, "disposition"),
        )
        if self.disposition == "effective":
            if self.validationStage is not None:
                _fail_value(
                    type_name, "validationStage", "must be None for effective rules"
                )
            return
        if self.validationStage is None:
            _fail_value(
                type_name, "validationStage", "must be read or encoding for a skip"
            )
        object.__setattr__(
            self,
            "validationStage",
            _literal(self.validationStage, _RULE_STAGE, type_name, "validationStage"),
        )


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class PromptResourceCandidate(metaclass=_PublicValueMeta):
    source: Literal["omh", "agents"]
    path: str
    disposition: Literal["effective", "shadowed"]

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        object.__setattr__(
            self,
            "source",
            _literal(self.source, _CANDIDATE_SOURCE, type_name, "source"),
        )
        object.__setattr__(self, "path", _absolute_path(self.path, type_name, "path"))
        object.__setattr__(
            self,
            "disposition",
            _literal(
                self.disposition, _CANDIDATE_DISPOSITION, type_name, "disposition"
            ),
        )


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class PromptResourceResolution(metaclass=_PublicValueMeta):
    kind: Literal["skill", "prompt_template"]
    name: str
    candidates: tuple[PromptResourceCandidate, ...]

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        object.__setattr__(
            self, "kind", _literal(self.kind, _RESOURCE_KIND, type_name, "kind")
        )
        object.__setattr__(self, "name", _string(self.name, type_name, "name"))
        candidates = _sequence(
            self.candidates, (PromptResourceCandidate,), type_name, "candidates"
        )
        object.__setattr__(self, "candidates", candidates)
        if not candidates:
            _fail_value(type_name, "candidates", "must be nonempty")
        if candidates[0].disposition != "effective":
            _fail_value(
                type_name, "candidates[0].disposition", "must be the effective winner"
            )
        for index, candidate in enumerate(candidates[1:], start=1):
            if candidate.disposition != "shadowed":
                _fail_value(
                    type_name,
                    f"candidates[{index}].disposition",
                    "must be shadowed",
                )
        if self.kind == "prompt_template":
            for index, candidate in enumerate(candidates):
                if candidate.source != "omh":
                    _fail_value(
                        type_name,
                        f"candidates[{index}].source",
                        "must be omh for a Prompt Template",
                    )


class ExtensionDiagnostic:
    __slots__ = ()
    Load: ClassVar[type[ExtensionDiagnosticLoad]]
    Lifecycle: ClassVar[type[ExtensionDiagnosticLifecycle]]

    def __new__(cls, *args: object, **kwargs: object) -> ExtensionDiagnostic:
        del args, kwargs
        if cls is ExtensionDiagnostic:
            raise TypeError("ExtensionDiagnostic is a sealed diagnostic base")
        return super().__new__(cls)

    def __init_subclass__(cls) -> None:
        if cls.__module__ != __name__:
            raise TypeError("ExtensionDiagnostic variants are sealed")
        super().__init_subclass__()


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class ExtensionDiagnosticLoad(ExtensionDiagnostic, metaclass=_PublicValueMeta):
    path: str
    phase: Literal["load", "factory"]
    message: str

    def __post_init__(self) -> None:
        type_name = "ExtensionDiagnostic.Load"
        object.__setattr__(self, "path", _absolute_path(self.path, type_name, "path"))
        object.__setattr__(
            self, "phase", _literal(self.phase, _LOAD_PHASE, type_name, "phase")
        )
        object.__setattr__(
            self, "message", _string(self.message, type_name, "message")
        )


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class ExtensionDiagnosticLifecycle(ExtensionDiagnostic, metaclass=_PublicValueMeta):
    path: str
    eventType: Literal["session_start", "resources_discover", "session_shutdown"]
    message: str
    stack: str | None = None

    def __post_init__(self) -> None:
        type_name = "ExtensionDiagnostic.Lifecycle"
        object.__setattr__(self, "path", _absolute_path(self.path, type_name, "path"))
        object.__setattr__(
            self,
            "eventType",
            _literal(self.eventType, _LIFECYCLE_EVENT, type_name, "eventType"),
        )
        object.__setattr__(
            self, "message", _string(self.message, type_name, "message")
        )
        object.__setattr__(
            self, "stack", _optional_string(self.stack, type_name, "stack")
        )


ExtensionDiagnostic.Load = ExtensionDiagnosticLoad
ExtensionDiagnostic.Lifecycle = ExtensionDiagnosticLifecycle
ExtensionDiagnosticLoad.__name__ = "Load"
ExtensionDiagnosticLoad.__qualname__ = "ExtensionDiagnostic.Load"
ExtensionDiagnosticLifecycle.__name__ = "Lifecycle"
ExtensionDiagnosticLifecycle.__qualname__ = "ExtensionDiagnostic.Lifecycle"


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceResolutionReport(metaclass=_PublicValueMeta):
    discovery: Literal["enabled", "disabled"]
    projectRules: tuple[ProjectRuleResolution, ...]
    skills: tuple[PromptResourceResolution, ...]
    promptTemplates: tuple[PromptResourceResolution, ...]

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        object.__setattr__(
            self,
            "discovery",
            _literal(self.discovery, _DISCOVERY, type_name, "discovery"),
        )
        object.__setattr__(
            self,
            "projectRules",
            _sequence(
                self.projectRules, (ProjectRuleResolution,), type_name, "projectRules"
            ),
        )
        object.__setattr__(
            self,
            "skills",
            _sequence(self.skills, (PromptResourceResolution,), type_name, "skills"),
        )
        object.__setattr__(
            self,
            "promptTemplates",
            _sequence(
                self.promptTemplates,
                (PromptResourceResolution,),
                type_name,
                "promptTemplates",
            ),
        )
        if self.discovery == "disabled" and (
            self.projectRules or self.skills or self.promptTemplates
        ):
            _fail_value(
                type_name, "discovery", "disabled reports must have empty collections"
            )
        for index, group in enumerate(self.skills):
            if group.kind != "skill":
                _fail_value(type_name, f"skills[{index}].kind", "must be skill")
        if any(
            previous.name >= current.name
            for previous, current in zip(self.skills, self.skills[1:])
        ):
            _fail_value(
                type_name,
                "skills",
                "must be strictly ordered by Unicode name",
            )
        for index, group in enumerate(self.promptTemplates):
            if group.kind != "prompt_template":
                _fail_value(
                    type_name,
                    f"promptTemplates[{index}].kind",
                    "must be prompt_template",
                )
        if any(
            previous.name >= current.name
            for previous, current in zip(
                self.promptTemplates, self.promptTemplates[1:]
            )
        ):
            _fail_value(
                type_name,
                "promptTemplates",
                "must be strictly ordered by Unicode name",
            )


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectResourceState(metaclass=_PublicValueMeta):
    status: Literal["current", "indeterminate"]
    report: ResourceResolutionReport
    extensionDiagnostics: tuple[ExtensionDiagnostic, ...]

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        object.__setattr__(
            self, "status", _literal(self.status, _STATUS, type_name, "status")
        )
        if type(self.report) is not ResourceResolutionReport:
            _fail_type(type_name, "report", "must be a ResourceResolutionReport")
        diagnostics = _sequence(
            self.extensionDiagnostics,
            (ExtensionDiagnosticLoad, ExtensionDiagnosticLifecycle),
            type_name,
            "extensionDiagnostics",
        )
        object.__setattr__(self, "extensionDiagnostics", diagnostics)


_RELOAD_STATUS = frozenset({"clean", "partial"})


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectResourceReloadResult(metaclass=_PublicValueMeta):
    status: Literal["clean", "partial"]
    state: ProjectResourceState
    diagnostics: tuple[ExtensionDiagnostic, ...]

    def __post_init__(self) -> None:
        type_name = type(self).__name__
        object.__setattr__(
            self, "status", _literal(self.status, _RELOAD_STATUS, type_name, "status")
        )
        if type(self.state) is not ProjectResourceState:
            _fail_type(type_name, "state", "must be a ProjectResourceState")
        diagnostics = _sequence(
            self.diagnostics,
            (ExtensionDiagnosticLoad, ExtensionDiagnosticLifecycle),
            type_name,
            "diagnostics",
        )
        object.__setattr__(self, "diagnostics", diagnostics)
        if self.state.status != "current":
            _fail_value(type_name, "state", "must be a current ProjectResourceState")
        if self.status == "clean":
            if diagnostics:
                _fail_value(
                    type_name, "status", "must be partial when diagnostics are present"
                )
            return
        if not diagnostics:
            _fail_value(
                type_name, "status", "must be clean when diagnostics are empty"
            )


_ADMISSION_KINDS = frozenset(
    {
        "project_resources",
        "project_rule",
        "skill",
        "prompt_template",
        "python_extension",
    }
)
_ADMISSION_STAGES = frozenset({"structure", "read", "encoding", "document"})
_TRUST_OPERATIONS = frozenset({"resolve", "update"})
_TRUST_STAGES = frozenset({"lock", "read", "encoding", "document", "write"})


@final
class ResourceAdmissionError(RuntimeError):
    __slots__ = ("_kind", "_name", "_path", "_stage")

    def __init__(
        self,
        *,
        kind: Literal[
            "project_resources",
            "project_rule",
            "skill",
            "prompt_template",
            "python_extension",
        ],
        path: str,
        stage: Literal["structure", "read", "encoding", "document"],
        name: str | None = None,
    ) -> None:
        kind_text = _literal(
            kind, _ADMISSION_KINDS, "ResourceAdmissionError", "kind"
        )
        stage_text = _literal(
            stage, _ADMISSION_STAGES, "ResourceAdmissionError", "stage"
        )
        path_text = _absolute_path(path, "ResourceAdmissionError", "path")
        if name is not None:
            name = _string(name, "ResourceAdmissionError", "name")
            if kind_text not in {"skill", "prompt_template"}:
                _fail_value(
                    "ResourceAdmissionError",
                    "name",
                    "is admitted only for a Prompt Resource",
                )
        super().__init__("Project resource admission failed")
        self._kind = kind_text
        self._name = name
        self._path = path_text
        self._stage = stage_text

    def __init_subclass__(cls) -> None:
        raise TypeError("ResourceAdmissionError is final")

    @property
    def kind(
        self,
    ) -> Literal[
        "project_resources",
        "project_rule",
        "skill",
        "prompt_template",
        "python_extension",
    ]:
        return cast(
            Literal[
                "project_resources",
                "project_rule",
                "skill",
                "prompt_template",
                "python_extension",
            ],
            self._kind,
        )

    @property
    def name(self) -> str | None:
        return self._name

    @property
    def path(self) -> str:
        return self._path

    @property
    def stage(self) -> Literal["structure", "read", "encoding", "document"]:
        return cast(
            Literal["structure", "read", "encoding", "document"],
            self._stage,
        )


@final
class TrustPolicyError(RuntimeError):
    __slots__ = ("_operation", "_stage")

    def __init__(
        self,
        *,
        operation: Literal["resolve", "update"],
        stage: Literal["lock", "read", "encoding", "document", "write"],
    ) -> None:
        operation_text = _literal(
            operation,
            _TRUST_OPERATIONS,
            "TrustPolicyError",
            "operation",
        )
        stage_text = _literal(
            stage,
            _TRUST_STAGES,
            "TrustPolicyError",
            "stage",
        )
        super().__init__("Project trust policy failed")
        self._operation = operation_text
        self._stage = stage_text

    def __init_subclass__(cls) -> None:
        raise TypeError("TrustPolicyError is final")

    @property
    def operation(self) -> Literal["resolve", "update"]:
        return cast(Literal["resolve", "update"], self._operation)

    @property
    def stage(self) -> Literal["lock", "read", "encoding", "document", "write"]:
        return cast(
            Literal["lock", "read", "encoding", "document", "write"],
            self._stage,
        )


@final
class ProjectResourceReloadError(RuntimeError):
    __slots__ = ("_state", "_diagnostics")

    def __init__(
        self,
        *,
        state: ProjectResourceState,
        diagnostics: tuple[ExtensionDiagnostic, ...] | list[ExtensionDiagnostic] = (),
    ) -> None:
        if type(state) is not ProjectResourceState:
            _fail_type(
                "ProjectResourceReloadError",
                "state",
                "must be a ProjectResourceState",
            )
        if state.status != "indeterminate":
            _fail_value(
                "ProjectResourceReloadError",
                "state",
                "must be an indeterminate ProjectResourceState",
            )
        copied = _sequence(
            diagnostics,
            (ExtensionDiagnosticLoad, ExtensionDiagnosticLifecycle),
            "ProjectResourceReloadError",
            "diagnostics",
        )
        super().__init__("Project resource reload failed")
        self._state = state
        self._diagnostics = copied

    def __init_subclass__(cls) -> None:
        raise TypeError("ProjectResourceReloadError is final")

    @property
    def state(self) -> ProjectResourceState:
        return self._state

    @property
    def diagnostics(self) -> tuple[ExtensionDiagnostic, ...]:
        return self._diagnostics
