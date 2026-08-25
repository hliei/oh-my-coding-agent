from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from yaml.loader import SafeLoader  # type: ignore[import-untyped]
from yaml.tokens import (  # type: ignore[import-untyped]
    AliasToken,
    AnchorToken,
    BlockEndToken,
    BlockMappingStartToken,
    DocumentEndToken,
    DocumentStartToken,
    FlowEntryToken,
    FlowMappingEndToken,
    FlowMappingStartToken,
    KeyToken,
    ScalarToken,
    StreamEndToken,
    StreamStartToken,
    TagToken,
    ValueToken,
)
import yaml  # type: ignore[import-untyped]


_ALLOWED_STRING_STYLES: Final[frozenset[str | None]] = frozenset({None, '"', "'"})


class FrontmatterError(ValueError):
    """Private marker for closed frontmatter syntax failure."""


@dataclass(frozen=True, slots=True)
class FrontmatterScalar:
    text: str
    style: str | None
    plain: bool

    def is_string(self) -> bool:
        if self.style not in _ALLOWED_STRING_STYLES:
            return False
        return "\n" not in self.text and "\r" not in self.text

    def as_bool(self) -> bool | None:
        if self.style is not None or not self.plain:
            return None
        if self.text == "true":
            return True
        if self.text == "false":
            return False
        return None


def split_frontmatter(text: str) -> tuple[str, str] | None:
    if not text.startswith("---"):
        return None
    if text.startswith("---\n"):
        start = 4
    elif text.startswith("---\r\n"):
        start = 5
    else:
        return None
    end = text.find("\n---", start - 1)
    if end == -1:
        return None
    yaml_string = text[start:end]
    if yaml_string.endswith("\r"):
        yaml_string = yaml_string[:-1]
    body = text[end + 4 :]
    if body.startswith("\n"):
        body = body[1:]
    elif body.startswith("\r\n"):
        body = body[2:]
    return yaml_string, body


def parse_frontmatter_mapping(yaml_string: str) -> dict[str, FrontmatterScalar]:
    try:
        tokens = list(yaml.scan(yaml_string, Loader=SafeLoader))
    except yaml.YAMLError as error:
        raise FrontmatterError("invalid YAML") from error
    index = 0

    def current() -> object:
        return tokens[index]

    def advance() -> object:
        nonlocal index
        token = tokens[index]
        index += 1
        return token

    if not tokens or type(tokens[0]) is not StreamStartToken:
        raise FrontmatterError("invalid YAML")
    advance()
    if index < len(tokens) and type(current()) is DocumentStartToken:
        advance()
    if index >= len(tokens):
        raise FrontmatterError("invalid YAML")
    opener = type(current())
    if opener is StreamEndToken:
        raise FrontmatterError("frontmatter must be a mapping")
    if opener is BlockMappingStartToken:
        closer = BlockEndToken
    elif opener is FlowMappingStartToken:
        closer = FlowMappingEndToken
    else:
        raise FrontmatterError("frontmatter must be a mapping")
    advance()
    mapping: dict[str, FrontmatterScalar] = {}
    while index < len(tokens) and type(current()) is not closer:
        if type(current()) is FlowEntryToken:
            advance()
            continue
        _reject_forbidden(current())
        if type(current()) is not KeyToken:
            raise FrontmatterError("invalid mapping key")
        advance()
        _reject_forbidden(current())
        if type(current()) is not ScalarToken:
            raise FrontmatterError("invalid mapping key")
        key_token = advance()
        assert type(key_token) is ScalarToken
        key_scalar = _scalar(key_token)
        if not key_scalar.is_string():
            raise FrontmatterError("invalid mapping key")
        if key_scalar.text in mapping:
            raise FrontmatterError("duplicate mapping key")
        _reject_forbidden(current())
        if type(current()) is not ValueToken:
            raise FrontmatterError("invalid mapping value")
        advance()
        if index >= len(tokens):
            raise FrontmatterError("invalid mapping value")
        _reject_forbidden(current())
        if type(current()) is not ScalarToken:
            raise FrontmatterError("invalid mapping value")
        mapping[key_scalar.text] = _scalar(advance())
    if index >= len(tokens) or type(current()) is not closer:
        raise FrontmatterError("invalid YAML")
    advance()
    if index < len(tokens) and type(current()) is DocumentEndToken:
        advance()
    if index != len(tokens) - 1 or type(current()) is not StreamEndToken:
        raise FrontmatterError("invalid YAML")
    return mapping


def _scalar(token: ScalarToken) -> FrontmatterScalar:
    return FrontmatterScalar(text=token.value, style=token.style, plain=token.plain)


def _reject_forbidden(token: object) -> None:
    if type(token) in {AliasToken, AnchorToken, TagToken}:
        raise FrontmatterError("unsupported YAML feature")
