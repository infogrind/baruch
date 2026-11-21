"""Carrying out work on a drive: the record order around each unit, the mount
check, deletions that never follow symlinks, and rsync with an exact list.

Per unit of a run: the record becomes `in-progress` on the drive, then in the
cache; the unit is synced; its folder on the drive is scanned again; the new
record goes to the drive, then record and contents to the cache together.
`prune` goes the other way: record off the drive, record and contents out of
the cache, then the data. So the cache only ever errs towards "not there".
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from baruch import cache, records
from baruch.cache import DriveCache
from baruch.drives import BARUCH_DIR, Docked, still_there
from baruch.records import ERRORS, IN_PROGRESS, OK, Record
from baruch.rsync import DriveError, DriveGone, classify, run_rsync
from baruch.scan import FILES_UNIT, UnitScan, scan_unit


class UnsafePath(DriveError):
    """A folder on the way is a symlink: never written or deleted through."""


@dataclass
class DriveState:
    """A docked drive while a command works on it: its records and the cache
    of it, kept in step."""

    docked: Docked
    records: dict[str, Record]
    cache: DriveCache
    cache_dir: Path
    exclude: list[str]

    @property
    def root(self) -> Path:
        return self.docked.path

    def save_records(self) -> None:
        records.write(self.root, self.records)

    def save_cache(self) -> None:
        cache.save(self.cache_dir, self.docked.name or "", self.cache)

    def rescan(self, unit: str) -> UnitScan:
        rule, folder = unit.split("/", 1)
        return scan_unit(self.root / rule, folder, self.exclude, inner=False)


def ensure_mounted(host, d: Docked) -> None:
    if not still_there(host, d):
        raise DriveGone(
            f"{d.name} is no longer mounted (or its label changed): stopping"
        )


def _check_inside(root: Path, rel: str) -> Path:
    """The path of `rel` on the drive, refusing any symlinked folder on the
    way (the last component itself may be a link: it's handled as a link)."""
    p = root
    parts = rel.split("/")
    for part in parts[:-1]:
        p = p / part
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            return root.joinpath(*parts)
        if stat.S_ISLNK(st.st_mode):
            raise UnsafePath(
                f"{p} is a symlink: nothing is written or deleted under it"
            )
    return root.joinpath(*parts)


def delete_entries(root: Path, rels: list[str]) -> None:
    """Files and links first, then folders deepest first. A folder that still
    holds something Baruch keeps stays."""
    dirs = []
    for rel in rels:
        p = _check_inside(root, rel)
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(st.st_mode):
            dirs.append(p)
        else:
            p.unlink()
    for p in sorted(dirs, key=lambda d: len(d.parts), reverse=True):
        with contextlib.suppress(OSError):
            p.rmdir()


def remove_unit(rule_root: Path, folder: str) -> None:
    """All of a unit's data on a drive (prune). Symlinks are removed as links,
    never followed."""
    if folder == FILES_UNIT:
        try:
            items = list(os.scandir(rule_root))
        except FileNotFoundError:
            return
        for item in items:
            if not item.is_dir(follow_symlinks=False):
                os.unlink(item.path)
        return
    p = _check_inside(rule_root.parent, f"{rule_root.name}/{folder}")
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(p)  # rmtree removes symlinks inside as links
    else:
        p.unlink()


def start_unit(state: DriveState, unit: str, now) -> Record:
    """Before a unit's sync: `in-progress`, on the drive, then in the cache."""
    old = state.records.get(unit)
    rec = Record(now, old.synced if old else None, IN_PROGRESS)
    state.records[unit] = rec
    state.save_records()
    state.cache.records[unit] = rec
    state.save_cache()
    return rec


def finish_unit(state: DriveState, unit: str, started, result: str, now) -> None:
    """After a unit's sync: rescan it, the record to the drive, then record
    and contents to the cache in one write."""
    scan = state.rescan(unit)
    rec = Record(started, now, result)
    state.records[unit] = rec
    state.save_records()
    state.cache.set_unit(unit, rec, scan)
    state.save_cache()


def sync_unit(op, root: Path, source: Path, rsync: str) -> str:
    """Deletions first, then copies. Returns the result for the record."""
    dest = root / op.rule
    _check_inside(root, f"{op.rule}/x")
    if op.delete:
        delete_entries(dest, op.delete)
    result = ERRORS if op.errors else OK
    transfer = op.transfer
    if transfer:
        for rel in transfer:
            _check_inside(dest, rel)
        code, err = run_rsync(
            rsync, source, dest, transfer, root / BARUCH_DIR / "partial"
        )
        res = classify(code, err)
        op.errors.extend(res.messages if res.result == ERRORS else [])
        if res.result == ERRORS:
            result = ERRORS
        op.messages = res.messages
    return result


def clear_unit(state: DriveState, unit: str) -> None:
    """Before prune deletes a unit's data: its record off the drive, then
    record and contents out of the cache."""
    if unit in state.records:
        del state.records[unit]
        state.save_records()
    state.cache.set_unit(unit, None, UnitScan(unit.split("/", 1)[1]))
    state.save_cache()
