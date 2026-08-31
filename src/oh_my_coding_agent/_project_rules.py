from __future__ import annotations

from dataclasses import dataclass
import os
import stat
from typing import Literal

from ._resource_state import (
    ProjectResourceState,
    ProjectRuleResolution,
    ResourceAdmissionError,
    ResourceResolutionReport,
)


@dataclass(frozen=True, slots=True)
class ProjectRule:
    path: str
    content: str


@dataclass(frozen=True, slots=True)
class ProjectRuleObservation:
    path: str
    content: str | None
    stage: Literal["read", "encoding"] | None


@dataclass(frozen=True, slots=True)
class ProjectRuleSnapshot:
    trusted: bool
    observations: tuple[ProjectRuleObservation, ...] = ()

    @property
    def rules(self) -> tuple[ProjectRule, ...]:
        return tuple(
            ProjectRule(path=item.path, content=item.content)
            for item in self.observations
            if item.content is not None
        )

    def state(self) -> ProjectResourceState:
        if not self.trusted:
            report = ResourceResolutionReport(
                discovery="disabled",
                projectRules=(),
                skills=(),
                promptTemplates=(),
            )
        else:
            report = ResourceResolutionReport(
                discovery="enabled",
                projectRules=tuple(
                    ProjectRuleResolution(
                        path=item.path,
                        disposition=(
                            "effective" if item.content is not None else "soft_skipped"
                        ),
                        validationStage=item.stage,
                    )
                    for item in self.observations
                ),
                skills=(),
                promptTemplates=(),
            )
        return ProjectResourceState(
            status="current",
            report=report,
            extensionDiagnostics=(),
        )


def load_project_rules(cwd: str, project_trusted: bool) -> ProjectRuleSnapshot:
    if not project_trusted:
        return ProjectRuleSnapshot(trusted=False)
    observations: list[ProjectRuleObservation] = []
    for directory in _discovery_chain(cwd):
        path = os.path.join(directory, "AGENTS.md")
        kind = _lstat_kind(path)
        if kind is None:
            continue
        if kind != "file":
            raise ResourceAdmissionError(
                kind="project_rule",
                path=path,
                stage="structure",
            )
        observation = _read_rule(path)
        observations.append(observation)
    return ProjectRuleSnapshot(trusted=True, observations=tuple(observations))


def _read_rule(path: str) -> ProjectRuleObservation:
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        content = raw.decode("utf-8")
    except OSError:
        return ProjectRuleObservation(path=path, content=None, stage="read")
    except UnicodeDecodeError:
        return ProjectRuleObservation(path=path, content=None, stage="encoding")
    if content.startswith("\ufeff") or "\0" in content:
        return ProjectRuleObservation(path=path, content=None, stage="encoding")
    return ProjectRuleObservation(path=path, content=content, stage=None)


def _discovery_chain(cwd: str) -> tuple[str, ...]:
    root = _project_root(cwd)
    if root is None:
        return (cwd,)
    chain: list[str] = []
    current = cwd
    while True:
        chain.append(current)
        if current == root:
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    chain.reverse()
    return tuple(chain)


def _project_root(cwd: str) -> str | None:
    current = cwd
    while True:
        kind = _lstat_kind(os.path.join(current, ".git"))
        if kind in {"directory", "file"}:
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _lstat_kind(path: str) -> str | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if stat.S_ISREG(info.st_mode):
        return "file"
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    return "other"
