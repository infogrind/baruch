"""Listing a rule's source (or its folder on a drive) as units.

A rule's units are the direct subfolders of its source, plus one unit
`(files)` for the files and symlinks directly in it. Paths inside a unit are
relative to the rule's folder, e.g. `The Wire/S01/E01.mkv`.
"""

from __future__ import annotations

import fnmatch
import os
import stat
from collections.abc import Callable
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


# Called while a scan goes on, with the unit being listed and the files and
# bytes seen so far: (unit, files, bytes).
Progress = Callable[[str, int, int], None]


@dataclass
class _Tally:
    progress: Progress | None
    unit: str = ""
    files: int = 0
    bytes: int = 0

    def start(self, unit: str) -> None:
        self.unit = unit
        self.tick()

    def add(self, entry: Entry) -> None:
        if entry.kind != DIR:
            self.files += 1
            self.bytes += entry.size
        self.tick()

    def tick(self) -> None:
        if self.progress:
            self.progress(self.unit, self.files, self.bytes)


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


def _lstat(item: os.DirEntry) -> os.stat_result:
    return item.stat(follow_symlinks=False)


def scan_tree(
    root: Path,
    exclude: list[str],
    inner: bool = True,
    progress: Progress | None = None,
) -> TreeScan:
    """A rule's folder as units. Excluded names are skipped at every depth; on
    a drive (`inner=False`) only at the unit level, so excluded files inside a
    unit are seen and deleted by its sync."""
    out = TreeScan()
    tally = _Tally(progress)
    inner_exclude = exclude if inner else []
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
            st = _lstat(item)
        except OSError as e:
            # Unknown whether it's a folder or a file: never gone, never
            # orphaned, nothing of that name deleted on the drive.
            msg = e.strerror or str(e)
            unit = UnitScan(item.name, unreadable=msg)
            unit.errors.append((item.name, msg))
            out.units[item.name] = unit
            files_unit.errors.append((item.name, msg))
            continue
        entry = _entry(st, item.path)
        if entry is None:
            out.special += 1
        elif entry.kind == DIR and item.name == FILES_UNIT:
            out.errors.append(
                (
                    item.name,
                    f"a folder named {FILES_UNIT} can't be backed up; rename it",
                )
            )
            files_unit.errors.append((item.name, "reserved name"))
        elif entry.kind == DIR:
            tally.start(item.name)
            unit = UnitScan(item.name)
            unit.entries[item.name] = entry
            _walk(Path(item.path), item.name, inner_exclude, unit, out, tally)
            out.units[item.name] = unit
        else:
            files_unit.entries[item.name] = entry
            tally.add(entry)
    out.units[FILES_UNIT] = files_unit
    return out


def _walk(
    path: Path,
    rel: str,
    exclude: list[str],
    unit: UnitScan,
    out: TreeScan,
    tally: _Tally | None = None,
):
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
            st = _lstat(item)
        except OSError as e:
            unit.errors.append((child, e.strerror or str(e)))
            continue
        entry = _entry(st, item.path)
        if entry is None:
            out.special += 1
            continue
        unit.entries[child] = entry
        if tally:
            tally.add(entry)
        if entry.kind == DIR:
            _walk(Path(item.path), child, exclude, unit, out, tally)


def scan_unit(
    rule_root: Path, folder: str, exclude: list[str], inner: bool
) -> UnitScan:
    """One unit again, e.g. on the drive right after its sync."""
    if folder == FILES_UNIT:
        unit = UnitScan(FILES_UNIT)
        try:
            items = sorted(os.scandir(rule_root), key=lambda e: e.name)
        except OSError as e:
            unit.unreadable = e.strerror or str(e)
            return unit
        for item in items:
            if excluded(item.name, exclude):
                continue
            try:
                entry = _entry(_lstat(item), item.path)
            except OSError as e:
                unit.errors.append((item.name, e.strerror or str(e)))
                continue
            if entry is not None and entry.kind != DIR:
                unit.entries[item.name] = entry
        return unit
    unit = UnitScan(folder)
    path = rule_root / folder
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return unit
    except OSError as e:
        unit.unreadable = e.strerror or str(e)
        return unit
    entry = _entry(st, str(path))
    if entry is None or entry.kind != DIR:
        return unit  # only a folder is a folder unit's data
    unit.entries[folder] = entry
    _walk(path, folder, exclude if inner else [], unit, TreeScan())
    return unit


RESERVED_ROOT = (".baruch", "lost+found")


@dataclass
class DriveScan:
    """Everything on a drive: each top-level folder as units (a rule's folder,
    or one no rule explains), and files directly at the root."""

    folders: dict[str, TreeScan] = field(default_factory=dict)
    loose: dict[str, Entry] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)


def scan_drive(
    root: Path, exclude: list[str], progress: Progress | None = None
) -> DriveScan:
    """`progress` gets units as `folder/unit`."""
    out = DriveScan()
    folders = _Folders(progress) if progress else None
    try:
        top = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError as e:
        out.errors.append((".", e.strerror or str(e)))
        return out
    for item in top:
        if item.name in RESERVED_ROOT or excluded(item.name, exclude):
            continue
        try:
            entry = _entry(_lstat(item), item.path)
        except OSError as e:
            out.errors.append((item.name, e.strerror or str(e)))
            continue
        if entry is None:
            continue
        if entry.kind == DIR:
            out.folders[item.name] = scan_tree(
                Path(item.path),
                exclude,
                inner=False,
                progress=folders.folder(item.name) if folders else None,
            )
        else:
            out.loose[item.name] = entry
    return out


class _Folders:
    """A drive's progress over all its folders: `folder/unit`, and the files
    and bytes counted across folders."""

    def __init__(self, progress: Progress):
        self.progress = progress
        self.base = self.last = (0, 0)

    def folder(self, name: str) -> Progress:
        self.base = self.last

        def report(unit: str, files: int, size: int) -> None:
            self.last = (self.base[0] + files, self.base[1] + size)
            self.progress(f"{name}/{unit}", *self.last)

        return report
