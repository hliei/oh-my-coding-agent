from __future__ import annotations

from typing import BinaryIO
import sys

from oh_my_llm._canonical import encodeCanonical


_BIDI = frozenset(
    {
        "\u061c",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    }
)
_FIELD_EXTRA = frozenset({'\t', '\n', '"', '\\'})


def encode_body(value: str) -> str:
    return "".join(_escape(character, field=False) for character in value)


def encode_field(value: str) -> str:
    return "".join(_escape(character, field=True) for character in value)


def encode_json(value: object) -> str:
    return encode_body(encodeCanonical(value).decode("utf-8"))


def write_stdout(data: bytes | str) -> None:
    _write(sys.stdout.buffer, _payload(data))


def write_stderr(data: bytes | str) -> bool:
    try:
        _write(sys.stderr.buffer, _payload(data))
        return True
    except OSError:
        return False


def write_usage(reason: str) -> None:
    write_stderr(f"usage: {encode_field(reason)}\n")


def write_error(message: str) -> bool:
    return write_stderr(f"error: {encode_field(message)}\n")


def write_lifecycle(code: str, message: str) -> None:
    write_stderr(f"lifecycle {encode_field(code)}: {encode_field(message)}\n")


def write_cleanup(message: str) -> None:
    write_stderr(f"lifecycle cleanup: {encode_field(message)}\n")


def write_identity(session_id: str, kind: str) -> None:
    _write(sys.stderr.buffer, _payload(f"session {encode_field(session_id)} {kind}\n"))


def write_identity_stdout(session_id: str, kind: str) -> None:
    write_stdout(f"session {encode_field(session_id)} {kind}\n")


def _escape(character: str, *, field: bool) -> str:
    code = ord(character)
    if not field and character in {'\t', '\n'}:
        return character
    if (
        code < 32
        or code == 127
        or 128 <= code <= 159
        or character in _BIDI
        or (field and character in _FIELD_EXTRA)
    ):
        return f"\\u{code:04X}"
    return character


def _payload(data: bytes | str) -> bytes:
    if isinstance(data, str):
        return data.encode("utf-8")
    return data


def _write(buffer: BinaryIO, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = buffer.write(payload[offset:])
        if written is None or written <= 0:
            raise OSError("terminal write made no progress")
        offset += written
    buffer.flush()
