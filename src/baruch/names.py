"""Unit names exactly as on disk, stored reversibly in the registry.

File names are `str` with surrogate escapes for bytes that aren't valid
UTF-8 (Python's default for file names on Linux). In the registry and in
contents lists they're written with `\\xNN` for such bytes and `\\\\` for a
literal backslash, so every name survives a round trip.
"""

from __future__ import annotations

import re
import unicodedata

_ESCAPE = re.compile(r"\\(\\|x[0-9a-fA-F]{2})")


def escape(name: str) -> str:
    raw = name.encode("utf-8", "surrogateescape").replace(b"\\", b"\\\\")
    return raw.decode("utf-8", "backslashreplace")


def unescape(text: str) -> str:
    out = bytearray()
    pos = 0
    for m in _ESCAPE.finditer(text):
        out += text[pos : m.start()].encode("utf-8")
        token = m.group(1)
        out += b"\\" if token == "\\" else bytes([int(token[1:], 16)])
        pos = m.end()
    out += text[pos:].encode("utf-8")
    return out.decode("utf-8", "surrogateescape")


def display(name: str) -> str:
    """For output: invalid bytes and unprintable characters escaped."""
    out = []
    for ch in escape(name).replace("\\\\", "\\"):
        if ch.isprintable() or ch == " ":
            out.append(ch)
        else:
            out.append(f"\\x{ord(ch):02x}" if ord(ch) < 256 else f"\\u{ord(ch):04x}")
    return "".join(out)


def similar(wanted: str, candidates: list[str]) -> list[str]:
    """Names that match after Unicode normalisation and ignoring case."""

    def key(s: str) -> str:
        return unicodedata.normalize("NFC", s).casefold()

    target = key(wanted)
    return sorted(c for c in candidates if key(c) == target)
