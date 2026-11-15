"""The plan: everything a run would do on the docked drive.

Pure logic: configuration, registry and file listings in, plan out. Nothing
here touches files.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from baruch.config import Config, Rule
from baruch.registry import INCOMPLETE, SKIPPED, Registry
from baruch.scan import DIR, FILES_UNIT, TreeScan, UnitScan, excluded
from baruch.sizes import human, percent

# Kinds of unit operations
FIRST = "first"  # no data on this drive yet
SYNC = "sync"  # recorded copy, synced file by file
UNTRACKED = "untracked"  # data on the drive the registry doesn't know, home here
CLEANUP = "cleanup"  # old copy of a unit whose home is elsewhere
GONE = "gone"  # unit's folder is gone on the NAS

# Execution groups, in order
G_CLEANUP, G_INCOMPLETE, G_PINNED, G_SPREAD, G_PENDING, G_NEW = range(6)

RESERVED_ROOT = {".baruch", "lost+found"}


@dataclass
class UnitOp:
    unit: str
    rule: str
    kind: str
    group: int
    size: int  # size on the NAS (0 for cleanups and gone units)
    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    write_bytes: int = 0
    overwrite_bytes: int = 0
    delete_bytes: int = 0
    delete_files: int = 0  # files and links among `delete`
    files: int = 0  # files and links on the NAS (for first copies)
    copy_files: int = 0  # files and links among `new` and `changed`
    placement: bool = False  # a first copy: needs room above the reserve
    note: str = ""
    loss: float = 0.0  # share of the unit's bytes on the drive that are deleted
    stale: bool = False  # recorded copy is skipped, incomplete or unverified

    @property
    def transfer(self) -> list[str]:
        return sorted(set(self.new) | set(self.changed))

    @property
    def net(self) -> int:
        return self.write_bytes - self.overwrite_bytes - self.delete_bytes

    @property
    def needs(self) -> int:
        """Bytes needed beyond what the deletions free, for the fit check."""
        return self.write_bytes - self.overwrite_bytes

    @property
    def counts_for_warnings(self) -> bool:
        return self.kind in (SYNC, UNTRACKED, GONE)


@dataclass
class Orphan:
    path: str  # relative to the drive root
    size: int
    note: str
    keep: bool = False  # prune keeps it (e.g. its home has no confirmed copy)
    unit: str | None = None


@dataclass
class Waiting:
    unit: str
    drive: str
    size: int
    here: bool = False  # the docked drive could take it instead


@dataclass
class Suggestion:
    unit: str
    to: str
    size: int
    newest: int


@dataclass
class Plan:
    drive: str
    takes_units: bool
    reserve: int
    free: int
    ops: list[UnitOp] = field(default_factory=list)
    new_units: dict[str, str | None] = field(default_factory=dict)
    home_changes: dict[str, str | None] = field(default_factory=dict)
    gone: list[str] = field(default_factory=list)
    confirm_unverified: list[str] = field(default_factory=list)
    drop_copies: list[str] = field(default_factory=list)  # records without data here
    skipped: list[tuple[str, str]] = field(default_factory=list)
    record_skipped: list[str] = field(default_factory=list)
    in_sync: list[str] = field(default_factory=list)  # home here, nothing to do
    waiting: list[Waiting] = field(default_factory=list)
    nowhere: list[tuple[str, int]] = field(default_factory=list)
    markers_missing: list[Rule] = field(default_factory=list)
    scan_errors: list[tuple[str, str]] = field(default_factory=list)
    drive_errors: list[tuple[str, str]] = field(default_factory=list)
    home_old: dict[str, str | None] = field(default_factory=dict)
    unreadable: list[tuple[str, str]] = field(default_factory=list)
    orphans: list[Orphan] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    free_after: int = 0
    suggestions: list[Suggestion] = field(default_factory=list)
    sizes: dict[str, tuple[int, int]] = field(default_factory=dict)  # size, newest

    @property
    def empty(self) -> bool:
        return not self.ops

    def registry_changes(self) -> list[str]:
        """Changes to the registry beyond the unit operations, shown in the
        plan; they also need a y."""
        out = []
        for name, home in sorted(self.home_changes.items()):
            old = self.home_old.get(name) or "none"
            if any(op.unit == name for op in self.ops):
                continue
            out.append(f"{name}: home {old} → {home or 'none (placed again)'}")
        for name, home in sorted(self.new_units.items()):
            if home and home != self.drive:
                out.append(f"{name}: new unit, home {home}")
        for name in self.drop_copies:
            out.append(f"{name}: copy record on {self.drive} dropped (no data here)")
        for name in self.confirm_unverified:
            out.append(f"{name}: copy on {self.drive} confirmed")
        op_units = {op.unit for op in self.ops}
        for name in self.gone:
            if name not in op_units and name not in self.drop_copies:
                out.append(f"{name}: gone on the NAS (no copy on {self.drive})")
        return out

    @property
    def needs_answer(self) -> bool:
        return bool(
            self.ops
            or self.registry_changes()
            or self.record_skipped
            or any(h is None for h in self.new_units.values())
        )

    @property
    def reserve_used(self) -> int:
        return max(0, self.reserve - self.free_after) if self.takes_units else 0


def diff(nas: UnitScan | None, disk: UnitScan | None, op: UnitOp) -> None:
    nas_e = nas.entries if nas else {}
    disk_e = disk.entries if disk else {}
    for rel, e in nas_e.items():
        d = disk_e.get(rel)
        if d is None:
            op.new.append(rel)
            op.write_bytes += e.size
            op.copy_files += e.kind != DIR
        elif not e.same(d):
            if e.kind != d.kind:
                op.delete.append(rel)
                if d.kind != DIR:
                    op.delete_files += 1
                    op.delete_bytes += d.size
                op.new.append(rel)
                op.write_bytes += e.size
                op.copy_files += e.kind != DIR
            elif e.kind != DIR:
                op.copy_files += 1
                op.changed.append(rel)
                op.write_bytes += e.size
                op.overwrite_bytes += d.size
    for rel, d in disk_e.items():
        if rel in nas_e or (nas is not None and nas.blocked(rel)):
            continue
        op.delete.append(rel)
        if d.kind != DIR:
            op.delete_files += 1
            op.delete_bytes += d.size
    op.new.sort()
    op.changed.sort()
    op.delete.sort()


def has_data(u: UnitScan | None) -> bool:
    return bool(u and u.entries)


def make_plan(
    *,
    config: Config,
    reg: Registry,
    drive: str,
    capacity: int,
    free: int,
    nas: dict[str, TreeScan | None],
    disk: dict[str, TreeScan],
    root_names: list[str],
    skip: set[str] | None = None,
) -> Plan:
    skip = skip or set()
    conf = config.drives.get(drive)
    takes_units = conf is not None
    reserve = conf.reserve(capacity) if conf else 0
    plan = Plan(drive, takes_units, reserve, free)
    sim = free  # free space as the run goes on

    existing: list[UnitOp] = []
    pending: list[tuple[str, Rule, UnitScan, UnitScan | None]] = []
    to_place: list[tuple[str, Rule, UnitScan, UnitScan | None]] = []
    explained: set[str] = set()  # unit names whose data on this drive is accounted for

    for rule in config.rules:
        scan = nas.get(rule.name)
        if scan is None:
            plan.markers_missing.append(rule)
            continue
        if scan.root_error:
            plan.scan_errors.append((rule.name, scan.root_error))
            continue
        disk_scan = disk.get(rule.name) or TreeScan()
        if disk_scan.root_error:
            plan.drive_errors.append((rule.name, disk_scan.root_error))
            continue
        for rel, msg in scan.errors:
            plan.scan_errors.append((f"{rule.name}/{rel}", msg))
        known = {name.split("/", 1)[1] for name in reg.units_of_rule(rule.name)}
        for folder in sorted(set(scan.units) | known):
            full = f"{rule.name}/{folder}"
            nu = scan.units.get(folder)
            du = disk_scan.units.get(folder)
            ru = reg.units.get(full)
            if (
                folder == FILES_UNIT
                and nu is not None
                and not nu.entries
                and ru is None
                and not has_data(du)
            ):
                continue
            if nu is not None:
                plan.sizes[full] = (nu.size, nu.newest)
                for rel, msg in nu.errors:
                    if rel == nu.name:
                        continue
                    plan.scan_errors.append((f"{rule.name}/{rel}", msg))
            if nu is not None and nu.unreadable:
                plan.unreadable.append((full, nu.unreadable))
                explained.add(full)
                continue

            if du is not None and du.errors:
                plan.drive_errors.extend((f"{rule.name}/{p}", m) for p, m in du.errors)
                explained.add(full)
                continue
            if full in skip and nu is None:
                plan.skipped.append((full, "skipped on request"))
                explained.add(full)
                continue

            if nu is None:  # gone on the NAS
                plan.gone.append(full)
                explained.add(full)
                if has_data(du):
                    op = UnitOp(
                        full, rule.name, GONE, G_CLEANUP, 0, note="gone on the NAS"
                    )
                    diff(None, du, op)
                    op.loss = 1.0
                    plan.ops.append(op)
                    sim += op.delete_bytes
                elif ru and drive in ru.copies:
                    plan.drop_copies.append(full)
                continue

            # Where the unit should be.
            home = ru.home if ru else None
            if rule.pinned:
                if home != rule.pin and ru is not None:
                    plan.home_changes[full] = rule.pin
                home = rule.pin
            elif home is not None and home not in (rule.spread or []):
                plan.home_changes[full] = None
                home = None
            if ru is None:
                plan.new_units[full] = home
            if full in plan.home_changes:
                plan.home_old[full] = ru.home if ru else None

            # Unverified copies: a home copy is confirmed by its sync; another
            # one by its data being here. Without data the record goes.
            here = ru.copies.get(drive) if ru else None
            if here and here.unverified:
                if not has_data(du):
                    plan.drop_copies.append(full)
                elif home != drive:
                    plan.confirm_unverified.append(full)

            if full in skip:
                plan.skipped.append((full, "skipped on request"))
                explained.add(full)
                if home == drive and here and has_data(du):
                    probe = UnitOp(full, rule.name, SYNC, G_SPREAD, nu.size)
                    diff(nu, du, probe)
                    if probe.transfer or probe.delete:
                        plan.record_skipped.append(full)
                continue

            if home == drive:
                explained.add(full)
                if not takes_units:
                    plan.skipped.append((full, f"{drive} isn't listed under drives"))
                    continue
                if has_data(du):
                    recorded = ru is not None and drive in ru.copies
                    kind = SYNC if recorded else UNTRACKED
                    incomplete = recorded and ru.copies[drive].result == INCOMPLETE
                    group = (
                        G_INCOMPLETE
                        if incomplete
                        else (G_PINNED if rule.pinned else G_SPREAD)
                    )
                    op = UnitOp(full, rule.name, kind, group, nu.size)
                    op.stale = recorded and (
                        ru.copies[drive].result in (SKIPPED, INCOMPLETE)
                        or ru.copies[drive].unverified
                    )
                    diff(nu, du, op)
                    op.files = nu.files
                    op.loss = op.delete_bytes / du.size if du.size else 0.0
                    existing.append(op)
                elif ru is None:
                    to_place.append((full, rule, nu, du))
                else:
                    pending.append((full, rule, nu, du))
            elif home is None:
                to_place.append((full, rule, nu, du))
            else:
                # Home is elsewhere: a copy here is an old one.
                if has_data(du) and ru and drive in ru.copies:
                    explained.add(full)
                    home_copy = ru.copies.get(home)
                    here_synced = ru.copies[drive].synced
                    newer = here_synced is None or (
                        home_copy is not None
                        and home_copy.synced is not None
                        and home_copy.synced >= here_synced
                    )
                    if home_copy and home_copy.confirmed and newer:
                        why = (
                            f"moved to {home}"
                            if not rule.pinned
                            else f"pin moved to {home}"
                        )
                        op = UnitOp(
                            full,
                            rule.name,
                            CLEANUP,
                            G_CLEANUP,
                            0,
                            note=f"{why}, copy confirmed there on "
                            f"{home_copy.synced:%Y-%m-%d}",
                        )
                        diff(None, du, op)
                        plan.ops.append(op)
                        sim += op.delete_bytes

    # Existing units on this drive: incomplete, pinned, spread; largest first.
    for op in sorted(existing, key=lambda o: (o.group, -o.size, o.unit)):
        if not (op.transfer or op.delete):
            if op.kind == UNTRACKED or op.group == G_INCOMPLETE or op.stale:
                plan.ops.append(op)  # recorded on y, even without file changes
            else:
                plan.in_sync.append(op.unit)
            continue
        if op.needs - op.delete_bytes > sim:
            plan.skipped.append((op.unit, f"doesn't fit ({_need(op.net, sim)})"))
            plan.record_skipped.append(op.unit)
            continue
        sim -= op.net
        plan.ops.append(op)

    # Copy pending: home is here, no data yet.
    for full, rule, nu, du in sorted(pending, key=lambda t: (-t[2].size, t[0])):
        if sim - nu.size < reserve:
            plan.skipped.append(
                (full, f"doesn't fit ({_need(nu.size, sim - reserve)})")
            )
            continue
        op = UnitOp(full, rule.name, FIRST, G_PENDING, nu.size, placement=True)
        diff(nu, du, op)
        op.files = nu.files
        sim -= op.net
        plan.ops.append(op)

    # New units and units without a home, largest first.
    for full, rule, nu, du in sorted(to_place, key=lambda t: (-t[2].size, t[0])):
        net = nu.size
        if has_data(du):
            probe = UnitOp(full, rule.name, UNTRACKED, G_NEW, nu.size)
            diff(nu, du, probe)
            net = probe.net
        target, wait_for, here_ok = _place(
            config, reg, rule, drive, takes_units, sim, reserve, net, nu.size
        )
        if target == drive:
            explained.add(full)
            if full in plan.new_units:
                plan.new_units[full] = drive
            else:
                plan.home_changes[full] = drive
            kind = UNTRACKED if has_data(du) else FIRST
            op = UnitOp(full, rule.name, kind, G_NEW, nu.size, placement=True)
            diff(nu, du, op)
            op.files = nu.files
            if du and du.size:
                op.loss = op.delete_bytes / du.size
            sim -= op.net
            plan.ops.append(op)
        elif target is not None:
            # A pinned unit whose pin is another drive: recorded, copied there.
            continue
        elif wait_for:
            plan.waiting.append(Waiting(full, wait_for, nu.size, here_ok))
        else:
            plan.nowhere.append((full, nu.size))

    plan.free_after = sim
    _orphans(plan, config, reg, drive, disk, root_names, explained)
    _warnings(plan, config, disk)
    if takes_units and plan.reserve_used:
        plan.suggestions = suggest_moves(
            config, reg, drive, plan.reserve - plan.free_after, plan.sizes
        )
    plan.ops.sort(key=lambda o: (o.group, -o.size, o.unit))
    return plan


def _need(need: int, room: int) -> str:
    return f"needs {human(need)}, {human(max(room, 0))} free"


def _place(config, reg, rule, drive, takes_units, sim, reserve, net, size):
    """Returns (target drive, drive it waits for, could go here instead)."""
    if rule.pinned:
        return rule.pin, None, False
    here_fits = takes_units and drive in rule.spread and sim - net >= reserve
    for name in rule.spread:
        conf = config.drives.get(name)
        if conf is None:
            continue
        if name == drive:
            if here_fits:
                return drive, None, False
            continue
        rec = reg.active_drive(name)
        if rec is None:
            continue
        if rec.free - conf.reserve(rec.capacity) < size:
            continue  # closed for this unit
        later = (
            rule.spread.index(drive) > rule.spread.index(name)
            if drive in rule.spread
            else False
        )
        return None, name, here_fits and later
    return None, None, False


def _orphans(plan, config, reg, drive, disk, root_names, explained):
    exclude = config.settings.exclude
    missing = {r.name for r in plan.markers_missing}
    active = {r.name for r in config.rules} - missing
    for name in root_names:
        if name in RESERVED_ROOT or excluded(name, exclude) or name in missing:
            continue
        scan = disk.get(name)
        size = sum(u.size for u in scan.units.values()) if scan else 0
        if name not in active:
            plan.orphans.append(Orphan(name, size, "orphaned: no rule of this name"))
            continue
        if any(r.name == name for r in config.rules) and plan_rule_blocked(plan, name):
            continue
        for folder, du in sorted((scan.units if scan else {}).items()):
            if not has_data(du) or excluded(folder, exclude):
                continue
            full = f"{name}/{folder}"
            if full in explained:
                continue
            ru = reg.units.get(full)
            path = name if folder == FILES_UNIT else f"{name}/{folder}"
            if folder == FILES_UNIT:
                path = f"{name}/(files)"
            if ru is None and full not in plan.new_units:
                plan.orphans.append(
                    Orphan(path, du.size, "orphaned: not on the NAS", unit=full)
                )
                continue
            home = plan.home_changes.get(
                full, ru.home if ru else plan.new_units.get(full)
            )
            home_copy = ru.copies.get(home) if (ru and home) else None
            if home and home_copy and home_copy.confirmed:
                note = (
                    f"orphaned: existing folder, not recorded; home {home} has a "
                    "confirmed copy"
                )
                plan.orphans.append(Orphan(path, du.size, note, unit=full))
            else:
                where = f"home {home}" if home else "it has no home yet"
                note = (
                    f"orphaned: existing folder, not recorded; {where}, no confirmed "
                    "copy"
                )
                plan.orphans.append(Orphan(path, du.size, note, keep=True, unit=full))


def plan_rule_blocked(plan, rule_name: str) -> bool:
    return any(path == rule_name for path, _ in plan.scan_errors)


def _warnings(plan: Plan, config: Config, disk: dict[str, TreeScan]) -> None:
    s = config.settings
    total_files = 0
    total_bytes = 0
    for scan in disk.values():
        for u in scan.units.values():
            total_files += u.files
            total_bytes += u.size
    changed_files = 0
    changed_bytes = 0
    for op in plan.ops:
        if not op.counts_for_warnings:
            continue
        changed_files += len(op.changed) + op.delete_files
        changed_bytes += op.overwrite_bytes + op.delete_bytes
    if total_files and changed_files / total_files > s.warn_changed_files:
        plan.warnings.append(
            f"WARNING: {changed_files} of {total_files} files "
            f"({percent(changed_files, total_files)}) on {plan.drive} would be "
            "overwritten or deleted"
        )
    if total_bytes and changed_bytes / total_bytes > s.warn_changed_bytes:
        plan.warnings.append(
            f"WARNING: {human(changed_bytes)} of {human(total_bytes)} "
            f"({percent(changed_bytes, total_bytes)}) on {plan.drive} would be "
            "overwritten or deleted"
        )
    for op in plan.ops:
        if op.counts_for_warnings and op.loss > s.warn_unit_loss:
            why = " (gone on the NAS)" if op.kind == GONE else ""
            plan.warnings.append(
                f"WARNING: {op.unit} would lose {op.loss:.0%} of its content{why}"
            )


def suggest_moves(config, reg, drive, need, sizes) -> list[Suggestion]:
    """The fewest units that bring the drive back to its reserve, preferring
    those that haven't changed for the longest time."""
    candidates = []
    for name, unit in reg.units.items():
        if unit.home != drive:
            continue
        rule = config.rule(name.split("/", 1)[0])
        if rule is None or rule.pinned:
            continue
        size, newest = sizes.get(
            name, (unit.copies.get(drive).size if drive in unit.copies else 0, 0)
        )
        if size > 0:
            candidates.append((newest, -size, name, rule, size))
    taken: dict[str, int] = {}

    def destination(rule, size):
        for target in rule.spread:
            if target == drive:
                continue
            conf = config.drives.get(target)
            rec = reg.active_drive(target)
            if conf is None or rec is None:
                continue
            if rec.free - conf.reserve(rec.capacity) - taken.get(target, 0) >= size:
                return target
        return None

    # One unit that's enough on its own, the one unchanged for longest...
    for newest, _, name, rule, size in sorted(candidates):
        if size >= need:
            target = destination(rule, size)
            if target:
                return [Suggestion(name, target, size, newest)]
    # ...otherwise the largest first, so it takes the fewest units.
    out: list[Suggestion] = []
    freed = 0
    for newest, _, name, rule, size in sorted(candidates, key=lambda c: (c[1], c[0])):
        if freed >= need:
            break
        target = destination(rule, size)
        if target:
            taken[target] = taken.get(target, 0) + size
            out.append(Suggestion(name, target, size, newest))
            freed += size
    return out
