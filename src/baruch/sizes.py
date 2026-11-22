"""Sizes, shares and durations, all binary (KiB, MiB, GiB, TiB); counts."""

from __future__ import annotations

import re
from datetime import timedelta

UNITS = {"": 1, "B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


class FormatError(ValueError):
    pass


def parse_size(text: str | int) -> int:
    """`500G` -> 500 GiB. Plain numbers are bytes."""
    if isinstance(text, int):
        return text
    m = re.fullmatch(
        r"\s*([0-9]+(?:\.[0-9]+)?)\s*([BKMGT]?)(?:i?B)?\s*", str(text), re.I
    )
    if not m:
        raise FormatError(f"not a size: {text!r}")
    return int(float(m.group(1)) * UNITS[m.group(2).upper()])


def parse_share(text: str | float) -> float:
    """`10%` -> 0.1."""
    m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*", str(text))
    if not m:
        raise FormatError(f"not a percentage: {text!r}")
    return float(m.group(1)) / 100


def parse_duration(text: str) -> timedelta:
    """`30d`, `12h`, `2w`."""
    m = re.fullmatch(r"\s*([0-9]+)\s*([dhw])\s*", str(text))
    if not m:
        raise FormatError(f"not a duration: {text!r}")
    n = int(m.group(1))
    return {"d": timedelta(days=n), "h": timedelta(hours=n), "w": timedelta(weeks=n)}[
        m.group(2)
    ]


def human(n: int) -> str:
    """1536 -> '1.5 KiB'."""
    if abs(n) < 1024:
        return f"{n} B"
    nf: float = n
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        nf /= 1024
        if abs(nf) < 1024 or unit == "TiB":
            return f"{nf:.1f} {unit}"
    raise AssertionError  # pragma: no cover


def percent(part: float, whole: float) -> str:
    if whole <= 0:
        return "100%"
    return f"{100 * part / whole:.0f}%"


def count(n: int, one: str, many: str | None = None) -> str:
    """`1 unit`, `2 units`."""
    return f"{n} {one if n == 1 else many or one + 's'}"
