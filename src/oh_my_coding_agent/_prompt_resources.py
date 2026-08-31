from __future__ import annotations

from dataclasses import dataclass
import os
import re
import stat
from typing import Final, Literal

from ._frontmatter import (
    FrontmatterError,
    FrontmatterScalar,
    parse_frontmatter_mapping,
    split_frontmatter,
)
from ._project_rules import _discovery_chain
from ._resource_state import (
    PromptResourceCandidate,
    PromptResourceResolution,
    ResourceAdmissionError,
)


_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_NAME_LENGTH = 64
_MAX_DESCRIPTION_LENGTH = 1024
_MAX_ARGUMENT_HINT_LENGTH = 256
_SKILL_PREFIX = "/skill:"
_TEMPLATE_SUB = re.compile(
    r"\$\{([0-9]+):-([^}]*)\}|\$\{@:([0-9]+)(?::([0-9]+))?\}|\$(ARGUMENTS|@|[0-9]+)"
)
_ES_WHITESPACE: Final[frozenset[str]] = frozenset(
    {
        "\t",
        "\n",
        "\v",
        "\f",
        "\r",
        " ",
        "\u00a0",
        "\u1680",
        *map(chr, range(0x2000, 0x200B)),
        "\u2028",
        "\u2029",
        "\u202f",
        "\u205f",
        "\u3000",
        "\ufeff",
    }
)


@dataclass(frozen=True, slots=True)
class SkillResource:
    name: str
    description: str
    body: str
    location: str
    disable_model_invocation: bool


@dataclass(frozen=True, slots=True)
class TemplateResource:
    name: str
    description: str
    body: str
    argument_hint: str | None


@dataclass(frozen=True, slots=True)
class _PromptResourceSource:
    name: str
    source: Literal["omh", "agents"]
    path: str
    relative: str
    directory_rank: int


@dataclass(frozen=True, slots=True)
class PromptResourceSnapshot:
    skills: tuple[SkillResource, ...]
    templates: tuple[TemplateResource, ...]
    skill_resolutions: tuple[PromptResourceResolution, ...] = ()
    template_resolutions: tuple[PromptResourceResolution, ...] = ()

    def skill(self, name: str) -> SkillResource | None:
        for resource in self.skills:
            if resource.name == name:
                return resource
        return None

    def template(self, name: str) -> TemplateResource | None:
        for resource in self.templates:
            if resource.name == name:
                return resource
        return None


EMPTY_SNAPSHOT = PromptResourceSnapshot(skills=(), templates=())


def is_resource_name(name: str) -> bool:
    return (
        bool(name)
        and len(name) <= _MAX_NAME_LENGTH
        and _NAME_PATTERN.fullmatch(name) is not None
    )


def load_prompt_resources(cwd: str, project_trusted: bool) -> PromptResourceSnapshot:
    if not project_trusted:
        return EMPTY_SNAPSHOT
    skill_sources: list[_PromptResourceSource] = []
    template_sources: list[_PromptResourceSource] = []
    for directory_rank, directory in enumerate(_discovery_chain(cwd)):
        omh = _admit_dir(os.path.join(directory, ".omh"))
        if omh is not None:
            skill_sources.extend(
                _discover_skills(
                    os.path.join(omh, "skills"),
                    source="omh",
                    prefix=".omh/skills",
                    directory_rank=directory_rank,
                )
            )
            template_sources.extend(
                _discover_templates(
                    os.path.join(omh, "prompts"),
                    directory_rank=directory_rank,
                )
            )
        agents = _admit_dir(os.path.join(directory, ".agents"))
        if agents is not None:
            skill_sources.extend(
                _discover_skills(
                    os.path.join(agents, "skills"),
                    source="agents",
                    prefix=".agents/skills",
                    directory_rank=directory_rank,
                )
            )
    skills, skill_resolutions = _select_skills(skill_sources, cwd=cwd)
    templates, template_resolutions = _select_templates(template_sources)
    return PromptResourceSnapshot(
        skills=skills,
        templates=templates,
        skill_resolutions=skill_resolutions,
        template_resolutions=template_resolutions,
    )


def expand_prompt(
    text: str, snapshot: PromptResourceSnapshot, *, enabled: bool
) -> str:
    if not enabled or not text.startswith("/"):
        return text
    if text.startswith(_SKILL_PREFIX):
        expanded = _expand_skill(text[len(_SKILL_PREFIX) :], snapshot)
    else:
        expanded = _expand_template(text[1:], snapshot)
    return text if expanded is None else expanded


def _expand_skill(rest: str, snapshot: PromptResourceSnapshot) -> str | None:
    name, suffix = _split_name(rest)
    if not is_resource_name(name):
        return None
    skill = snapshot.skill(name)
    if skill is None:
        return None
    skill_directory = skill.location.rsplit("/", 1)[0]
    block = (
        f'<skill name="{skill.name}" location="{skill.location}">\n'
        f"References are relative to {skill_directory}.\n"
        f"\n"
        f"{skill.body}\n"
        f"</skill>"
    )
    argument = _es_trim(suffix)
    if argument:
        return f"{block}\n\n{argument}"
    return block


def _expand_template(rest: str, snapshot: PromptResourceSnapshot) -> str | None:
    name, suffix = _split_name(rest)
    if not is_resource_name(name):
        return None
    template = snapshot.template(name)
    if template is None:
        return None
    return substitute_args(template.body, parse_command_args(suffix))


def _split_name(rest: str) -> tuple[str, str]:
    for index, char in enumerate(rest):
        if char in _ES_WHITESPACE:
            return rest[:index], rest[index + 1 :]
    return rest, ""


def parse_command_args(args_string: str) -> list[str]:
    args: list[str] = []
    current = ""
    in_quote: str | None = None
    for char in args_string:
        if in_quote is not None:
            if char == in_quote:
                in_quote = None
            else:
                current += char
        elif char in {'"', "'"}:
            in_quote = char
        elif char in _ES_WHITESPACE:
            if current:
                args.append(current)
                current = ""
        else:
            current += char
    if current:
        args.append(current)
    return args


def substitute_args(content: str, args: list[str]) -> str:
    all_args = " ".join(args)

    def replace(match: re.Match[str]) -> str:
        default_num, default_value, slice_start, slice_length, simple = match.groups()
        if default_num is not None:
            index = int(default_num) - 1
            if index < 0 or index >= len(args):
                return default_value
            value = args[index]
            return value if value else default_value
        if slice_start is not None:
            start = int(slice_start) - 1
            if start < 0:
                start = 0
            if slice_length is not None:
                length = int(slice_length)
                return " ".join(args[start : start + length])
            return " ".join(args[start:])
        if simple in {"ARGUMENTS", "@"}:
            return all_args
        index = int(simple) - 1
        if index < 0 or index >= len(args):
            return ""
        return args[index]

    return _TEMPLATE_SUB.sub(replace, content)


def _discover_skills(
    skills_dir: str,
    *,
    source: Literal["omh", "agents"],
    prefix: str,
    directory_rank: int,
) -> tuple[_PromptResourceSource, ...]:
    kind = _lstat_kind(skills_dir)
    if kind is None:
        return ()
    if kind != "dir":
        raise ResourceAdmissionError(
            kind="project_resources",
            path=os.path.abspath(skills_dir),
            stage="structure",
        )
    names = _direct_names(skills_dir)
    selected: list[_PromptResourceSource] = []
    for name in names:
        entry = os.path.join(skills_dir, name)
        relative = f"{prefix}/{name}/SKILL.md"
        entry_kind = _lstat_kind(entry)
        if entry_kind != "dir":
            if not is_resource_name(name):
                continue
            raise ResourceAdmissionError(
                kind="skill",
                name=name,
                path=os.path.abspath(os.path.join(entry, "SKILL.md")),
                stage="structure",
            )
        skill_file = os.path.join(entry, "SKILL.md")
        file_kind = _lstat_kind(skill_file)
        if file_kind != "file":
            raise ResourceAdmissionError(
                kind="skill",
                name=name,
                path=os.path.abspath(skill_file),
                stage="structure",
            )
        selected.append(
            _PromptResourceSource(
                name=name,
                source=source,
                path=skill_file,
                relative=relative,
                directory_rank=directory_rank,
            )
        )
    return tuple(selected)


def _discover_templates(
    prompts_dir: str, *, directory_rank: int
) -> tuple[_PromptResourceSource, ...]:
    kind = _lstat_kind(prompts_dir)
    if kind is None:
        return ()
    if kind != "dir":
        raise ResourceAdmissionError(
            kind="project_resources",
            path=os.path.abspath(prompts_dir),
            stage="structure",
        )
    names = _direct_names(prompts_dir)
    selected: list[_PromptResourceSource] = []
    for filename in names:
        if not filename.endswith(".md"):
            continue
        stem = filename[: -len(".md")]
        path = os.path.join(prompts_dir, filename)
        relative = f".omh/prompts/{filename}"
        file_kind = _lstat_kind(path)
        if file_kind != "file":
            raise ResourceAdmissionError(
                kind="prompt_template",
                name=stem,
                path=os.path.abspath(path),
                stage="structure",
            )
        selected.append(
            _PromptResourceSource(
                name=stem,
                source="omh",
                path=path,
                relative=relative,
                directory_rank=directory_rank,
            )
        )
    return tuple(selected)


def _select_skills(
    sources: list[_PromptResourceSource], *, cwd: str
) -> tuple[tuple[SkillResource, ...], tuple[PromptResourceResolution, ...]]:
    groups = _group_sources(sources)
    skills: list[SkillResource] = []
    resolutions: list[PromptResourceResolution] = []
    for name in sorted(groups):
        candidates = groups[name]
        winner = candidates[0]
        text = _read_utf8(winner, "skill")
        try:
            skill = _parse_skill(
                name,
                text,
                winner.relative,
                _cwd_relative(winner.path, cwd),
            )
        except ValueError:
            skill = None
        if skill is None:
            raise _source_error(winner, "skill", "document")
        skills.append(skill)
        resolutions.append(_resolution("skill", name, candidates))
    return tuple(skills), tuple(resolutions)


def _select_templates(
    sources: list[_PromptResourceSource],
) -> tuple[tuple[TemplateResource, ...], tuple[PromptResourceResolution, ...]]:
    groups = _group_sources(sources)
    templates: list[TemplateResource] = []
    resolutions: list[PromptResourceResolution] = []
    for name in sorted(groups):
        candidates = groups[name]
        winner = candidates[0]
        text = _read_utf8(winner, "prompt_template")
        try:
            template = _parse_template(name, text, winner.relative)
        except ValueError:
            template = None
        if template is None:
            raise _source_error(winner, "prompt_template", "document")
        templates.append(template)
        resolutions.append(_resolution("prompt_template", name, candidates))
    return tuple(templates), tuple(resolutions)


def _group_sources(
    sources: list[_PromptResourceSource],
) -> dict[str, list[_PromptResourceSource]]:
    groups: dict[str, list[_PromptResourceSource]] = {}
    for candidate in sources:
        groups.setdefault(candidate.name, []).append(candidate)
    for candidates in groups.values():
        candidates.sort(
            key=lambda candidate: (
                candidate.directory_rank,
                candidate.source == "omh",
            ),
            reverse=True,
        )
    return groups


def _resolution(
    kind: Literal["skill", "prompt_template"],
    name: str,
    sources: list[_PromptResourceSource],
) -> PromptResourceResolution:
    return PromptResourceResolution(
        kind=kind,
        name=name,
        candidates=tuple(
            PromptResourceCandidate(
                source=source.source,
                path=os.path.abspath(source.path),
                disposition="effective" if index == 0 else "shadowed",
            )
            for index, source in enumerate(sources)
        ),
    )


def _parse_skill(
    directory_name: str, text: str, relative: str, location: str
) -> SkillResource:
    mapping, body = _document_fields(text, relative, "Skill")
    allowed = {"name", "description", "disable-model-invocation"}
    if set(mapping) - allowed:
        raise ValueError(f'Skill "{relative}" is invalid')
    name_scalar = mapping.get("name")
    if name_scalar is None or not name_scalar.is_string():
        raise ValueError(f'Skill "{relative}" is invalid')
    if name_scalar.text != directory_name or not is_resource_name(name_scalar.text):
        raise ValueError(f'Skill "{relative}" is invalid')
    description = _required_description(mapping.get("description"), relative, "Skill")
    disable = False
    flag = mapping.get("disable-model-invocation")
    if flag is not None:
        parsed = flag.as_bool()
        if parsed is None:
            raise ValueError(f'Skill "{relative}" is invalid')
        disable = parsed
    return SkillResource(
        name=name_scalar.text,
        description=description,
        body=body,
        location=location,
        disable_model_invocation=disable,
    )


def _parse_template(name: str, text: str, relative: str) -> TemplateResource:
    if not is_resource_name(name):
        raise ValueError(f'Prompt Template "{relative}" is invalid')
    mapping, body = _document_fields(text, relative, "Prompt Template")
    allowed = {"description", "argument-hint"}
    if set(mapping) - allowed:
        raise ValueError(f'Prompt Template "{relative}" is invalid')
    description = _required_description(
        mapping.get("description"), relative, "Prompt Template"
    )
    hint_scalar = mapping.get("argument-hint")
    argument_hint: str | None = None
    if hint_scalar is not None:
        if not hint_scalar.is_string():
            raise ValueError(f'Prompt Template "{relative}" is invalid')
        argument_hint = _es_trim(hint_scalar.text)
        if (
            not argument_hint
            or "\n" in argument_hint
            or "\r" in argument_hint
            or len(argument_hint) > _MAX_ARGUMENT_HINT_LENGTH
        ):
            raise ValueError(f'Prompt Template "{relative}" is invalid')
    return TemplateResource(
        name=name,
        description=description,
        body=body,
        argument_hint=argument_hint,
    )


def _document_fields(
    text: str, relative: str, kind: str
) -> tuple[dict[str, FrontmatterScalar], str]:
    split = split_frontmatter(text)
    if split is None:
        raise ValueError(f'{kind} "{relative}" is invalid')
    yaml_string, body = split
    try:
        mapping = parse_frontmatter_mapping(yaml_string)
    except FrontmatterError as error:
        raise ValueError(f'{kind} "{relative}" is invalid') from error
    trimmed = _es_trim(body)
    if not trimmed:
        raise ValueError(f'{kind} "{relative}" is invalid')
    return mapping, trimmed


def _required_description(
    scalar: FrontmatterScalar | None, relative: str, kind: str
) -> str:
    if scalar is None or not scalar.is_string():
        raise ValueError(f'{kind} "{relative}" is invalid')
    description = _es_trim(scalar.text)
    if (
        not description
        or "\n" in description
        or "\r" in description
        or len(description) > _MAX_DESCRIPTION_LENGTH
    ):
        raise ValueError(f'{kind} "{relative}" is invalid')
    return description


def _read_utf8(
    source: _PromptResourceSource,
    kind: Literal["skill", "prompt_template"],
) -> str:
    read_failed = False
    try:
        with open(source.path, "rb") as handle:
            raw = handle.read()
    except OSError:
        read_failed = True
        raw = b""
    if read_failed:
        raise _source_error(source, kind, "read")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _source_error(source, kind, "encoding")
    encoding_failed = False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        encoding_failed = True
        text = ""
    if encoding_failed or "\0" in text:
        raise _source_error(source, kind, "encoding")
    return text


def _direct_names(path: str) -> list[str]:
    read_failed = False
    try:
        names = sorted(
            entry.name
            for entry in os.scandir(path)
            if not entry.name.startswith(".")
        )
    except OSError:
        read_failed = True
        names = []
    if read_failed:
        raise ResourceAdmissionError(
            kind="project_resources",
            path=os.path.abspath(path),
            stage="read",
        )
    return names


def _lstat_kind(path: str) -> str | None:
    read_failed = False
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        read_failed = True
        info = None
    if read_failed:
        raise ResourceAdmissionError(
            kind="project_resources",
            path=os.path.abspath(path),
            stage="read",
        )
    assert info is not None
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if stat.S_ISDIR(info.st_mode):
        return "dir"
    if stat.S_ISREG(info.st_mode):
        return "file"
    return "other"


def _admit_dir(path: str) -> str | None:
    kind = _lstat_kind(path)
    if kind is None:
        return None
    if kind != "dir":
        raise ResourceAdmissionError(
            kind="project_resources",
            path=os.path.abspath(path),
            stage="structure",
        )
    return path


def _cwd_relative(path: str, cwd: str) -> str:
    return os.path.relpath(path, cwd).replace(os.sep, "/")


def _source_error(
    source: _PromptResourceSource,
    kind: Literal["skill", "prompt_template"],
    stage: Literal["read", "encoding", "document"],
) -> ResourceAdmissionError:
    return ResourceAdmissionError(
        kind=kind,
        name=source.name,
        path=os.path.abspath(source.path),
        stage=stage,
    )


def _es_trim(value: str) -> str:
    start = 0
    end = len(value)
    while start < end and value[start] in _ES_WHITESPACE:
        start += 1
    while end > start and value[end - 1] in _ES_WHITESPACE:
        end -= 1
    return value[start:end]
