from __future__ import annotations

from dataclasses import dataclass
import os
import stat

from ._resource_state import (
    ProjectResourceState,
    ProjectRuleResolution,
    ResourceResolutionReport,
)


@dataclass(frozen=True, slots=True)
class ProjectRule:
    path: str
    content: str


@dataclass(frozen=True, slots=True)
class ProjectRuleSnapshot:
    trusted: bool
    rules: tuple[ProjectRule, ...]

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
                        path=rule.path,
                        disposition="effective",
                        validationStage=None,
                    )
                    for rule in self.rules
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
        return ProjectRuleSnapshot(trusted=False, rules=())
    path = os.path.join(cwd, "AGENTS.md")
    kind = _lstat_kind(path)
    if kind != "file":
        return ProjectRuleSnapshot(trusted=True, rules=())
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        content = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return ProjectRuleSnapshot(trusted=True, rules=())
    return ProjectRuleSnapshot(
        trusted=True,
        rules=(ProjectRule(path=path, content=content),),
    )


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
    return "other"
