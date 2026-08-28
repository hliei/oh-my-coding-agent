from __future__ import annotations

from typing import Any, BinaryIO
import sys
import termios
import tty
import unicodedata

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
_ERASE_COLUMN = b"\x08 \x08"
_UTF8_ERROR = "interactive input is not UTF-8"


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


def _acquire_end_of_line_editing(fd: int) -> tuple[list[Any], bool]:
    saved = termios.tcgetattr(fd)
    echo = bool(saved[3] & termios.ECHO)
    tty.setcbreak(fd, termios.TCSANOW)
    return saved, echo


def _restore_tty(fd: int, saved: list[Any]) -> None:
    try:
        termios.tcsetattr(fd, termios.TCSANOW, saved)
    except OSError:
        return


class _EndOfLineEditor:
    __slots__ = ("_echo", "_chars", "_pending")

    def __init__(self, *, echo: bool) -> None:
        self._echo = echo
        self._chars: list[str] = []
        self._pending = bytearray()

    def reset(self) -> None:
        self._chars.clear()
        self._pending.clear()

    def feed(self, data: bytes) -> list[bytes]:
        self._pending.extend(data)
        completed: list[bytes] = []
        while self._pending:
            first = self._pending[0]
            if first < 0x20 or first == 0x7F:
                del self._pending[0]
                event = self._control(first)
                if event is not None:
                    completed.append(event)
                continue
            character = _take_complete_character(self._pending)
            if character is None:
                break
            self._chars.append(character)
            if self._echo:
                write_stdout(character)
        return completed

    def _control(self, first: int) -> bytes | None:
        if first in {0x0A, 0x0D}:
            if first == 0x0D and self._pending[:1] == b"\n":
                del self._pending[0]
            line = "".join(self._chars)
            self._chars.clear()
            if self._echo:
                write_stdout("\n")
            return (line + "\n").encode("utf-8")
        if first in {0x08, 0x7F}:
            if not self._chars:
                return None
            character = self._chars.pop()
            if self._echo:
                write_stdout(_ERASE_COLUMN * _display_columns(character))
            return None
        if first == 0x04:
            if self._chars:
                line = "".join(self._chars)
                self._chars.clear()
                return (line + "\n").encode("utf-8")
            return b""
        character = chr(first)
        self._chars.append(character)
        return None


def _display_columns(character: str) -> int:
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def _take_complete_character(buffer: bytearray) -> str | None:
    length = _utf8_length(buffer[0])
    if length is None:
        raise OSError(_UTF8_ERROR)
    if len(buffer) < length:
        if any((byte & 0xC0) != 0x80 for byte in buffer[1:]):
            raise OSError(_UTF8_ERROR)
        return None
    raw = bytes(buffer[:length])
    try:
        character = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise OSError(_UTF8_ERROR) from error
    del buffer[:length]
    return character


def _utf8_length(lead: int) -> int | None:
    if lead < 0x80:
        return 1
    if 0xC2 <= lead <= 0xDF:
        return 2
    if 0xE0 <= lead <= 0xEF:
        return 3
    if 0xF0 <= lead <= 0xF4:
        return 4
    return None
