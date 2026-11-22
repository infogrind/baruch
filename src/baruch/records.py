"""Per-unit records on a drive: `/.baruch/units.yaml`.

The facts about a drive live on that drive. One record per unit synced there:
when its last attempt started, when its last finished sync ended, and the
result of the last attempt.

The file has one record per line, in a fixed form that is also valid YAML,
so `restore.sh` can read it with plain `sh`:

    'tv/Lost': {started: 2026-11-02T20:30:00Z, synced: null, result: ok}

The name is in single quotes (a `'` in it written as `''`), with the escapes
of `names.escape` as literal text. Lines are never wrapped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from baruch import names
from baruch.timefmt import from_text, to_text

OK, ERRORS, IN_PROGRESS = "ok", "errors", "in-progress"
RESULTS = (OK, ERRORS, IN_PROGRESS)

FILE = "units.yaml"
HEADER = "# Baruch: one record per unit synced on this drive. Don't edit.\n"

_LINE = re.compile(
    r"'((?:[^']|'')*)': \{started: (\S+), synced: (\S+), result: ([a-z-]+)\}"
)


class RecordsError(Exception):
    pass


@dataclass(frozen=True)
class Record:
    started: datetime | None
    synced: datetime | None  # end of the last sync that finished
    result: str

    @property
    def complete(self) -> bool:
        """A finished sync, possibly with errors."""
        return self.synced is not None and self.result in (OK, ERRORS)


def key(name: str) -> str:
    """A unit name as written in the file, quotes included."""
    return "'" + names.escape(name).replace("'", "''") + "'"


def line(name: str, r: Record) -> str:
    return (
        f"{key(name)}: {{started: {to_text(r.started) or 'null'}, "
        f"synced: {to_text(r.synced) or 'null'}, result: {r.result}}}"
    )


def dump(records: dict[str, Record]) -> str:
    return HEADER + "".join(line(n, records[n]) + "\n" for n in sorted(records))


def _time(text: str) -> datetime | None:
    return None if text == "null" else from_text(text)


def parse(text: str) -> dict[str, Record]:
    out: dict[str, Record] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.startswith("#"):
            continue
        m = _LINE.fullmatch(raw)
        if not m or m.group(4) not in RESULTS:
            raise RecordsError(f"line {n} can't be read: {raw[:80]!r}")
        try:
            record = Record(_time(m.group(2)), _time(m.group(3)), m.group(4))
        except ValueError as e:
            raise RecordsError(f"line {n}: {e}") from None
        out[names.unescape(m.group(1).replace("''", "'"))] = record
    return out


def path(root: Path) -> Path:
    from baruch.drives import BARUCH_DIR

    return root / BARUCH_DIR / FILE


def read(root: Path) -> dict[str, Record]:
    """The records on a registered drive. A missing or unreadable file is an
    error, never taken as empty."""
    p = path(root)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RecordsError(f"{p} is missing") from None
    except (OSError, UnicodeDecodeError) as e:
        raise RecordsError(f"{p} can't be read: {e}") from None
    try:
        return parse(text)
    except RecordsError as e:
        raise RecordsError(f"{p}: {e}") from None


def write(root: Path, records: dict[str, Record]) -> None:
    from baruch.drives import write_to_drive

    write_to_drive(path(root), dump(records))
