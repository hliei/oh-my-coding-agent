from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Final


_LINE_SPLIT: Final = re.compile(r"(\n|\r\n)")
_CONTEXT_LINES: Final = 4


@dataclass(frozen=True, slots=True)
class EditMatchError:
    code: str
    extra: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class DiffString:
    diff: str
    first_changed_line: int


@dataclass(slots=True)
class _Change:
    count: int
    added: bool
    removed: bool
    previous: _Change | None = None
    value: str = ""


@dataclass(slots=True)
class _DiffPath:
    old_pos: int
    last: _Change | None


def overlapping_starts(haystack: str, needle: str) -> list[int]:
    starts: list[int] = []
    cursor = 0
    while True:
        found = haystack.find(needle, cursor)
        if found < 0:
            return starts
        starts.append(found)
        cursor = found + 1


def apply_literal_edits(
    original: str, edits: Sequence[Mapping[str, str]]
) -> str | EditMatchError:
    matched: list[tuple[int, int, int, str]] = []
    for index, edit in enumerate(edits):
        old_text = edit["oldText"]
        starts = overlapping_starts(original, old_text)
        if not starts:
            return EditMatchError("text_not_found", {"editIndex": index})
        if len(starts) != 1:
            return EditMatchError(
                "text_not_unique",
                {"editIndex": index, "occurrences": len(starts)},
            )
        matched.append((starts[0], len(old_text), index, edit["newText"]))
    matched.sort(key=lambda item: item[0])
    for previous, current in zip(matched, matched[1:]):
        if previous[0] + previous[1] > current[0]:
            return EditMatchError(
                "overlapping_edits",
                {
                    "firstEditIndex": previous[2],
                    "secondEditIndex": current[2],
                },
            )
    pieces: list[str] = []
    cursor = 0
    for start, length, _index, new_text in matched:
        pieces.append(original[cursor:start])
        pieces.append(new_text)
        cursor = start + length
    pieces.append(original[cursor:])
    new_text = "".join(pieces)
    if new_text == original:
        return EditMatchError("no_change", {})
    return new_text


def _tokenize_lines(value: str) -> list[str]:
    parts = _LINE_SPLIT.split(value)
    if parts and parts[-1] == "":
        parts.pop()
    tokens: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 and tokens:
            tokens[-1] += part
        else:
            tokens.append(part)
    return [token for token in tokens if token]


def _extract_common(
    path: _DiffPath,
    new_tokens: Sequence[str],
    old_tokens: Sequence[str],
    diagonal: int,
) -> int:
    old_len = len(old_tokens)
    new_len = len(new_tokens)
    old_pos = path.old_pos
    new_pos = old_pos - diagonal
    common = 0
    while (
        new_pos + 1 < new_len
        and old_pos + 1 < old_len
        and old_tokens[old_pos + 1] == new_tokens[new_pos + 1]
    ):
        new_pos += 1
        old_pos += 1
        common += 1
    if common:
        path.last = _Change(common, False, False, path.last)
    path.old_pos = old_pos
    return new_pos


def _add_to_path(path: _DiffPath, added: bool, removed: bool, old_inc: int) -> _DiffPath:
    last = path.last
    if last is not None and last.added is added and last.removed is removed:
        return _DiffPath(
            path.old_pos + old_inc,
            _Change(last.count + 1, added, removed, last.previous),
        )
    return _DiffPath(
        path.old_pos + old_inc,
        _Change(1, added, removed, last),
    )


def _build_values(
    last: _Change | None, new_tokens: Sequence[str], old_tokens: Sequence[str]
) -> list[_Change]:
    components: list[_Change] = []
    current = last
    while current is not None:
        components.append(current)
        previous = current.previous
        current.previous = None
        current = previous
    components.reverse()
    new_pos = 0
    old_pos = 0
    for component in components:
        if not component.removed:
            component.value = "".join(
                new_tokens[new_pos : new_pos + component.count]
            )
            new_pos += component.count
            if not component.added:
                old_pos += component.count
        else:
            component.value = "".join(
                old_tokens[old_pos : old_pos + component.count]
            )
            old_pos += component.count
    return components


def diff_lines(old_text: str, new_text: str) -> list[_Change]:
    old_tokens = _tokenize_lines(old_text)
    new_tokens = _tokenize_lines(new_text)
    old_len = len(old_tokens)
    new_len = len(new_tokens)
    seed = _DiffPath(-1, None)
    best_path: dict[int, _DiffPath | None] = {0: seed}
    new_pos = _extract_common(seed, new_tokens, old_tokens, 0)
    if seed.old_pos + 1 >= old_len and new_pos + 1 >= new_len:
        return _build_values(seed.last, new_tokens, old_tokens)
    edit_length = 1
    max_edit = old_len + new_len
    min_diagonal = float("-inf")
    max_diagonal = float("inf")
    while edit_length <= max_edit:
        diagonal = int(max(min_diagonal, -edit_length))
        limit = int(min(max_diagonal, edit_length))
        while diagonal <= limit:
            remove_path = best_path.get(diagonal - 1)
            add_path = best_path.get(diagonal + 1)
            if remove_path is not None:
                best_path[diagonal - 1] = None
            can_add = False
            if add_path is not None:
                add_new_pos = add_path.old_pos - diagonal
                can_add = 0 <= add_new_pos < new_len
            can_remove = (
                remove_path is not None and remove_path.old_pos + 1 < old_len
            )
            if not can_add and not can_remove:
                best_path[diagonal] = None
                diagonal += 2
                continue
            if not can_remove or (
                can_add
                and remove_path is not None
                and add_path is not None
                and remove_path.old_pos < add_path.old_pos
            ):
                assert add_path is not None
                base = _add_to_path(add_path, True, False, 0)
            else:
                assert remove_path is not None
                base = _add_to_path(remove_path, False, True, 1)
            new_pos = _extract_common(base, new_tokens, old_tokens, diagonal)
            if base.old_pos + 1 >= old_len and new_pos + 1 >= new_len:
                return _build_values(base.last, new_tokens, old_tokens)
            best_path[diagonal] = base
            if base.old_pos + 1 >= old_len:
                max_diagonal = min(max_diagonal, diagonal - 1)
            if new_pos + 1 >= new_len:
                min_diagonal = max(min_diagonal, diagonal + 1)
            diagonal += 2
        edit_length += 1
    raise RuntimeError("line diff did not converge")


def generate_diff_string(old_content: str, new_content: str) -> DiffString:
    parts = diff_lines(old_content, new_content)
    output: list[str] = []
    old_lines = old_content.split("\n")
    new_lines = new_content.split("\n")
    width = len(str(max(len(old_lines), len(new_lines))))
    old_line = 1
    new_line = 1
    last_was_change = False
    first_changed: int | None = None
    for index, part in enumerate(parts):
        raw = part.value.split("\n")
        if raw and raw[-1] == "":
            raw.pop()
        if part.added or part.removed:
            if first_changed is None:
                first_changed = new_line
            for line in raw:
                if part.added:
                    output.append(f"+{str(new_line).rjust(width)} {line}")
                    new_line += 1
                else:
                    output.append(f"-{str(old_line).rjust(width)} {line}")
                    old_line += 1
            last_was_change = True
            continue
        next_is_change = index < len(parts) - 1 and (
            parts[index + 1].added or parts[index + 1].removed
        )
        leading = last_was_change
        trailing = next_is_change
        if leading and trailing:
            if len(raw) <= _CONTEXT_LINES * 2:
                for line in raw:
                    output.append(f" {str(old_line).rjust(width)} {line}")
                    old_line += 1
                    new_line += 1
            else:
                leading_lines = raw[:_CONTEXT_LINES]
                trailing_lines = raw[-_CONTEXT_LINES:]
                skipped = len(raw) - len(leading_lines) - len(trailing_lines)
                for line in leading_lines:
                    output.append(f" {str(old_line).rjust(width)} {line}")
                    old_line += 1
                    new_line += 1
                output.append(f" {'':>{width}} ...")
                old_line += skipped
                new_line += skipped
                for line in trailing_lines:
                    output.append(f" {str(old_line).rjust(width)} {line}")
                    old_line += 1
                    new_line += 1
        elif leading:
            shown = raw[:_CONTEXT_LINES]
            skipped = len(raw) - len(shown)
            for line in shown:
                output.append(f" {str(old_line).rjust(width)} {line}")
                old_line += 1
                new_line += 1
            if skipped > 0:
                output.append(f" {'':>{width}} ...")
                old_line += skipped
                new_line += skipped
        elif trailing:
            skipped = max(0, len(raw) - _CONTEXT_LINES)
            if skipped > 0:
                output.append(f" {'':>{width}} ...")
                old_line += skipped
                new_line += skipped
            for line in raw[skipped:]:
                output.append(f" {str(old_line).rjust(width)} {line}")
                old_line += 1
                new_line += 1
        else:
            old_line += len(raw)
            new_line += len(raw)
        last_was_change = False
    if first_changed is None:
        raise RuntimeError("diff of identical texts has no firstChangedLine")
    return DiffString("\n".join(output), first_changed)


def _split_lines(text: str) -> list[str]:
    trailing = text.endswith("\n")
    lines = [line + "\n" for line in text.split("\n")]
    if trailing:
        lines.pop()
    else:
        lines[-1] = lines[-1][:-1]
    return lines


def generate_unified_patch(path: str, old_content: str, new_content: str) -> str:
    diff = diff_lines(old_content, new_content)
    diff.append(_Change(0, False, False, value=""))
    hunks: list[tuple[int, int, int, int, list[str]]] = []
    old_range_start = 0
    new_range_start = 0
    current: list[str] = []
    old_line = 1
    new_line = 1
    context = _CONTEXT_LINES
    for index, part in enumerate(diff):
        lines = _split_lines(part.value) if part.value or part.added or part.removed else []
        if part.added or part.removed:
            if not old_range_start:
                old_range_start = old_line
                new_range_start = new_line
                if index > 0:
                    previous_lines = _split_lines(diff[index - 1].value)
                    current = (
                        [f" {line}" for line in previous_lines[-context:]]
                        if context > 0
                        else []
                    )
                    old_range_start -= len(current)
                    new_range_start -= len(current)
            prefix = "+" if part.added else "-"
            current.extend(prefix + line for line in lines)
            if part.added:
                new_line += len(lines)
            else:
                old_line += len(lines)
            continue
        if old_range_start:
            if len(lines) <= context * 2 and index < len(diff) - 2:
                current.extend(f" {line}" for line in lines)
            else:
                context_size = min(len(lines), context)
                current.extend(f" {line}" for line in lines[:context_size])
                hunks.append(
                    (
                        old_range_start,
                        old_line - old_range_start + context_size,
                        new_range_start,
                        new_line - new_range_start + context_size,
                        current,
                    )
                )
                old_range_start = 0
                new_range_start = 0
                current = []
        old_line += len(lines)
        new_line += len(lines)
    formatted: list[str] = [f"--- {path}", f"+++ {path}"]
    for old_start, old_lines, new_start, new_lines, hunk_lines in hunks:
        cleaned: list[str] = []
        for line in hunk_lines:
            if line.endswith("\n"):
                cleaned.append(line[:-1])
            else:
                cleaned.append(line)
                cleaned.append("\\ No newline at end of file")
        if old_lines == 0:
            old_start -= 1
        if new_lines == 0:
            new_start -= 1
        formatted.append(f"@@ -{old_start},{old_lines} +{new_start},{new_lines} @@")
        formatted.extend(cleaned)
    return "\n".join(formatted) + "\n"
