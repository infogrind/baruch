"""The commands. Each takes a Context and parsed arguments and returns an
exit code; problems are raised as Fail and printed by the CLI."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

from baruch import names
from baruch.config import MARKER, Config, ConfigError, check, load
from baruch.context import Context
from baruch.drives import BARUCH_DIR, Docked, classify, docked, read_label, write_label
from baruch.execute import (
    DriveFull,
    DriveGone,
    RsyncError,
    check_rsync,
    ensure_mounted,
    mark_incomplete,
    record,
    run_rsync,
    sync_unit,
)
from baruch.planner import CLEANUP, GONE, Plan, make_plan, suggest_moves
from baruch.registry import (
    ERRORS,
    INCOMPLETE,
    OK,
    SKIPPED,
    Copy,
    Drive,
    Registry,
    RegistryError,
    Store,
    Unit,
    dump,
    folder_of,
    parse,
    rule_of,
    write_atomic,
)
from baruch.render import (
    render_plan,
    render_summary,
    reserve_line,
    suggestion_line,
)
from baruch.scan import FILE, FILES_UNIT, TreeScan, scan_drive_root, scan_tree
from baruch.sizes import human
from baruch.timefmt import from_text, show, to_text

LOG_DAYS = 90


class Fail(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# Helpers


class Out:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def __call__(self, text: str = "") -> None:
        self.ctx.stdout.write(text + "\n")

    def raw(self, text: str) -> None:
        self.ctx.stdout.write(text)

    def ask(self, question: str) -> bool:
        return self.read(f"{question} [y/n] ").strip().lower() in ("y", "yes")

    def read(self, prompt: str) -> str:
        self.ctx.stdout.write(prompt)
        self.ctx.stdout.flush()
        answer = self.ctx.stdin.readline()
        if not self.ctx.stdin.isatty():
            self.ctx.stdout.write("\n")
        return answer.strip()


def config_path(ctx: Context) -> Path:
    return ctx.config_dir / "config.yaml"


def get_config(ctx: Context) -> Config:
    try:
        return load(config_path(ctx))
    except ConfigError as e:
        raise Fail("Configuration problems:\n" + _bullets(e.problems), 2) from None


def _bullets(items: list[str]) -> str:
    return "\n".join(f"  - {p}" for p in items)


def store_of(ctx: Context) -> Store:
    return Store(ctx.state_dir, ctx.host.now)


def get_registry(store: Store) -> Registry:
    if not store.exists():
        raise Fail(
            f"No registry at {store.path}. Create one with `baruch init`, or restore "
            "one from a drive with `baruch registry restore`."
        )
    try:
        return store.load()
    except RegistryError as e:
        raise Fail(
            f"{e}. Baruch won't start with an empty registry: fix it, or restore one "
            "from a drive with `baruch registry restore`."
        ) from None


def lock(ctx: Context, store: Store) -> None:
    if not store.lock():
        raise Fail("Another Baruch command is running.")
    ctx.locks.append(store)


def choose_drive(ctx: Context, out: Out, reg: Registry, name: str | None) -> Docked:
    found = docked(ctx.host, reg)
    usable = [d for d in found if d.usable]
    if name:
        for d in usable:
            if d.name == name:
                return d
        raise Fail(f"{name} isn't docked.\n" + _volumes(found))
    if not usable:
        raise Fail("No usable drive docked.\n" + _volumes(found))
    if len(usable) == 1:
        return usable[0]
    choices = ", ".join(d.name or "" for d in usable)
    answer = out.read(f"Which drive? ({choices}) ")
    for d in usable:
        if d.name == answer:
            return d
    raise Fail(f"No docked drive named {answer!r}.")


def _volumes(found: list[Docked]) -> str:
    if not found:
        return "  (no USB volumes mounted)"
    return "\n".join(f"  {d.path.name}: {d.reason()}" for d in found)


def scan_nas(config: Config) -> dict[str, TreeScan | None]:
    out: dict[str, TreeScan | None] = {}
    for rule in config.rules:
        if not (rule.source / MARKER).exists():
            out[rule.name] = None
        else:
            out[rule.name] = scan_tree(rule.source, config.settings.exclude)
    return out


def scan_disk(root: Path, config: Config) -> tuple[dict[str, TreeScan], list[str]]:
    names_ = scan_drive_root(root)
    disk = {}
    for name in names_:
        if name == BARUCH_DIR:
            continue
        p = root / name
        if p.is_dir() and not p.is_symlink():
            disk[name] = scan_tree(p, [])
    return disk, names_


def _log_dir(ctx: Context) -> Path:
    d = ctx.state_dir / "logs"
    d.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - LOG_DAYS * 86400
    for p in d.iterdir():
        if p.stat().st_mtime < cutoff:
            p.unlink()
    return d


def new_log(ctx: Context, drive: str) -> Path:
    stamp = ctx.host.now().strftime("%Y%m%dT%H%M%S")
    path = _log_dir(ctx) / f"{stamp}-{drive}.log"
    n = 1
    while path.exists():
        n += 1
        path = path.with_name(f"{stamp}-{drive}-{n}.log")
    return path


def write_contents(ctx: Context, d: Docked) -> None:
    lines = []
    for dirpath, dirnames, filenames in os.walk(d.path):
        rel_dir = os.path.relpath(dirpath, d.path)
        if rel_dir == ".":
            dirnames[:] = [n for n in dirnames if n != BARUCH_DIR]
        for name in sorted(filenames) + sorted(
            n for n in dirnames if os.path.islink(os.path.join(dirpath, n))
        ):
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            rel = os.path.normpath(os.path.join(rel_dir, name))
            lines.append(f"{names.escape(rel)}\t{st.st_size}\t{int(st.st_mtime)}")
    text = "\n".join(sorted(lines)) + "\n"
    write_atomic(d.path / BARUCH_DIR / "contents.txt", text)
    write_atomic(ctx.state_dir / "contents" / f"{d.name}.txt", text)


def write_scan_state(ctx: Context, plan: Plan, drive: str) -> None:
    path = ctx.state_dir / "scan.yaml"
    try:
        old = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        old = {}
    orphans = old.get("orphans") or {}
    orphans[drive] = [
        {"path": names.escape(o.path), "size": o.size, "note": o.note}
        for o in plan.orphans
    ]
    doc = {
        "time": to_text(ctx.host.now()),
        "units": {
            names.escape(n): {"size": s, "newest": m}
            for n, (s, m) in sorted(plan.sizes.items())
        },
        "gone": [names.escape(n) for n in plan.gone],
        "orphans": orphans,
    }
    write_atomic(path, yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))


def read_scan_state(ctx: Context) -> dict:
    try:
        doc = yaml.safe_load((ctx.state_dir / "scan.yaml").read_text()) or {}
    except FileNotFoundError:
        return {}
    doc["units"] = {names.unescape(k): v for k, v in (doc.get("units") or {}).items()}
    doc["gone"] = [names.unescape(n) for n in doc.get("gone") or []]
    return doc


def drop_unused_drives(reg: Registry, config: Config) -> None:
    for name in list(reg.drives):
        if name in config.drives:
            continue
        if not any(name in u.copies for u in reg.units.values()):
            del reg.drives[name]


# init, config check


def cmd_init(ctx: Context, args) -> int:
    out = Out(ctx)
    store = store_of(ctx)
    if store.exists():
        raise Fail(f"A registry already exists at {store.path}.")
    lock(ctx, store)
    store.create(Registry())
    out(f"Created an empty registry at {store.path}.")
    return 0


def cmd_config_check(ctx: Context, args) -> int:
    out = Out(ctx)
    config = get_config(ctx)
    problems = check(config)
    if problems:
        out("Configuration problems:\n" + _bullets(problems))
        return 1
    out(f"Configuration OK: {len(config.rules)} rules, {len(config.drives)} drives.")
    return 0


# run


def cmd_run(ctx: Context, args) -> int:
    out = Out(ctx)
    config = get_config(ctx)
    store = store_of(ctx)
    reg = get_registry(store)
    if not args.dry_run and not ctx.interactive:
        raise Fail(
            "`baruch run` needs a terminal: it shows the plan and asks before "
            "changing anything."
        )
    d = choose_drive(ctx, out, reg, args.drive)
    if not args.dry_run:
        lock(ctx, store)
    rsync = ctx.host.rsync()
    try:
        check_rsync(rsync)
    except RsyncError as e:
        raise Fail(str(e)) from None
    capacity, free = ctx.host.disk_usage(d.path)
    nas = scan_nas(config)
    disk, root_names = scan_disk(d.path, config)
    plan = make_plan(
        config=config,
        reg=reg,
        drive=d.name,
        capacity=capacity,
        free=free,
        nas=nas,
        disk=disk,
        root_names=root_names,
        skip=set(args.skip or []),
    )
    text = render_plan(plan, reg, d.path.name)
    out.raw(text)
    tail = render_summary(plan) if not plan.empty else ""
    if args.dry_run:
        out.raw(tail)
        for w in plan.warnings:
            out(w)
        out("Dry run: nothing changed.")
        return 0

    log = new_log(ctx, d.name)
    log.write_text(text + tail + "".join(w + "\n" for w in plan.warnings))
    if not plan.empty:
        out.raw(tail)
        out(f"Plan saved to {log}")
        for w in plan.warnings:
            out(w)
        if not out.ask("Carry out this plan?"):
            out("Aborted, nothing changed.")
            return 0

    apply_registry_changes(reg, plan, d.name, ctx.host.now())
    store.save(reg)
    code = execute(ctx, out, config, reg, store, d, plan, rsync, log)
    return code


def apply_registry_changes(reg: Registry, plan: Plan, drive: str, now) -> None:
    today = now.date()
    for name, home in plan.new_units.items():
        reg.units[name] = Unit(home, today)
    for name, home in plan.home_changes.items():
        if name in reg.units:
            reg.units[name].home = home
    for name in plan.confirm_unverified:
        reg.units[name].copies[drive].unverified = False
    for name in plan.drop_copies:
        if name in reg.units:
            reg.units[name].copies.pop(drive, None)
    for name in plan.record_skipped:
        unit = reg.units.get(name)
        if unit and drive in unit.copies:
            old = unit.copies[drive]
            unit.copies[drive] = Copy(old.synced, SKIPPED, old.size)
    for name in plan.in_sync:
        unit = reg.units.get(name)
        copy = unit.copies.get(drive) if unit else None
        if copy and copy.result == OK:
            copy.synced = now


def execute(ctx, out, config, reg, store, d, plan, rsync, log) -> int:
    reserve = plan.reserve
    code = 0
    current = None
    messages: list[str] = []
    try:
        for op in plan.ops:
            current = op
            ensure_mounted(ctx.host, d)
            _, free_now = ctx.host.disk_usage(d.path)
            floor = reserve if op.placement else 0
            if free_now + op.delete_bytes - op.needs < floor:
                out(f"{names.display(op.unit)}: skipped, doesn't fit any more")
                unit = reg.units.get(op.unit)
                if unit and d.name in unit.copies:
                    record(reg, op, d.name, SKIPPED, None)
                    store.save(reg)
                continue
            rule = config.rule(op.rule)
            source = rule.source if rule else Path("/nonexistent")
            res = sync_unit(op, d, source, rsync)
            record(reg, op, d.name, res.result, ctx.host.now())
            store.save(reg)
            status = "done" if res.result == OK else "synced with errors"
            if op.kind in (CLEANUP, GONE):
                status = "deleted"
            out(f"{names.display(op.unit)}: {status}")
            for m in res.messages:
                out(f"  {m}")
            messages.extend(f"{op.unit}: {m}" for m in res.messages)
            current = None
    except KeyboardInterrupt:
        if current is not None and current.kind not in (CLEANUP, GONE):
            mark_incomplete(reg, current, d.name)
        store.save(reg)
        out("Interrupted: finished units stay recorded; the next run continues.")
        code = 130
    except DriveGone as e:
        store.save(reg)
        out(str(e))
        _append(log, str(e))
        return 1
    except (DriveFull, RsyncError) as e:
        if current is not None and current.kind not in (CLEANUP, GONE):
            mark_incomplete(reg, current, d.name)
        store.save(reg)
        out(f"{names.display(current.unit) if current else ''}: {e}")
        out("The run stopped for this drive.")
        code = 1
    for name in plan.gone:
        unit = reg.units.get(name)
        if unit and not unit.copies:
            del reg.units[name]
    finish(ctx, out, config, reg, store, d, plan)
    _append(log, "\n".join(messages) + f"\nfinished with exit code {code}\n")
    return code


def _append(log: Path, text: str) -> None:
    with log.open("a") as f:
        f.write(text + "\n")


def finish(ctx, out, config, reg, store, d, plan) -> None:
    now = ctx.host.now()
    write_contents(ctx, d)
    shutil.copyfile(config_path(ctx), d.path / BARUCH_DIR / "config.yaml")
    capacity, free = ctx.host.disk_usage(d.path)
    drive = reg.drives[d.name]
    drive.capacity, drive.free, drive.last_seen = capacity, free, now
    complete = all(
        u.copies.get(d.name) is not None and u.copies[d.name].result in (OK, ERRORS)
        for name, u in reg.units.items()
        if u.home == d.name and name not in plan.gone
    )
    if complete:
        drive.last_complete_run = now
    drop_unused_drives(reg, config)
    store.save(reg)
    write_atomic(d.path / BARUCH_DIR / "registry.yaml", dump(reg))
    write_scan_state(ctx, plan, d.name)
    if plan.reserve_used:
        out(reserve_line(plan.reserve_used, plan.reserve))
        for s in plan.suggestions:
            out(suggestion_line(s))
    used = capacity - free
    out(f"{d.name}: {human(used)} used, {human(free)} free.")
    out("safe to eject in DSM")


# status


def unit_state(name: str, unit: Unit, gone: set[str]) -> str:
    if name in gone:
        return "gone"
    if unit.home is None:
        return "waiting"
    if unit.home not in unit.copies:
        return "copy pending"
    if unit.copies[unit.home].result in (SKIPPED, INCOMPLETE):
        return (
            "incomplete" if unit.copies[unit.home].result == INCOMPLETE else "outdated"
        )
    if any(drive != unit.home for drive in unit.copies):
        return "cleanup pending"
    return "in place"


def cmd_status(ctx: Context, args) -> int:
    out = Out(ctx)
    config = get_config(ctx)
    store = store_of(ctx)
    reg = get_registry(store)
    scan = read_scan_state(ctx)
    now = ctx.host.now()
    gone = set(scan.get("gone") or [])

    out("Drives:")
    total_free = 0
    for name in sorted(set(reg.drives) | set(config.drives)):
        drive = reg.drives.get(name)
        if drive is None or drive.forgotten:
            out(f"  {name}: not registered")
            continue
        conf = config.drives.get(name)
        reserve = conf.reserve(drive.capacity) if conf else 0
        total_free += max(0, drive.free - reserve)
        syncs = [
            u.copies[name].synced
            for u in reg.units.values()
            if name in u.copies and u.copies[name].synced
        ]
        oldest = show(min(syncs)) if syncs else "none"
        out(
            f"  {name}: last complete run {show(drive.last_complete_run)}, last seen "
            f"{show(drive.last_seen)}, oldest unit sync {oldest}"
        )
        line = f"    capacity {human(drive.capacity)}, free {human(drive.free)}"
        if conf:
            used = max(0, reserve - drive.free)
            share = used / reserve if reserve else 0
            line += f", reserve {human(reserve)} ({share:.0%} used)"
        line += f", last verify {show(drive.last_verify)}"
        out(line)
    out(f"Total free space above the reserves: {human(total_free)}")
    if scan.get("time"):
        t = from_text(scan["time"])
        days = (now - t).days
        out(f"Status from the last scan of the NAS: {show(t)} ({days} days ago)")
    else:
        out("No scan of the NAS yet (no run so far).")

    attention = []
    for name in sorted(reg.units):
        unit = reg.units[name]
        state = unit_state(name, unit, gone)
        notes = []
        if state != "in place":
            detail = state
            if state == "copy pending":
                detail += f" (home {unit.home})"
            notes.append(detail)
        if any(c.result == ERRORS for c in unit.copies.values()):
            notes.append("synced with errors")
        if not unit.copies and state != "gone":
            notes.append("no backup")
        if notes:
            attention.append(f"  {names.display(name)}: {', '.join(notes)}")
    if attention:
        out("Needs attention:")
        for line in attention:
            out(line)

    for name, drive in sorted(reg.drives.items()):
        if drive.forgotten:
            continue
        if name not in config.drives:
            n = sum(1 for u in reg.units.values() if name in u.copies)
            out(f"{name}: not in the configuration, still holds {n} copies")
        if drive.last_seen and now - drive.last_seen > config.settings.stale_after:
            out(f"{name}: not docked for {(now - drive.last_seen).days} days")
        conf = config.drives.get(name)
        if conf:
            reserve = conf.reserve(drive.capacity)
            if drive.free < reserve:
                out(f"{name}: " + reserve_line(reserve - drive.free, reserve))
                sizes = {
                    n: (v.get("size", 0), v.get("newest", 0))
                    for n, v in (scan.get("units") or {}).items()
                }
                for s in suggest_moves(config, reg, name, reserve - drive.free, sizes):
                    out(suggestion_line(s))
    for drive, items in sorted((scan.get("orphans") or {}).items()):
        if items:
            out(f"Orphaned data on {drive} (remove with `baruch prune {drive}`):")
            for o in items:
                out(f"  {names.display(names.unescape(o['path']))}: {o['note']}")
    return 0


# find


def cmd_find(ctx: Context, args) -> int:
    out = Out(ctx)
    store = store_of(ctx)
    reg = get_registry(store)
    pattern = args.pattern
    wild = any(c in pattern for c in "*?[")
    folder = ctx.state_dir / "contents"
    found: dict[str, list[str]] = {}
    lost: dict[str, list[str]] = {}
    for path in sorted(folder.glob("*.txt")) if folder.exists() else []:
        drive = path.stem
        active = reg.active_drive(drive) is not None
        for line in path.read_text(encoding="utf-8").splitlines():
            rel = names.unescape(line.split("\t", 1)[0])
            hit = (
                fnmatch.fnmatch(rel, pattern)
                or fnmatch.fnmatch(Path(rel).name, pattern)
                if wild
                else pattern.casefold() in rel.casefold()
            )
            if hit:
                (found if active else lost).setdefault(rel, []).append(drive)
    if not found and not lost:
        out("No matching files on any drive.")
        return 0
    for rel in sorted(found):
        parts = rel.split("/")
        unit_name = (
            f"{parts[0]}/{parts[1]}" if len(parts) > 2 else f"{parts[0]}/{FILES_UNIT}"
        )
        unit = reg.units.get(unit_name)
        for drive in found[rel]:
            copy = unit.copies.get(drive) if unit else None
            when = (
                f"synced {show(copy.synced)}"
                if copy and copy.synced
                else "not recorded"
            )
            home = " (home)" if unit and unit.home == drive else ""
            out(f"{names.display(rel)}  {drive}{home}  {when}")
    for rel in sorted(set(lost) - set(found)):
        out(
            f"{names.display(rel)}  on no drive (was only on forgotten "
            f"{', '.join(lost[rel])})"
        )
    return 0


# move set


def cmd_move_set(ctx: Context, args) -> int:
    out = Out(ctx)
    config = get_config(ctx)
    store = store_of(ctx)
    reg = get_registry(store)
    name = args.unit
    unit = reg.units.get(name)
    if unit is None:
        similar = names.similar(name, list(reg.units))
        hint = f" Did you mean: {', '.join(similar)}?" if similar else ""
        raise Fail(f"No unit named {names.display(name)}.{hint}")
    rule = config.rule(rule_of(name))
    if rule is None:
        raise Fail(f"{names.display(name)}: its rule isn't in the configuration.")
    if rule.pinned:
        raise Fail(
            f"{names.display(name)} is pinned to {rule.pin}: change the pin in the "
            "configuration instead."
        )
    target = args.to
    rec = reg.active_drive(target)
    if rec is None:
        raise Fail(f"{target} is not registered.")
    if target not in (rule.spread or []):
        raise Fail(
            f"{target} is not in the spread list of rule {rule.name}: add it to the "
            "list first."
        )
    lock(ctx, store)
    scan = read_scan_state(ctx)
    size = (scan.get("units") or {}).get(name, {}).get("size")
    if size is None:
        size = max((c.size for c in unit.copies.values()), default=0)
    conf = config.drives.get(target)
    room = rec.free - (conf.reserve(rec.capacity) if conf else 0)
    if size > room:
        out(
            f"Warning: {names.display(name)} ({human(size)}) doesn't fit on {target} "
            f"({human(max(room, 0))} free above the reserve at its last backup)."
        )
    unit.home = target
    store.save(reg)
    out(
        f"{names.display(name)}: home is now {target}. It's copied there when "
        f"{target} is docked; the old copy is deleted after that."
    )
    return 0


# drive register / forget


def cmd_register(ctx: Context, args) -> int:
    out = Out(ctx)
    store = store_of(ctx)
    reg = get_registry(store)
    try:
        config = load(config_path(ctx))
    except ConfigError:
        config = None
    name = args.name
    found = docked(ctx.host, reg)
    candidates = [d for d in found if not d.usable]
    if not candidates:
        if found:
            raise Fail(
                "Every docked disk already belongs to a registered drive:\n"
                + _volumes(found)
            )
        raise Fail("No disk docked.")
    disk = candidates[0]
    if len(candidates) > 1:
        answer = out.read(
            "Which disk? (" + ", ".join(c.path.name for c in candidates) + ") "
        )
        disk = next((c for c in candidates if c.path.name == answer), None)
        if disk is None:
            raise Fail(f"No docked disk {answer!r}.")
    existing = reg.drives.get(name)
    if existing and not existing.forgotten:
        raise Fail(
            f"{name} is already registered to another disk. A new disk gets a new "
            "name (see 'Upgrading a drive' in the README)."
        )
    fs = ctx.host.fs_type(disk.path)
    if fs != "ext4":
        raise Fail(f"{disk.path.name} is {fs}, not ext4. Backup drives must be ext4.")
    if disk.status != "no label":
        label = read_label(disk.path)
        shown = label.name if hasattr(label, "name") else "unreadable"
        if not args.force:
            raise Fail(
                f"{disk.path.name} already has a label ({disk.reason()}). Use --force "
                "to overwrite it."
            )
        out(f"{disk.path.name} has a label: name {shown} ({disk.reason()}).")
        if not out.ask(f"Overwrite it with a new label for {name}?"):
            out("Nothing changed.")
            return 0
    lock(ctx, store)
    label = write_label(disk.path, name)
    capacity, free = ctx.host.disk_usage(disk.path)
    reg.drives[name] = Drive(label.disk_id, capacity, free, last_seen=ctx.host.now())
    store.save(reg)
    out(
        f"Registered {disk.path.name} as {name}: {human(capacity)}, {human(free)} free."
    )
    if config is not None and name not in config.drives:
        out(f"Note: {name} is not in the configuration yet; add it under drives.")
    if args.force:
        out("Old data on the disk is left as it was; delete it yourself if needed.")
    return 0


def cmd_forget(ctx: Context, args) -> int:
    out = Out(ctx)
    config = get_config(ctx)
    store = store_of(ctx)
    reg = get_registry(store)
    name = args.name
    drive = reg.drives.get(name)
    if drive is None or drive.forgotten:
        raise Fail(f"{name} isn't a registered drive.")
    held = sorted(n for n, u in reg.units.items() if name in u.copies)
    alone = sorted(n for n in held if set(reg.units[n].copies) == {name})
    out(f"Forgetting {name} drops its {len(held)} copies from the registry and makes")
    out("its label invalid. The data on the disk is left as it is.")
    if alone:
        out(f"{len(alone)} units would then have no backup.")
    if not out.ask(f"Forget {name}?"):
        out("Nothing changed.")
        return 0
    lock(ctx, store)
    for n in held:
        reg.units[n].copies.pop(name, None)
    drive.disk_id = None
    for n, unit in reg.units.items():
        rule = config.rule(rule_of(n))
        if unit.home == name and (rule is None or name not in rule.drives):
            unit.home = None
    drop_unused_drives(reg, config)
    store.save(reg)
    out(f"Forgot {name}.")
    if alone:
        out("Units that now have no backup:")
        for n in alone:
            out(f"  {names.display(n)}")
    return 0


# prune


def cmd_prune(ctx: Context, args) -> int:
    out = Out(ctx)
    config = get_config(ctx)
    store = store_of(ctx)
    reg = get_registry(store)
    d = choose_drive(ctx, out, reg, args.drive)
    lock(ctx, store)
    capacity, free = ctx.host.disk_usage(d.path)
    disk, root_names = scan_disk(d.path, config)
    plan = make_plan(
        config=config,
        reg=reg,
        drive=d.name,
        capacity=capacity,
        free=free,
        nas=scan_nas(config),
        disk=disk,
        root_names=root_names,
    )
    delete = [o for o in plan.orphans if not o.keep]
    keep = [o for o in plan.orphans if o.keep]
    if not plan.orphans:
        out(f"Nothing to prune on {d.name}.")
        return 0
    out(f"Prune plan for {d.name}:")
    for o in delete:
        out(f"  - {names.display(o.path)} ({human(o.size)}): {o.note}")
    for o in keep:
        out(f"  kept: {names.display(o.path)} ({human(o.size)}): {o.note}")
    if not delete:
        return 0
    total = sum(o.size for o in delete)
    on_drive = sum(u.size for s in disk.values() for u in s.units.values())
    if on_drive and total / on_drive > config.settings.warn_changed_bytes:
        out(
            f"WARNING: {human(total)} of {human(on_drive)} on {d.name} would be deleted"
        )
    if not out.ask("Delete these?"):
        out("Nothing changed.")
        return 0
    for o in delete:
        ensure_mounted(ctx.host, d)
        _remove(d.path, o.path)
        if o.unit and o.unit in reg.units:
            reg.units[o.unit].copies.pop(d.name, None)
    rules = {r.name for r in config.rules}
    for n in list(reg.units):
        unit = reg.units[n]
        if rule_of(n) in rules:
            continue
        if any(o.path == rule_of(n) for o in delete):
            unit.copies.pop(d.name, None)
        if not unit.copies:
            del reg.units[n]
    store.save(reg)
    write_contents(ctx, d)
    out(f"Deleted {len(delete)} items ({human(total)}).")
    return 0


def _remove(root: Path, rel: str) -> None:
    if rel.endswith("/" + FILES_UNIT):
        folder = root / rel.rsplit("/", 1)[0]
        for p in folder.iterdir():
            if not p.is_dir() or p.is_symlink():
                p.unlink()
        return
    p = root / rel
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    elif os.path.lexists(p):
        p.unlink()


# verify


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def cmd_verify(ctx: Context, args) -> int:
    out = Out(ctx)
    config = get_config(ctx)
    store = store_of(ctx)
    reg = get_registry(store)
    d = choose_drive(ctx, out, reg, args.drive)
    lock(ctx, store)
    read_errors: list[tuple[str, str]] = []  # rule, rel
    changed: list[str] = []
    unclear: list[tuple[Path, Path]] = []
    nas = scan_nas(config)
    for name in sorted(reg.units):
        unit = reg.units[name]
        if unit.home != d.name or d.name not in unit.copies:
            continue
        rule = config.rule(rule_of(name))
        scan = nas.get(rule.name) if rule else None
        if rule is None or scan is None:
            continue
        nu = scan.units.get(folder_of(name))
        if nu is None:
            continue
        for rel, e in nu.entries.items():
            if e.kind != FILE:
                continue
            drive_file = d.path / rule.name / rel
            nas_file = rule.source / rel
            try:
                st = os.stat(drive_file)
            except FileNotFoundError:
                changed.append(f"{rule.name}/{rel}")
                continue
            if st.st_size != e.size or int(st.st_mtime) != e.mtime:
                changed.append(f"{rule.name}/{rel}")
                continue
            try:
                drive_hash = _hash(drive_file)
            except OSError:
                read_errors.append((rule.name, rel))
                continue
            try:
                nas_hash = _hash(nas_file)
            except OSError:
                continue
            if drive_hash != nas_hash:
                unclear.append((nas_file, drive_file))
    if changed:
        out(
            f"{len(changed)} files changed on the NAS since the last sync (normal; "
            "the next run takes care of them):"
        )
        for rel in changed:
            out(f"  {names.display(rel)}")
    if unclear:
        out(
            f"{len(unclear)} files differ with the same size and time; it's unknown "
            "which side is right. Decide each one:"
        )
        for nas_file, drive_file in unclear:
            out(f"  {names.display(str(nas_file))}")
            out(f'    NAS → drive: rsync -a "{nas_file}" "{drive_file}"')
            out(f'    drive → NAS: rsync -a "{drive_file}" "{nas_file}"')
    if read_errors:
        out(f"{len(read_errors)} files can't be read on {d.name}:")
        for rule_name, rel in read_errors:
            out(f"  {names.display(rule_name + '/' + rel)}")
        out("Read errors are a sign the drive is failing; consider replacing it.")
        n = len(read_errors)
        if out.ask(f"copy these {n} unreadable files again from the NAS?"):
            rsync = ctx.host.rsync()
            by_rule: dict[str, list[str]] = {}
            for rule_name, rel in read_errors:
                by_rule.setdefault(rule_name, []).append(rel)
            for rule_name, rels in by_rule.items():
                rule = config.rule(rule_name)
                ensure_mounted(ctx.host, d)
                code, err = run_rsync(
                    rsync,
                    rule.source,
                    d.path / rule_name,
                    rels,
                    d.path / BARUCH_DIR / "partial",
                )
                if code != 0:
                    out(err)
            out("Copied again.")
    if not (changed or unclear or read_errors):
        out(f"All files of {d.name}'s units match the NAS.")
    reg.drives[d.name].last_verify = ctx.host.now()
    store.save(reg)
    return 0


# registry restore


def cmd_registry_restore(ctx: Context, args) -> int:
    out = Out(ctx)
    store = store_of(ctx)
    candidates = []
    for p in ctx.host.drive_paths():
        if not ctx.host.is_mount(p):
            continue
        snap = p / BARUCH_DIR / "registry.yaml"
        label = read_label(p)
        if snap.exists() and hasattr(label, "name"):
            if args.drive and label.name != args.drive:
                continue
            candidates.append((p, label, snap))
    if not candidates:
        raise Fail("No docked drive with a registry snapshot.")
    if len(candidates) > 1:
        raise Fail("Several drives with snapshots are docked: choose one with --drive.")
    path, label, snap = candidates[0]
    try:
        reg = parse(snap.read_text(encoding="utf-8"))
    except RegistryError as e:
        raise Fail(f"The snapshot on {label.name} can't be read: {e}") from None
    if classify(path, reg).status != "ok":
        raise Fail(f"The snapshot on {path.name} doesn't match its label.")
    when = datetime.fromtimestamp(snap.stat().st_mtime, UTC)
    out(f"Snapshot on {label.name}, written {show(when)}.")
    out("Copies on other drives are marked unverified until each is docked.")
    question = (
        "Replace the current registry with it?"
        if store.exists()
        else ("Use it as the registry?")
    )
    if not out.ask(question):
        out("Nothing changed.")
        return 0
    lock(ctx, store)
    for unit in reg.units.values():
        for drive, copy in unit.copies.items():
            if drive != label.name:
                copy.unverified = True
    store.save(reg)
    out(f"Registry restored from {label.name}.")
    return 0
