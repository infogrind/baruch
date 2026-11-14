"""Listing a rule's source (or its folder on a drive) as units.

A rule's units are the direct subfolders of its source, plus one unit
`(files)` for the files and symlinks directly in it. Paths inside a unit are
relative to the rule's folder, e.g. `The Wire/S01/E01.mkv`.
"""

from __future__ import annotations

import fnmatch
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

FILES_UNIT = "(files)"

FILE, DIR, LINK = "f", "d", "l"


@dataclass(frozen=True)
class Entry:
    kind: str
    size: int = 0
    mtime: int = 0
    target: str | None = None
    inode: tuple[int, int] | None = None

    def same(self, other: Entry) -> bool:
        if self.kind != other.kind:
            return False
        if self.kind == FILE:
            return self.size == other.size and self.mtime == other.mtime
        if self.kind == LINK:
            return self.target == other.target
        return True


@dataclass
class UnitScan:
    name: str
    entries: dict[str, Entry] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)
    unreadable: str | None = None  # the unit's own folder couldn't be listed

    @property
    def size(self) -> int:
        seen: set[tuple[int, int]] = set()
        total = 0
        for e in self.entries.values():
            if e.kind != FILE:
                continue
            if e.inode is not None:
                if e.inode in seen:
                    continue
                seen.add(e.inode)
            total += e.size
        return total

    @property
    def files(self) -> int:
        return sum(1 for e in self.entries.values() if e.kind != DIR)

    @property
    def newest(self) -> int:
        return max((e.mtime for e in self.entries.values() if e.kind != DIR), default=0)

    def blocked(self, rel: str) -> bool:
        """True if `rel` lies under a path whose listing failed."""
        return any(rel == p or rel.startswith(p + "/") for p, _ in self.errors)


@dataclass
class TreeScan:
    units: dict[str, UnitScan] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)  # rel path, message
    special: int = 0
    root_error: str | None = None
    other: list[str] = field(default_factory=list)  # top-level names that aren't units


def excluded(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def _entry(st: os.stat_result, path: str) -> Entry | None:
    mode = st.st_mode
    if stat.S_ISREG(mode):
        return Entry(FILE, st.st_size, int(st.st_mtime), None, (st.st_dev, st.st_ino))
    if stat.S_ISDIR(mode):
        return Entry(DIR, 0, int(st.st_mtime))
    if stat.S_ISLNK(mode):
        return Entry(LINK, 0, int(st.st_mtime), os.readlink(path))
    return None


def scan_tree(root: Path, exclude: list[str]) -> TreeScan:
    out = TreeScan()
    files_unit = UnitScan(FILES_UNIT)
    try:
        top = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError as e:
        out.root_error = e.strerror or str(e)
        return out
    for item in top:
        if excluded(item.name, exclude):
            continue
        try:
            st = item.stat(follow_symlinks=False)
        except OSError as e:
            out.errors.append((item.name, e.strerror or str(e)))
            continue
        entry = _entry(st, item.path)
        if entry is None:
            out.special += 1
        elif entry.kind == DIR:
            unit = UnitScan(item.name)
            unit.entries[item.name] = entry
            _walk(Path(item.path), item.name, exclude, unit, out)
            out.units[item.name] = unit
        else:
            files_unit.entries[item.name] = entry
    out.units[FILES_UNIT] = files_unit
    return out


def _walk(path: Path, rel: str, exclude: list[str], unit: UnitScan, out: TreeScan):
    try:
        items = sorted(os.scandir(path), key=lambda e: e.name)
    except OSError as e:
        msg = e.strerror or str(e)
        unit.errors.append((rel, msg))
        if rel == unit.name:
            unit.unreadable = msg
        return
    for item in items:
        if excluded(item.name, exclude):
            continue
        child = f"{rel}/{item.name}"
        try:
            st = item.stat(follow_symlinks=False)
        except OSError as e:
            unit.errors.append((child, e.strerror or str(e)))
            continue
        entry = _entry(st, item.path)
        if entry is None:
            out.special += 1
            continue
        unit.entries[child] = entry
        if entry.kind == DIR:
            _walk(Path(item.path), child, exclude, unit, out)


def scan_drive_root(root: Path) -> list[str]:
    """Top-level names on a drive."""
    try:
        return sorted(os.listdir(root))
    except OSError:
        return []
