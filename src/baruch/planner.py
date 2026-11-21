"""The plan: everything a run would do on the docked drive.

Pure logic: configuration, registry, the NAS scan, the docked drive's scan and
records, and the other drives' caches in; the plan out. Nothing here touches
files. A run only syncs inside units whose home is the docked drive and never
deletes a whole unit; everything else on the drive is reported (`stale`).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime

from baruch import stale
from baruch.cache import DriveCache
from baruch.config import Config, Rule
from baruch.records import IN_PROGRESS, Record
from baruch.registry import Registry, folder_of, rule_of
from baruch.scan import DIR, FILES_UNIT, DriveScan, TreeScan, UnitScan, excluded
from baruch.sizes import human

FIRST = "first"  # no data on this drive yet: one summary line
SYNC = "sync"  # data here: every new, changed and deleted file is listed

G_INCOMPLETE, G_EXISTING, G_FIRST = range(3)  # order of execution


@dataclass
class UnitOp:
    unit: str
    rule: str
    kind: str
    group: int
    size: int  # on the NAS
    new: list[str] = field(default_factory=list)  # relative to the rule's folder
    changed: list[str] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)  # among `delete`
    own: list[str] = field(default_factory=list)  # Baruch's own state, changed
    write_bytes: int = 0
    overwrite_bytes: int = 0
    delete_bytes: int = 0
    delete_files: int = 0
    files: int = 0  # files and links on the NAS
    warn_files: int = 0  # overwritten or deleted, Baruch's own state left out
    warn_bytes: int = 0
    unrecorded: bool = False  # data here, but no record: "existing folder"
    errors: list[str] = field(default_factory=list)  # the record gets `errors`
    messages: list[str] = field(default_factory=list)  # from rsync, when done
    loss: float = 0.0  # share of its bytes here overwritten or deleted
    placed: bool = False  # got its home in this plan

    @property
    def transfer(self) -> list[str]:
        return sorted(set(self.new) | set(self.changed))

    @property
    def has_changes(self) -> bool:
        return bool(self.new or self.changed or self.delete)

    @property
    def net(self) -> int:
        return self.write_bytes - self.overwrite_bytes - self.delete_bytes

    @property
    def needs(self) -> int:
        """Bytes needed beyond what the deletions free, for the fit check."""
        return self.write_bytes - self.overwrite_bytes


@dataclass
class Plan:
    drive: str
    takes_units: bool
    reserve: int
    free: int
    ops: list[UnitOp] = field(default_factory=list)
    new_homes: dict[str, str] = field(default_factory=dict)  # unit -> home
    rehomed: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    dropped: list[tuple[str, str]] = field(default_factory=list)  # unit, why
    excluded_data: dict[str, list[str]] = field(default_factory=dict)
    drop_gone: list[str] = field(default_factory=list)
    unplaced: list[tuple[str, int]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    markers_missing: list[str] = field(default_factory=list)
    scan_errors: list[tuple[str, str]] = field(default_factory=list)
    drive_errors: list[tuple[str, str]] = field(default_factory=list)
    unreadable: list[tuple[str, str]] = field(default_factory=list)
    gone: dict[str, list[str]] = field(default_factory=dict)  # rule -> units
    stale: list[stale.Item] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    free_after: int = 0
    largest: list[tuple[str, int, int]] = field(default_factory=list)
    room: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    pending_elsewhere: list[tuple[str, int, datetime | None]] = field(
        default_factory=list
    )

    @property
    def file_ops(self) -> bool:
        return any(op.has_changes for op in self.ops)

    def registry_changes(self) -> list[str]:
        """Changes to the registry, listed in the plan; they need a y too."""
        out = []
        for unit, home in sorted(self.new_homes.items()):
            out.append(f"{unit}: new unit, home {home}")
        for unit, (old, new) in sorted(self.rehomed.items()):
            target = new or "none (fits on no drive)"
            out.append(f"{unit}: home {old} → {target} ({old} isn't in its rule)")
        for unit, why in self.dropped:
            out.append(f"{unit}: dropped ({why})")
        for unit in self.drop_gone:
            out.append(f"{unit}: gone on the NAS, no data on any drive: entry removed")
        return out

    @property
    def needs_answer(self) -> bool:
        return self.file_ops or bool(self.registry_changes())

    @property
    def reserve_used(self) -> int:
        return max(0, self.reserve - self.free_after) if self.takes_units else 0

    @property
    def gone_count(self) -> int:
        return sum(len(v) for v in self.gone.values())


def has_data(u: UnitScan | None) -> bool:
    return bool(u and u.entries)


def _under(rel: str, paths: list[str]) -> bool:
    return any(rel == p or rel.startswith(p + "/") for p in paths)


def diff(
    nu: UnitScan | None,
    du: UnitScan | None,
    op: UnitOp,
    patterns: list[str],
    own: str | None = None,
    skip_new: set[str] | None = None,
) -> None:
    """Fill `op` with what makes the drive's copy match the NAS. Nothing is
    deleted under a path the NAS couldn't list; nothing is touched under a
    path the drive couldn't list."""
    nas_e = nu.entries if nu else {}
    disk_e = du.entries if du else {}
    nas_blocked = [p for p, _ in nu.errors] if nu else []
    disk_blocked = [p for p, _ in du.errors] if du else []
    skip_new = skip_new or set()
    for rel, e in nas_e.items():
        if rel in skip_new or _under(rel, disk_blocked):
            continue
        d = disk_e.get(rel)
        if d is None:
            op.new.append(rel)
            op.write_bytes += e.size
        elif not e.same(d):
            if e.kind != d.kind:
                op.delete.append(rel)
                if d.kind != DIR:
                    op.delete_files += 1
                    op.delete_bytes += d.size
                op.new.append(rel)
                op.write_bytes += e.size
            elif e.kind != DIR:
                op.changed.append(rel)
                op.write_bytes += e.size
                op.overwrite_bytes += d.size
    for rel, d in disk_e.items():
        if rel in nas_e or _under(rel, nas_blocked) or _under(rel, disk_blocked):
            continue
        op.delete.append(rel)
        if any(excluded(part, patterns) for part in rel.split("/")):
            op.excluded.append(rel)
        if d.kind != DIR:
            op.delete_files += 1
            op.delete_bytes += d.size
    if own:
        touched = set(op.changed) | set(op.delete) | set(op.new)
        op.own = sorted(r for r in touched if _under(r, [own]))
    own_set = set(op.own)
    for rel in set(op.changed) | set(op.delete):
        d = disk_e[rel]
        if rel in own_set or d.kind == DIR:
            continue
        op.warn_files += 1
        op.warn_bytes += d.size
    op.new.sort()
    op.changed.sort()
    op.delete.sort()
    op.excluded.sort()


def _record_rank(r: Record | None) -> tuple:
    """Data without a record ranks below any record."""
    if r is None or r.synced is None:
        return (0, datetime.min.isoformat())
    return (1, r.synced.isoformat())


def make_plan(
    *,
    config: Config,
    reg: Registry,
    drive: str,
    capacity: int,
    free: int,
    nas: dict[str, TreeScan | None],
    disk: DriveScan,
    records: dict[str, Record],
    caches: dict[str, DriveCache],
    skip: set[str] | None = None,
    own_state: tuple[str, str] | None = None,
    now: datetime | None = None,
) -> Plan:
    """`nas`: each rule's source as units (None: marker missing). `disk`: the
    docked drive, freshly scanned; `records`: its records. `caches`: the other
    drives. `own_state`: Baruch's state folder as (rule, path in its folder)."""
    skip = skip or set()
    patterns = config.settings.exclude
    conf = config.drives.get(drive)
    takes_units = conf is not None
    reserve = conf.reserve(capacity) if conf else 0
    plan = Plan(drive, takes_units, reserve, free)
    rules = {r.name: r for r in config.rules}
    homes = dict(reg.homes)

    def disk_unit(unit: str) -> UnitScan | None:
        tree = disk.folders.get(rule_of(unit))
        return tree.units.get(folder_of(unit)) if tree else None

    def data_on(d: str, unit: str) -> bool:
        if d == drive:
            return has_data(disk_unit(unit))
        c = caches.get(d)
        return bool(c and c.has_data(unit))

    def record_on(d: str, unit: str) -> Record | None:
        if d == drive:
            return records.get(unit)
        c = caches.get(d)
        return c.records.get(unit) if c else None

    # 1. The registry against the config: removed rules, excluded folders,
    #    homes whose drive the rule no longer lists, gone units.
    removed: dict[str, int] = {}
    for unit, home in sorted(reg.homes.items()):
        rule = rules.get(rule_of(unit))
        if rule is None:
            removed[rule_of(unit)] = removed.get(rule_of(unit), 0) + 1
            plan.dropped.append((unit, f"rule {rule_of(unit)} removed"))
            del homes[unit]
            continue
        scan = nas.get(rule.name)
        if scan is None or scan.root_error:
            continue  # marker missing or source unreadable: nothing changes
        folder = folder_of(unit)
        if folder != FILES_UNIT and excluded(folder, patterns):
            where = [d for d in reg.drives if data_on(d, unit)]
            plan.dropped.append((unit, "its folder is now excluded"))
            plan.excluded_data[unit] = where
            del homes[unit]
            continue
        if folder not in scan.units and not _under(folder, [p for p, _ in scan.errors]):
            plan.gone.setdefault(rule.name, []).append(unit)
            if unit in skip:
                plan.skipped.append((unit, "skipped on request"))
            if not any(data_on(d, unit) for d in reg.drives):
                plan.drop_gone.append(unit)
                del homes[unit]
            continue
        if home not in rule.drives:
            plan.rehomed[unit] = (home, None)
            homes[unit] = None  # placed again below
    for rule_name, n in sorted(removed.items()):
        plan.warnings.append(
            f"WARNING: rule {rule_name} removed: {n} units dropped; their data "
            "becomes unexplained data"
        )
    for unit, where in sorted(plan.excluded_data.items()):
        places = ", ".join(where) if where else "no drive"
        plan.warnings.append(
            f"WARNING: {unit} is now excluded: dropped; its data stays on "
            f"{places} ({stale.unit_path(unit)}), delete it by hand"
        )

    # 2. Units on the NAS, and which ones need a home.
    to_place: list[tuple[str, Rule, UnitScan]] = []
    sizes: dict[str, int] = {}
    newest: dict[str, int] = {}
    nas_units: dict[str, UnitScan] = {}
    for rule in config.rules:
        scan = nas.get(rule.name)
        if scan is None:
            plan.markers_missing.append(rule.name)
            continue
        tree = disk.folders.get(rule.name)
        if tree is not None and tree.root_error:
            # Nothing of this rule is touched while its folder can't be read.
            plan.drive_errors.append((rule.name, tree.root_error))
            for unit, home in homes.items():
                if home == drive and rule_of(unit) == rule.name:
                    plan.skipped.append((unit, "its rule's folder can't be read"))
            continue
        if scan.root_error:
            plan.scan_errors.append((rule.name, scan.root_error))
            continue
        for rel, msg in scan.errors:
            plan.scan_errors.append((f"{rule.name}/{rel}", msg))
        for folder, nu in sorted(scan.units.items()):
            unit = f"{rule.name}/{folder}"
            for rel, msg in nu.errors:
                if rel != nu.name and not nu.unreadable:
                    plan.scan_errors.append((f"{rule.name}/{rel}", msg))
            if nu.unreadable:
                plan.unreadable.append((unit, nu.unreadable))
                continue
            nas_units[unit] = nu
            sizes[unit] = nu.size
            newest[unit] = nu.newest
            if homes.get(unit) is None:
                to_place.append((unit, rule, nu))

    # 3. What other drives still have to take: their home units' growth.
    pending: dict[str, int] = {}
    for unit, home in homes.items():
        if home is None or home == drive or unit not in nas_units:
            continue
        c = caches.get(home)
        there = c.unit(unit).size if c and c.unit(unit) else 0
        pending[home] = pending.get(home, 0) + max(0, sizes[unit] - there)

    sim = free  # the docked drive's free space as the run goes on

    # 4. Units whose home is already the docked drive.
    existing: list[UnitOp] = []
    first: list[UnitOp] = []
    for unit, nu in nas_units.items():
        if homes.get(unit) != drive:
            continue
        op = _unit_op(
            plan, unit, nu, disk_unit(unit), records, patterns, own_state, disk
        )
        if op is None:
            continue
        if unit in skip:
            plan.skipped.append((unit, "skipped on request"))
            continue
        (existing if op.kind == SYNC else first).append(op)

    for op in sorted(existing, key=lambda o: (o.group, -o.size, o.unit)):
        if op.needs - op.delete_bytes > sim:
            plan.skipped.append((op.unit, f"doesn't fit ({_need(op.net, sim)})"))
            continue
        sim -= op.net
        plan.ops.append(op)
    for op in sorted(first, key=lambda o: (-o.size, o.unit)):
        if sim - op.size < reserve:
            plan.skipped.append(
                (op.unit, f"doesn't fit ({_need(op.size, sim - reserve)})")
            )
            continue
        sim -= op.net
        plan.ops.append(op)

    # 5. Placement, largest first: a listed drive already holding the data,
    #    else the first one with room, else unplaced.
    for unit, rule, nu in sorted(to_place, key=lambda t: (-t[2].size, t[0])):
        listed = [d for d in rule.drives if d in reg.drives and d in config.drives]
        holding = [d for d in listed if data_on(d, unit)]
        target = None
        if holding:
            target = max(
                holding,
                key=lambda d: (_record_rank(record_on(d, unit)), -listed.index(d)),
            )
        else:
            for d in listed:
                r = config.drives[d].reserve(
                    capacity if d == drive else reg.drives[d].capacity
                )
                room = sim if d == drive else reg.drives[d].free - pending.get(d, 0)
                if room - nu.size >= r:
                    target = d
                    break
        if unit in plan.rehomed:
            plan.rehomed[unit] = (plan.rehomed[unit][0], target)
        if target is None:
            plan.unplaced.append((unit, nu.size))
            continue
        homes[unit] = target
        if unit not in plan.rehomed:
            plan.new_homes[unit] = target
        if target != drive:
            pending[target] = pending.get(target, 0) + nu.size
            continue
        op = _unit_op(
            plan, unit, nu, disk_unit(unit), records, patterns, own_state, disk
        )
        if op is None:
            continue
        op.placed = True
        if unit in skip:
            plan.skipped.append((unit, "skipped on request"))
            continue
        fits = (
            op.needs - op.delete_bytes <= sim
            if op.kind == SYNC
            else sim - op.size >= reserve
        )
        if not fits:
            plan.skipped.append((unit, f"doesn't fit ({_need(op.net, sim)})"))
            continue
        sim -= op.net
        plan.ops.append(op)

    plan.free_after = sim
    plan.ops.sort(key=lambda o: (o.group, -o.size, o.unit))

    # 6. Everything else on the drive, as `prune` would see it.
    view = deepcopy(reg)
    view.homes = {u: h for u, h in homes.items() if h is not None}
    plan.stale = stale.classify(
        config=config,
        reg=view,
        drive=drive,
        contents=disk,
        records=records,
        nas=nas,
        caches=caches,
    )

    _warnings(plan, config, disk)
    if plan.reserve_used:
        _reserve_report(plan, config, reg, homes, disk, caches, pending)
    if now is not None:
        _pending_elsewhere(plan, config, reg, homes, caches, now)
    return plan


def _unit_op(plan, unit, nu, du, records, patterns, own_state, disk):
    """The sync of one home unit, or None if it can't be synced this run."""
    rule, folder = unit.split("/", 1)
    if du is not None and du.unreadable:
        plan.drive_errors.append((unit, du.unreadable))
        plan.skipped.append((unit, "its folder on the drive can't be read"))
        return None
    tree = disk.folders.get(rule)
    skip_new: set[str] = set()
    errors: list[str] = []
    if folder != FILES_UNIT and tree is not None:
        loose = tree.units.get(FILES_UNIT)
        if loose is not None and folder in loose.entries:
            plan.skipped.append(
                (unit, "skipped: name collides with data on the drive, see the plan")
            )
            return None
    if folder == FILES_UNIT and tree is not None:
        for name in nu.entries:
            if has_data(tree.units.get(name)):
                skip_new.add(name)
                errors.append(f"{name}: name collides with a folder on the drive")
    rec = records.get(unit)
    kind = SYNC if has_data(du) else FIRST
    group = (
        G_INCOMPLETE
        if rec and rec.result == IN_PROGRESS
        else (G_EXISTING if kind == SYNC else G_FIRST)
    )
    op = UnitOp(unit, rule, kind, group, nu.size)
    own = None
    if own_state and own_state[0] == rule:
        own = own_state[1]
    diff(nu, du, op, patterns, own, skip_new)
    op.files = nu.files
    op.unrecorded = kind == SYNC and rec is None
    op.errors = errors
    op.errors += [f"{p}: {m}" for p, m in nu.errors if p != nu.name]
    if du is not None:
        op.errors += [f"{p}: {m} (on the drive)" for p, m in du.errors]
        if du.size:
            op.loss = op.warn_bytes / du.size
    return op


def _need(need: int, room: int) -> str:
    return f"needs {human(need)}, {human(max(room, 0))} free"


def _warnings(plan: Plan, config: Config, disk: DriveScan) -> None:
    s = config.settings
    total_files = total_bytes = 0
    for tree in disk.folders.values():
        for u in tree.units.values():
            total_files += u.files
            total_bytes += u.size
    changed_files = sum(op.warn_files for op in plan.ops)
    changed_bytes = sum(op.warn_bytes for op in plan.ops)
    if total_files and changed_files / total_files > s.warn_changed_files:
        plan.warnings.append(
            f"WARNING: {changed_files} of {total_files} files "
            f"({100 * changed_files / total_files:.0f}%) on {plan.drive} would be "
            "overwritten or deleted"
        )
    if total_bytes and changed_bytes / total_bytes > s.warn_changed_bytes:
        plan.warnings.append(
            f"WARNING: {human(changed_bytes)} of {human(total_bytes)} "
            f"({100 * changed_bytes / total_bytes:.0f}%) on {plan.drive} would be "
            "overwritten or deleted"
        )
    for op in plan.ops:
        if op.loss > s.warn_unit_loss:
            plan.warnings.append(
                f"WARNING: {op.unit} would lose {op.loss:.0%} of its bytes to "
                "overwrites and deletions"
            )


def _reserve_report(plan, config, reg, homes, disk, caches, pending) -> None:
    """The drive's largest units and the room on the other drives of their
    rules, so the user can pick one to move."""
    units = []
    for unit, home in homes.items():
        if home != plan.drive:
            continue
        tree = disk.folders.get(rule_of(unit))
        du = tree.units.get(folder_of(unit)) if tree else None
        if has_data(du):
            units.append((unit, du.size, du.newest))
    plan.largest = sorted(units, key=lambda t: (-t[1], t[0]))[:10]
    for rule in config.rules:
        if not any(rule_of(u) == rule.name for u, _, _ in plan.largest):
            continue
        rooms = []
        for d in rule.drives:
            if d == plan.drive or d not in reg.drives or d not in config.drives:
                continue
            rec = reg.drives[d]
            r = config.drives[d].reserve(rec.capacity)
            rooms.append((d, max(0, rec.free - pending.get(d, 0) - r)))
        plan.room[rule.name] = rooms


def _pending_elsewhere(plan, config, reg, homes, caches, now) -> None:
    """Units copy pending on drives not docked for longer than stale_after."""
    limit = config.settings.stale_after
    counts: dict[str, int] = {}
    for unit, home in homes.items():
        if home is None or home == plan.drive or home not in reg.drives:
            continue
        c = caches.get(home)
        if c is not None and c.complete(unit):
            continue
        seen = reg.drives[home].last_seen
        if seen is None or now - seen > limit:
            counts[home] = counts.get(home, 0) + 1
    plan.pending_elsewhere = [
        (d, n, reg.drives[d].last_seen) for d, n in sorted(counts.items())
    ]


def apply(reg: Registry, plan: Plan) -> None:
    """The plan's registry changes, saved right after `y`."""
    for unit, _ in plan.dropped:
        reg.homes.pop(unit, None)
    for unit in plan.drop_gone:
        reg.homes.pop(unit, None)
    for unit, (_, new) in plan.rehomed.items():
        if new is None:
            reg.homes.pop(unit, None)
        else:
            reg.homes[unit] = new
    reg.homes.update(plan.new_homes)
