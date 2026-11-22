"""The NAS's cache of each drive: its records and contents as last read.

Built only from what Baruch itself read from the drive: the records from its
`units.yaml`, the contents from Baruch's own scan of the drive. It can be out
of date, but never says something the drive didn't. Each drive's cache is one
JSON file, so a unit's record and contents always change together, in one
atomic write.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from baruch import names
from baruch.records import Record
from baruch.registry import write_atomic
from baruch.scan import DIR, FILES_UNIT, DriveScan, Entry, TreeScan, UnitScan
from baruch.timefmt import from_text, to_text


class CacheError(Exception):
    pass


@dataclass
class DriveCache:
    disk_id: str
    read: datetime | None = None
    records: dict[str, Record] = field(default_factory=dict)
    contents: DriveScan = field(default_factory=DriveScan)

    def unit(self, name: str) -> UnitScan | None:
        rule, folder = name.split("/", 1)
        tree = self.contents.folders.get(rule)
        return tree.units.get(folder) if tree else None

    def has_data(self, name: str) -> bool:
        u = self.unit(name)
        return bool(u and u.entries)

    def complete(self, name: str) -> Record | None:
        """The record of a complete copy here: a finished sync, with the
        unit's data actually there."""
        r = self.records.get(name)
        if r is not None and r.complete and self.has_data(name):
            return r
        return None

    def set_unit(self, name: str, record: Record | None, scan: UnitScan) -> None:
        rule, folder = name.split("/", 1)
        tree = self.contents.folders.setdefault(rule, TreeScan())
        if scan.entries:
            tree.units[folder] = scan
        else:
            tree.units.pop(folder, None)
        if record is None:
            self.records.pop(name, None)
        else:
            self.records[name] = record


def path_of(cache_dir: Path, drive: str) -> Path:
    return cache_dir / f"{drive}.json"


def _entry(e: Entry) -> list:
    return [e.kind, e.size, e.mtime]


def _unentry(raw: list) -> Entry:
    return Entry(raw[0], int(raw[1]), int(raw[2]))


def to_doc(c: DriveCache) -> dict:
    folders = {}
    for top, tree in sorted(c.contents.folders.items()):
        folders[names.escape(top)] = {
            names.escape(folder): {
                names.escape(rel): _entry(e) for rel, e in sorted(u.entries.items())
            }
            for folder, u in sorted(tree.units.items())
            if u.entries
        }
    return {
        "disk_id": c.disk_id,
        "read": to_text(c.read),
        "records": {
            names.escape(n): [to_text(r.started), to_text(r.synced), r.result]
            for n, r in sorted(c.records.items())
        },
        "folders": folders,
        "loose": {
            names.escape(n): _entry(e) for n, e in sorted(c.contents.loose.items())
        },
    }


def from_doc(doc: dict) -> DriveCache:
    c = DriveCache(str(doc["disk_id"]), from_text(doc.get("read")))
    for key, (started, synced, result) in (doc.get("records") or {}).items():
        c.records[names.unescape(key)] = Record(
            from_text(started), from_text(synced), str(result)
        )
    for top, units in (doc.get("folders") or {}).items():
        tree = TreeScan()
        for folder, entries in units.items():
            name = names.unescape(folder)
            u = UnitScan(name)
            u.entries = {names.unescape(r): _unentry(e) for r, e in entries.items()}
            tree.units[name] = u
        c.contents.folders[names.unescape(top)] = tree
    c.contents.loose = {
        names.unescape(n): _unentry(e) for n, e in (doc.get("loose") or {}).items()
    }
    return c


def load(cache_dir: Path, drive: str) -> DriveCache | None:
    p = path_of(cache_dir, drive)
    try:
        return from_doc(json.loads(p.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise CacheError(f"{p} can't be read: {e}") from None


def save(cache_dir: Path, drive: str, c: DriveCache) -> None:
    write_atomic(
        path_of(cache_dir, drive), json.dumps(to_doc(c), separators=(",", ":"))
    )


def remove(cache_dir: Path, drive: str) -> None:
    path_of(cache_dir, drive).unlink(missing_ok=True)


def records_only(cache_dir: Path, drive: str) -> str | None:
    """The cached records of a drive, for a saved version (contents come back
    with the next scan)."""
    c = load(cache_dir, drive)
    if c is None:
        return None
    doc = to_doc(c)
    doc["folders"], doc["loose"] = {}, {}
    return json.dumps(doc, separators=(",", ":"))


def unit_files(scan: UnitScan | None) -> dict[str, Entry]:
    """Files and links of a unit (folders don't count as data on their own)."""
    if scan is None:
        return {}
    return {rel: e for rel, e in scan.entries.items() if e.kind != DIR}


__all__ = ["FILES_UNIT", "CacheError", "DriveCache", "load", "save", "remove"]
