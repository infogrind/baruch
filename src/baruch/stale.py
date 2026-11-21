"""What a drive holds besides its home units: stale and unexplained data.

One classification for `run`'s report, `status`, `prune` and `restore.sh`:

- stale data: a unit's data on a drive that isn't its home, of a unit without
  a home, or of a unit gone on the NAS. `prune` removes it, or holds it with a
  reason;
- unexplained data: data no current unit explains. Baruch never deletes it.

Pure logic: configuration, registry, NAS scans, the drive's contents and
records, and the other drives' caches in; a list of items out.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime

from baruch.cache import DriveCache, unit_files
from baruch.config import Config
from baruch.records import ERRORS, IN_PROGRESS, Record
from baruch.registry import Registry
from baruch.scan import FILES_UNIT, DriveScan, TreeScan, UnitScan, excluded
from baruch.timefmt import show_date

PRUNE, HOLD, UNEXPLAINED = "prune", "hold", "unexplained"
MOVED, NO_HOME, GONE = "moved", "no home", "gone"


@dataclass
class Item:
    path: str  # relative to the drive root, e.g. "tv/The Wire" or "tv2"
    action: str  # PRUNE, HOLD or UNEXPLAINED
    reason: str
    size: int
    files: int
    unit: str | None = None  # None for unexplained data
    kind: str | None = None  # MOVED, NO_HOME or GONE for stale data
    home: str | None = None
    only_files: int = 0  # files on no other drive: this is their only backup
    only_bytes: int = 0
    in_progress: bool = False  # this drive's record says the sync didn't finish
    # Where else Baruch believes this unit's files are, as last seen: the
    # drives (with when each was last seen) and whether the NAS has them.
    elsewhere: dict[str, datetime | None] = field(default_factory=dict)
    on_nas: bool = False


def unit_path(unit: str) -> str:
    """Where a unit's data is on a drive, for messages and commands."""
    rule, folder = unit.split("/", 1)
    return rule if folder == FILES_UNIT else unit


def _has_component(rel: str, patterns: list[str]) -> bool:
    return any(excluded(part, patterns) for part in rel.split("/"))


def _files(scan: UnitScan | None, patterns: list[str]) -> dict:
    return {
        rel: e
        for rel, e in unit_files(scan).items()
        if not _has_component(rel, patterns)
    }


def _index(tree: TreeScan | None, into: set) -> None:
    """(name, size, time) of every file in a rule's folder."""
    if tree is None:
        return
    for u in tree.units.values():
        for rel, e in unit_files(u).items():
            into.add((os.path.basename(rel), e.size, e.mtime))


def classify(
    *,
    config: Config,
    reg: Registry,
    drive: str,
    contents: DriveScan,
    records: dict[str, Record],
    nas: dict[str, TreeScan | None],
    caches: dict[str, DriveCache],
) -> list[Item]:
    """Stale and unexplained data on `drive`. `nas` is the scan of each rule's
    source (None: marker missing); `caches` the other drives' caches."""
    patterns = config.settings.exclude
    rules = {r.name: r for r in config.rules}
    items: list[Item] = []

    for name, entry in sorted(contents.loose.items()):
        items.append(Item(name, UNEXPLAINED, "not in a rule's folder", entry.size, 1))

    for top, tree in sorted(contents.folders.items()):
        if tree.root_error:
            continue  # can't be read: nothing in it is touched
        if top not in rules:
            size = sum(u.size for u in tree.units.values())
            files = sum(len(unit_files(u)) for u in tree.units.values())
            if files or any(u.entries for u in tree.units.values()):
                items.append(
                    Item(
                        top, UNEXPLAINED, f"rule {top} isn't in the config", size, files
                    )
                )
            continue
        scan = nas.get(top)
        if scan is None or scan.root_error:
            continue  # marker missing or source unreadable: nothing is touched

        # What exists elsewhere, for "files only here" and for showing where.
        nas_index: set = set()
        _index(scan, nas_index)
        drive_index: dict[str, set] = {}
        for other, c in caches.items():
            if other != drive:
                drive_index[other] = set()
                _index(c.contents.folders.get(top), drive_index[other])
        # "Only here" means no other backup: the NAS doesn't count, it's what
        # the backup is for (a renamed folder isn't backed up until synced).
        elsewhere = set().union(*drive_index.values())
        # Other units on this drive count too (a renamed folder synced here).
        here_units: dict[tuple, set[str]] = {}
        for f, u in tree.units.items():
            for rel, e in unit_files(u).items():
                key = (os.path.basename(rel), e.size, e.mtime)
                here_units.setdefault(key, set()).add(f)

        for folder, du in sorted(tree.units.items()):
            if not du.entries:
                continue
            unit = f"{top}/{folder}"
            if scan.units.get(folder) is not None and scan.units[folder].unreadable:
                continue
            if any(p == folder or p.startswith(folder + "/") for p, _ in scan.errors):
                continue
            files = _files(du, patterns)
            nu = scan.units.get(folder)
            on_nas = nu is not None and (folder != FILES_UNIT or bool(nu.entries))
            home = reg.homes.get(unit)
            item = Item(unit, "", "", du.size, len(files), unit=unit)
            rec = records.get(unit)
            item.in_progress = rec is not None and rec.result == IN_PROGRESS

            if not on_nas:
                if home is None:
                    item.action, item.unit = UNEXPLAINED, None
                    item.path = unit_path(unit)
                    item.reason = "not a unit (not on the NAS)"
                else:
                    item.action, item.kind, item.home = PRUNE, GONE, home
                    item.reason = "gone on the NAS"
            elif home == drive:
                continue  # a home unit: synced by runs, not stale
            elif home is None:
                item.action, item.kind = HOLD, NO_HOME
                item.reason = "no home yet (`baruch run` places it)"
            else:
                item.kind, item.home = MOVED, home
                item.action, item.reason = _release(unit, home, rec, reg, caches)

            only = {}
            for rel, e in files.items():
                key = (os.path.basename(rel), e.size, e.mtime)
                if key not in elsewhere and not here_units.get(key, set()) - {folder}:
                    only[rel] = e
            item.only_files = len(only)
            item.only_bytes = sum(e.size for e in only.values())
            keys = {(os.path.basename(r), e.size, e.mtime) for r, e in files.items()}
            item.on_nas = bool(keys & nas_index)
            for other, index in sorted(drive_index.items()):
                if keys & index:
                    entry = reg.drives.get(other)
                    item.elsewhere[other] = entry.last_seen if entry else None
            items.append(item)
    return items


def _release(
    unit: str,
    home: str,
    here: Record | None,
    reg: Registry,
    caches: dict[str, DriveCache],
) -> tuple[str, str]:
    """May a unit's data here go, now that its home is elsewhere? Only if the
    home holds a newer copy synced without errors."""
    if home not in reg.drives:
        return HOLD, f"home {home} isn't registered: no copy"
    c = caches.get(home)
    if c is None:
        return HOLD, f"{home} has no cache: dock it first"
    rec = c.records.get(unit)
    if rec is not None and rec.result == IN_PROGRESS:
        return HOLD, f"home {home} is incomplete"
    if rec is None or not c.has_data(unit):
        return HOLD, f"home {home} has no copy yet"
    if rec.result == ERRORS:
        return HOLD, f"home {home} was synced with errors"
    assert rec.synced is not None
    if here is not None:
        latest = max(t for t in (here.synced, here.started) if t is not None)
        if rec.synced <= latest:
            return HOLD, (
                f"home {home} has an older copy ({show_date(rec.synced)}) than "
                f"this one ({show_date(latest)})"
            )
    return PRUNE, f"home {home} has a newer complete copy ({show_date(rec.synced)})"


def for_restore(items: list[Item]) -> list[Item]:
    """Stale units restore.sh restores in its second pass: those `prune`
    would hold, unless the sync here didn't finish."""
    return [i for i in items if i.action == HOLD and i.unit and not i.in_progress]
