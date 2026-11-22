"""The commands. Each takes a Context and parsed arguments and returns an
exit code; problems are raised as Fail and printed by the CLI."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import time
from pathlib import Path

from baruch import cache, names, planner, records, stale
from baruch.cache import CacheError, DriveCache
from baruch.config import MARKER, Config, ConfigError, check, describe, load
from baruch.context import Context
from baruch.drives import (
    BARUCH_DIR,
    Docked,
    docked,
    still_there,
    write_label,
    write_to_drive,
)
from baruch.execute import (
    DriveState,
    clear_unit,
    ensure_mounted,
    finish_unit,
    remove_unit,
    start_unit,
    sync_unit,
)
from baruch.records import ERRORS, IN_PROGRESS, OK, Record, RecordsError
from baruch.registry import Drive, Registry, RegistryError, Store, folder_of, rule_of
from baruch.render import render_plan, render_summary, reserve_lines, stale_lines
from baruch.restore import script as restore_script
from baruch.rsync import DriveError, DriveGone, RsyncError, check_rsync, run_rsync
from baruch.scan import FILE, DriveScan, TreeScan, scan_drive, scan_tree
from baruch.sizes import count, human
from baruch.starter import STARTER
from baruch.timefmt import from_text, show, show_date, to_text

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
    hint = (
        f"Copy back a saved version from {store.versions}, or create a new one "
        "with `baruch init` and take over each drive with "
        "`baruch drive register NAME`."
    )
    if not store.exists():
        raise Fail(f"No registry at {store.path}. {hint}")
    try:
        return store.load()
    except RegistryError as e:
        raise Fail(f"{e}. Baruch won't start with an empty registry. {hint}") from None


def lock(ctx: Context, store: Store) -> None:
    if not store.lock():
        raise Fail(
            "Another Baruch command that changes things is running (a run, prune, "
            "…); try again when it's done."
        )
    ctx.locks.append(store)


def need_terminal(ctx: Context, what: str) -> None:
    if not ctx.interactive:
        raise Fail(
            f"`baruch {what}` needs a terminal: it asks before changing anything."
        )


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


def own_files(ctx: Context) -> list[Path]:
    return [ctx.config_dir, ctx.state_dir]


def own_state(ctx: Context, config: Config) -> dict[str, str]:
    """Baruch's state folder, per rule whose source holds it (several rules
    can share a source): rule -> path inside the rule's folder."""
    state = Path(os.path.realpath(ctx.state_dir))
    out = {}
    for rule in config.rules:
        source = Path(os.path.realpath(rule.source))
        if state.is_relative_to(source) and state != source:
            out[rule.name] = str(state.relative_to(source))
    return out


def load_caches(store: Store, reg: Registry, out: Out | None = None) -> dict:
    caches: dict[str, DriveCache] = {}
    for name, entry in reg.drives.items():
        try:
            c = cache.load(store.cache_dir, name)
        except CacheError as e:
            if out:
                out(f"Warning: {e}; treated as missing until {name} is docked.")
            continue
        if c is not None and c.disk_id == entry.disk_id:
            caches[name] = c
    return caches


def read_drive(config: Config, d: Docked) -> tuple[dict[str, Record], DriveScan]:
    try:
        recs = records.read(d.path)
    except RecordsError as e:
        raise Fail(
            f"{e}.\nBaruch never takes a drive's records as empty. To start this "
            f"drive's records afresh (its data then counts as unrecorded, never as a "
            f"backup, until a run syncs it): `baruch drive forget {d.name}`, then "
            f"`baruch drive register {d.name} --force`."
        ) from None
    return recs, scan_drive(d.path, config.settings.exclude)


def drive_state(ctx, config, store, d, recs, scan, now) -> DriveState:
    assert d.label is not None
    c = DriveCache(d.label.disk_id, now, dict(recs), scan)
    return DriveState(d, dict(recs), c, store.cache_dir, config.settings.exclude)


def _log_dir(ctx: Context) -> Path:
    d = ctx.state_dir / "logs"
    d.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - LOG_DAYS * 86400
    for p in d.iterdir():
        if p.stat().st_mtime < cutoff:
            p.unlink()
    return d


def new_log(ctx: Context, what: str) -> Path:
    stamp = ctx.host.now().strftime("%Y%m%dT%H%M%S")
    path = _log_dir(ctx) / f"{stamp}-{what}.log"
    n = 1
    while path.exists():
        n += 1
        path = path.with_name(f"{stamp}-{what}-{n}.log")
    return path


def _append(log: Path, text: str) -> None:
    with log.open("a", encoding="utf-8") as f:
        f.write(text + "\n")


def contents_text(scan: DriveScan) -> str:
    lines = []
    for top, tree in scan.folders.items():
        for u in tree.units.values():
            for rel, e in u.entries.items():
                if e.kind == "d":
                    continue
                lines.append(f"{names.escape(top + '/' + rel)}\t{e.size}\t{e.mtime}")
    for name, e in scan.loose.items():
        lines.append(f"{names.escape(name)}\t{e.size}\t{e.mtime}")
    return "\n".join(sorted(lines)) + "\n"


def _save_scan_state(ctx: Context, plan: planner.Plan, nas) -> None:
    """The NAS scan as `status` uses it: sizes, gone, unplaced units."""
    units = {}
    for rule, scan in nas.items():
        if scan is None or scan.root_error:
            continue
        for folder, u in scan.units.items():
            if not u.unreadable:
                units[names.escape(f"{rule}/{folder}")] = [u.size, u.newest]
    doc = {
        "time": to_text(ctx.host.now()),
        "units": units,
        "gone": [
            names.escape(u)
            for u in [*(u for us in plan.gone.values() for u in us), *plan.drop_gone]
        ],
        "unplaced": [[names.escape(u), s] for u, s in plan.unplaced],
        "markers_missing": plan.markers_missing,
    }
    from baruch.registry import write_atomic

    write_atomic(ctx.state_dir / "scan.json", json.dumps(doc))


def _read_scan_state(ctx: Context) -> dict:
    try:
        doc = json.loads((ctx.state_dir / "scan.json").read_text())
    except (FileNotFoundError, ValueError):
        return {}
    doc["units"] = {names.unescape(k): v for k, v in doc.get("units", {}).items()}
    doc["gone"] = [names.unescape(u) for u in doc.get("gone", [])]
    doc["unplaced"] = [(names.unescape(u), s) for u, s in doc.get("unplaced", [])]
    return doc


def _save_stale(ctx: Context, drive: str, items: list[stale.Item]) -> None:
    from baruch.registry import write_atomic

    path = ctx.state_dir / "stale.json"
    try:
        doc = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        doc = {}
    doc[drive] = [
        {
            "path": names.escape(i.path),
            "action": i.action,
            "reason": i.reason,
            "size": i.size,
            "only_files": i.only_files,
        }
        for i in items
    ]
    write_atomic(path, json.dumps(doc))


def _read_stale(ctx: Context) -> dict:
    try:
        doc = json.loads((ctx.state_dir / "stale.json").read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return {
        d: [{**i, "path": names.unescape(i["path"])} for i in items]
        for d, items in doc.items()
    }


def end_writes(ctx, out, config, config_text, reg, store, state, nas, now):
    """Records are already on the drive. Now the contents list, the config
    copy, the restore script and the cache; the drive's entry; the registry.
    Only while the drive is still there."""
    d = state.docked
    entry = reg.drives[d.name]
    if not still_there(ctx.host, d):
        store.save(reg)
        out(
            f"{d.name} isn't mounted any more: nothing more was written to it, and "
            "its cache and free space stay as they were."
        )
        return None
    shutil.rmtree(d.path / BARUCH_DIR / "partial", ignore_errors=True)
    capacity, free = ctx.host.disk_usage(d.path)
    entry.capacity, entry.free, entry.last_seen = capacity, free, now
    others = {n: c for n, c in load_caches(store, reg).items() if n != d.name}
    items = stale.classify(
        config=config,
        reg=reg,
        drive=d.name,
        contents=state.cache.contents,
        records=state.records,
        nas=nas,
        caches=others,
    )
    home_units = sorted(u for u, h in reg.homes.items() if h == d.name)
    files = {
        "contents": contents_text(state.cache.contents),
        "config.yaml": config_text,
        "restore.sh": restore_script(
            config=config,
            drive=d.name,
            home_units=home_units,
            items=items,
            own_files=own_files(ctx),
            now=now,
        ),
    }
    for name, text in files.items():
        path = d.path / BARUCH_DIR / name
        try:
            write_to_drive(path, text)
            if name == "restore.sh":
                path.chmod(0o755)
        except OSError as e:
            out(f"Warning: couldn't write {path}: {e.strerror or e}")
    state.cache.read = now
    state.save_cache()
    store.save(reg)
    _save_stale(ctx, d.name, items)
    return items


# init, config check


def cmd_init(ctx: Context, args) -> int:
    out = Out(ctx)
    store = store_of(ctx)
    if store.exists():
        raise Fail(f"A registry already exists at {store.path}.")
    lock(ctx, store)
    store.create(Registry())
    out(f"Created an empty registry at {store.path}.")
    path = config_path(ctx)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(STARTER)
        out(f"Wrote a starter configuration to {path}: edit it, then run")
        out("`baruch config check`.")
    else:
        out(f"Kept the existing configuration at {path}.")
    return 0


def cmd_config_check(ctx: Context, args) -> int:
    out = Out(ctx)
    config = get_config(ctx)
    out(f"{len(config.rules)} rules:")
    for rule in config.rules:
        out(f"  {describe(rule)}")
    problems = check(config)
    if problems:
        out("Problems:\n" + _bullets(problems))
        return 1
    out("The configuration is fine.")
    return 0


# run


def _validate_skip(skip: set[str], reg: Registry, nas) -> None:
    known = set(reg.homes) | {
        f"{rule}/{folder}" for rule, t in nas.items() if t for folder in t.units
    }
    for name in sorted(skip - known):
        similar = names.similar(name, sorted(known))
        hint = (
            f" Did you mean: {', '.join(map(names.display, similar))}?"
            if similar
            else ""
        )
        raise Fail(f"--skip: no unit named {names.display(name)}.{hint}")


def cmd_run(ctx: Context, args) -> int:
    out = Out(ctx)
    config = get_config(ctx)
    config_text = config_path(ctx).read_text(encoding="utf-8")
    store = store_of(ctx)
    if not args.dry_run:
        need_terminal(ctx, "run")
        lock(ctx, store)
    reg = get_registry(store)
    d = choose_drive(ctx, out, reg, args.drive)
    assert d.name  # a usable drive is registered
    rsync = ctx.host.rsync()
    try:
        check_rsync(rsync)
    except RsyncError as e:
        raise Fail(str(e)) from None
    now = ctx.host.now()
    recs, disk = read_drive(config, d)
    capacity, free = ctx.host.disk_usage(d.path)
    nas = scan_nas(config)
    skip = set(args.skip or [])
    _validate_skip(skip, reg, nas)
    caches = load_caches(store, reg, out)  # the docked drive's: as last seen
    plan = planner.make_plan(
        config=config,
        reg=reg,
        drive=d.name,
        capacity=capacity,
        free=free,
        nas=nas,
        disk=disk,
        records=recs,
        caches=caches,
        skip=skip,
        own_state=own_state(ctx, config),
        now=now,
    )
    text = render_plan(plan, d.path.name)
    if args.dry_run:
        out.raw(text)
        out.raw(render_summary(plan, None))
        out("Dry run: nothing changed.")
        return 0

    state = drive_state(ctx, config, store, d, recs, disk, now)
    log = new_log(ctx, d.name)
    log.write_text(text + render_summary(plan, None), encoding="utf-8")
    out.raw(text)
    if plan.needs_answer:
        out.raw(render_summary(plan, log))
        if not out.ask("Carry out this plan?"):
            state.save_cache()  # it only mirrors the drive
            _append(log, "answered n: nothing changed")
            out("Aborted, nothing changed.")
            return 0
        _append(log, "answered y")

    planner.apply(reg, plan)
    store.save(reg)
    state.save_cache()
    _save_scan_state(ctx, plan, nas)
    return _execute(
        ctx, out, config, config_text, reg, store, state, plan, nas, rsync, log
    )


def _execute(ctx, out, config, config_text, reg, store, state, plan, nas, rsync, log):
    d = state.docked
    code = 0
    left_out: dict[str, str] = dict(plan.skipped)
    for rule in plan.markers_missing:
        left_out[f"rule {rule}"] = "skipped: marker missing"
    for unit, msg in plan.unreadable:
        if reg.homes.get(unit) == d.name:
            left_out[unit] = f"can't be read on the NAS: {msg}"
    messages: list[str] = []

    # Units with nothing to copy or delete: just a new record, all at once.
    quiet = [op for op in plan.ops if not op.has_changes]
    if quiet:
        now = ctx.host.now()
        ensure_mounted(ctx.host, d)
        for op in quiet:
            result = ERRORS if op.errors else OK
            rec = Record(now, now, result)
            state.records[op.unit] = rec
            state.cache.records[op.unit] = rec
        state.save_records()
        state.save_cache()

    busy = [op for op in plan.ops if op.has_changes]
    current = None
    try:
        for op in busy:
            current = op
            ensure_mounted(ctx.host, d)
            _, free_now = ctx.host.disk_usage(d.path)
            floor = plan.reserve if op.kind == planner.FIRST else 0
            if free_now + op.delete_bytes - op.needs < floor:
                out(f"{names.display(op.unit)}: skipped, doesn't fit any more")
                left_out[op.unit] = "doesn't fit"
                current = None
                continue
            started = start_unit(state, op.unit, ctx.host.now()).started
            rule = config.rule(op.rule)
            result = sync_unit(op, d.path, rule.source, rsync)
            finish_unit(state, op.unit, started, result, ctx.host.now())
            status = "done" if result == OK else "synced with errors"
            out(f"{names.display(op.unit)}: {status}")
            for m in op.messages + op.errors:
                out(f"  {names.display(m)}")
            messages.extend(f"{op.unit}: {m}" for m in op.messages + op.errors)
            if any("(on the drive)" in e for e in op.errors):
                left_out[op.unit] = "a folder on the drive can't be read"
            current = None
    except KeyboardInterrupt:
        out("Interrupted: finished units stay recorded; the next run continues.")
        code = 130
    except DriveGone as e:
        out(str(e))
        code = 1
    except (DriveError, RsyncError, OSError) as e:
        name = names.display(current.unit) + ": " if current else ""
        out(f"{name}{e}")
        out("The run stopped for this drive.")
        code = 1
    if current is not None:
        left_out[current.unit] = "interrupted"
        idx = busy.index(current)
        for op in busy[idx + 1 :]:
            left_out.setdefault(op.unit, "not reached (the run stopped)")

    now = ctx.host.now()
    entry = reg.drives[d.name]
    entry.left_out = left_out
    if code == 0 and not left_out and not plan.drive_errors:
        entry.last_complete_run = now
    items = end_writes(ctx, out, config, config_text, reg, store, state, nas, now)
    for line in reserve_lines(plan):
        out(line)
    if items is not None:
        for line in stale_lines(items, d.name):
            out(names.display(line))
    if left_out:
        out("Not complete:")
        for u, why in sorted(left_out.items()):
            out(f"  {names.display(u)}: {why}")
    if plan.drive_errors and code == 0:
        out("Some folders on the drive can't be read (see above): exit code 1.")
        code = 1
    _append(log, "\n".join(messages) + f"\nfinished with exit code {code}")
    if items is not None:
        used = entry.capacity - entry.free
        out(f"{d.name}: {human(used)} used, {human(entry.free)} free.")
        out("safe to eject in DSM")
    return code


# status


def unit_states(reg, config, caches, scan_state) -> dict[str, str]:
    """Each unit's state from the registry, the caches and the last scan."""
    gone = set(scan_state.get("gone", []))
    states = {}
    for unit, home in reg.homes.items():
        if unit in gone:
            states[unit] = "gone"
            continue
        c = caches.get(home)
        rec = c.records.get(unit) if c else None
        if c is None or rec is None or not c.has_data(unit):
            states[unit] = "copy pending"
        elif rec.result == IN_PROGRESS:
            states[unit] = "incomplete"
        elif rec.result == ERRORS:
            states[unit] = "in place, synced with errors"
        else:
            states[unit] = "in place"
    for unit, _ in scan_state.get("unplaced", []):
        states[unit] = "unplaced"
    return states


def cmd_status(ctx: Context, args) -> int:
    out = Out(ctx)
    config = get_config(ctx)
    store = store_of(ctx)
    reg = get_registry(store)
    caches = load_caches(store, reg, out)
    scan_state = _read_scan_state(ctx)
    now = ctx.host.now()
    when = from_text(scan_state.get("time"))
    out(
        f"Last scan of the NAS: {show(when)}" + (" (from the last run)" if when else "")
    )
    total_room = 0
    for name in sorted(reg.drives):
        e = reg.drives[name]
        conf = config.drives.get(name)
        reserve = conf.reserve(e.capacity) if conf else 0
        c = caches.get(name)
        synced = [
            r.synced
            for u, r in (c.records.items() if c else [])
            if reg.homes.get(u) == name and r.synced
        ]
        oldest = min(synced) if synced else None
        used_reserve = max(0, reserve - e.free) if conf else 0
        if conf:
            total_room += max(0, e.free - reserve)
        out("")
        out(f"{name}{'' if conf else ' (not in the configuration)'}")
        out(
            f"  last complete run {show(e.last_complete_run)}, "
            f"last seen {show(e.last_seen)}"
        )
        out(f"  oldest unit sync {show(oldest)}, last verify {show(e.last_verify)}")
        out(
            f"  {human(e.capacity)} capacity, {human(e.free)} free, reserve "
            f"{human(reserve)}"
            + (f" ({human(used_reserve)} of it used)" if used_reserve else "")
        )
        if e.left_out:
            out("  the last run wasn't complete:")
            for u, why in sorted(e.left_out.items()):
                out(f"    {names.display(u)}: {why}")
        if conf and e.last_seen and now - e.last_seen > config.settings.stale_after:
            out(f"  not docked for longer than {config.settings.stale_after.days} days")
        if e.read_errors:
            out(
                f"  its last verify found {e.read_errors} read errors: "
                "consider replacing it"
            )
        if (
            not conf
            and c
            and any(
                u.entries for t in c.contents.folders.values() for u in t.units.values()
            )
        ):
            out("  still holds data")
    out("")
    out(f"Free space above the reserves: {human(total_room)}")

    states = unit_states(reg, config, caches, scan_state)
    attention = {u: s for u, s in states.items() if s != "in place"}
    if attention:
        out("")
        out("Units that need attention:")
        for u, s in sorted(attention.items()):
            home = reg.homes.get(u)
            where = f" (home {home})" if home and s == "copy pending" else ""
            out(f"  {names.display(u)}: {s}{where}")
    nowhere = [
        u
        for u, s in states.items()
        if s != "gone" and not any(c.complete(u) for c in caches.values())
    ]
    if nowhere:
        out("")
        out(
            f"{count(len(nowhere), 'unit')} {'has' if len(nowhere) == 1 else 'have'} "
            "no backup (no complete copy on any drive)."
        )
    for drive, items in sorted(_read_stale(ctx).items()):
        prune = [i for i in items if i["action"] == stale.PRUNE]
        hold = [i for i in items if i["action"] == stale.HOLD]
        unexplained = [i for i in items if i["action"] == stale.UNEXPLAINED]
        if not items:
            continue
        out("")
        out(
            f"{drive} (as of its last run or prune): {len(prune)} stale units, "
            f"{human(sum(i['size'] for i in prune))} can be pruned; {len(hold)} held"
        )
        for i in hold:
            out(f"  held: {names.display(i['path'])}: {i['reason']}")
        for i in unexplained:
            out(
                f"  unexplained: {names.display(i['path'])}: {i['reason']} "
                f"(dock it and run `baruch prune {drive}` for the command)"
            )
    return 0


# find


def cmd_find(ctx: Context, args) -> int:
    out = Out(ctx)
    store = store_of(ctx)
    reg = get_registry(store)
    caches = load_caches(store, reg, out)
    pattern = args.pattern
    hits: dict[str, list[tuple[str, str, Record | None, int]]] = {}
    for drive, c in sorted(caches.items()):
        for top, tree in c.contents.folders.items():
            for folder, u in tree.units.items():
                unit = f"{top}/{folder}"
                for rel, e in u.entries.items():
                    if e.kind != FILE:
                        continue
                    path = f"{top}/{rel}"
                    if pattern not in path:
                        continue
                    role = "home" if reg.homes.get(unit) == drive else "stale"
                    hits.setdefault(path, []).append(
                        (drive, role, c.records.get(unit), e.size)
                    )
    if not hits:
        out(f"No file matching {pattern!r}: it's on no drive.")
        return 0
    for path in sorted(hits):
        size = hits[path][0][3]
        out(f"{names.display(path)}  {human(size)}")
        for drive, role, rec, _ in hits[path]:
            synced = show_date(rec.synced) if rec and rec.synced else "no record"
            out(f"  {drive:8} {role:6} synced {synced}")
    return 0


# move set


def cmd_move_set(ctx: Context, args) -> int:
    out = Out(ctx)
    config = get_config(ctx)
    store = store_of(ctx)
    lock(ctx, store)
    reg = get_registry(store)
    scan_state = _read_scan_state(ctx)
    known = set(reg.homes) | set(scan_state.get("units", {}))
    unit = args.unit
    if unit not in known:
        similar = names.similar(unit, sorted(known))
        if not similar:
            raise Fail(f"No unit named {names.display(unit)}.")
        out(f"No unit named exactly {names.display(unit)}. Similar:")
        for i, s in enumerate(similar, 1):
            out(f"  {i}. {names.display(s)}")
        answer = out.read("Which one? (number, or Enter to cancel) ")
        if not answer.isdigit() or not 1 <= int(answer) <= len(similar):
            out("Nothing changed.")
            return 0
        unit = similar[int(answer) - 1]
    rule = config.find_rule(rule_of(unit))
    if rule is None:
        raise Fail(f"Rule {rule_of(unit)} isn't in the config.")
    if unit in scan_state.get("gone", []):
        raise Fail(
            f"{names.display(unit)} is gone on the NAS: there's nothing to move."
        )
    target = args.to
    if target not in reg.drives:
        raise Fail(f"{target} isn't registered.")
    if target not in rule.drives:
        raise Fail(
            f"{target} isn't in rule {rule.name}'s list ({', '.join(rule.drives)}): "
            "add it there first."
        )
    old = reg.homes.get(unit)
    if old == target:
        raise Fail(f"{names.display(unit)}'s home is already {target}.")
    size = scan_state.get("units", {}).get(unit, [0, 0])[0]
    e = reg.drives[target]
    conf = config.drives.get(target)
    if conf and e.free - conf.reserve(e.capacity) < size:
        out(
            f"Warning: {names.display(unit)} ({human(size)}) may not fit on {target} "
            f"({human(e.free)} free at its last run)."
        )
    reg.homes[unit] = target
    store.save(reg)
    out(f"{names.display(unit)}: home {old or 'none'} → {target}.")
    if old:
        out(
            f"Its data on {old} stays and becomes stale data; `baruch prune {old}` "
            f"removes it once {target} has a newer copy synced without errors."
        )
    return 0


# drive register, forget


def cmd_register(ctx: Context, args) -> int:
    out = Out(ctx)
    config = get_config(ctx)
    store = store_of(ctx)
    lock(ctx, store)
    reg = get_registry(store)
    name = args.name
    found = [d for d in docked(ctx.host, reg) if not d.usable]
    registered = [d for d in docked(ctx.host, reg) if d.usable]
    if not found:
        if registered:
            names_ = ", ".join(d.name or "" for d in registered)
            raise Fail(f"The docked disks are registered already ({names_}).")
        raise Fail("No disk docked.")
    if len(found) > 1:
        raise Fail(
            "Several unregistered disks are docked: dock only the one to register."
        )
    d = found[0]
    if ctx.host.fs_type(d.path) != "ext4":
        raise Fail(
            f"{d.path.name} isn't ext4 ({ctx.host.fs_type(d.path)}): format it first."
        )
    if name not in config.drives:
        out(
            f"Warning: {name} is not in the configuration's drives yet; it takes "
            "no units until it is."
        )
    label = d.label
    takeover = (  # --force: overwrite the label instead (asked below)
        not args.force
        and label is not None
        and d.status == "unknown"
        and label.name == name
        and name not in reg.drives
        and label.disk_id not in reg.forgotten
    )
    if name in reg.drives:
        raise Fail(
            f"{name} is already registered to another disk (ID "
            f"{reg.drives[name].disk_id}). A new disk gets a new name, or "
            f"`baruch drive forget {name}` first."
        )
    now = ctx.host.now()
    if takeover:
        assert label is not None
        try:
            recs = records.read(d.path)
        except RecordsError as e:
            raise Fail(
                f"{e}: can't take this disk over as it is. To give it a new "
                "label and start its records afresh (its data then counts as "
                f"unrecorded): `baruch drive register {name} --force`."
            ) from None
        newest = max((r.synced for r in recs.values() if r.synced), default=None)
        out(
            f"This disk carries the label {name}, disk ID {label.disk_id}, which "
            "isn't in the registry."
        )
        out(f"It holds {len(recs)} unit records, newest sync {show(newest)}.")
        need_terminal(ctx, "drive register")
        if not out.ask(f"Take it over as {name}, keeping its label and records?"):
            out("Nothing changed.")
            return 0
        disk_id = label.disk_id
    else:
        if label is not None or d.status == "bad label":
            if not args.force:
                raise Fail(
                    f"{d.path.name} has a label already ({d.reason()}). To "
                    "overwrite it and start its records afresh: "
                    f"`baruch drive register {name} --force`."
                )
            need_terminal(ctx, "drive register --force")
            shown = f"{label.name}, disk ID {label.disk_id}" if label else "unreadable"
            out(f"Existing label: {shown}.")
            if not out.ask(f"Overwrite it with a new label {name}?"):
                out("Nothing changed.")
                return 0
        recs = {}
        for old in ("restore.sh", "contents", "config.yaml"):
            (d.path / BARUCH_DIR / old).unlink(missing_ok=True)
        disk_id = write_label(d.path, name).disk_id
        records.write(d.path, {})
    scan = scan_drive(d.path, config.settings.exclude)
    capacity, free = ctx.host.disk_usage(d.path)
    reg.drives[name] = Drive(disk_id, capacity, free, now)
    cache.save(store.cache_dir, name, DriveCache(disk_id, now, recs, scan))
    store.save(reg)
    data = sum(u.size for t in scan.folders.values() for u in t.units.values())
    out(f"Registered {name} ({human(capacity)}, {human(free)} free).")
    if data:
        out(f"It holds {human(data)} of data; the next run decides what it belongs to.")
    return 0


def cmd_forget(ctx: Context, args) -> int:
    out = Out(ctx)
    store = store_of(ctx)
    need_terminal(ctx, "drive forget")
    lock(ctx, store)
    reg = get_registry(store)
    name = args.name
    if name not in reg.drives:
        raise Fail(f"{name} isn't registered.")
    caches = load_caches(store, reg)
    mine = caches.get(name)
    losing = []
    if mine is not None:
        for unit in mine.records:
            if not mine.complete(unit):
                continue
            if not any(c.complete(unit) for n, c in caches.items() if n != name):
                losing.append(unit)
    if losing:
        out(
            f"After this, {count(len(losing), 'unit')} "
            f"{'has' if len(losing) == 1 else 'have'} no backup (no complete copy):"
        )
        for u in sorted(losing)[:50]:
            out(f"  {names.display(u)}")
        if len(losing) > 50:
            out(f"  … {len(losing) - 50} more")
    else:
        out("No unit would be left without a complete copy.")
    if not out.ask(
        f"Forget {name}? Its data no longer counts, and its label becomes invalid."
    ):
        out("Nothing changed.")
        return 0
    reg.forgotten.add(reg.drives[name].disk_id)
    del reg.drives[name]
    store.save(reg)
    cache.remove(store.cache_dir, name)
    out(f"Forgot {name}. Units whose home it is stay copy pending until a new disk")
    out(f"is registered as {name}, or until you remove it from the config.")
    return 0


# prune


def cmd_prune(ctx: Context, args) -> int:
    out = Out(ctx)
    config = get_config(ctx)
    config_text = config_path(ctx).read_text(encoding="utf-8")
    store = store_of(ctx)
    need_terminal(ctx, "prune")
    lock(ctx, store)
    reg = get_registry(store)
    d = choose_drive(ctx, out, reg, args.drive)
    assert d.name  # a usable drive is registered
    now = ctx.host.now()
    recs, disk = read_drive(config, d)
    nas = scan_nas(config)
    caches = {n: c for n, c in load_caches(store, reg, out).items() if n != d.name}
    items = stale.classify(
        config=config,
        reg=reg,
        drive=d.name,
        contents=disk,
        records=recs,
        nas=nas,
        caches=caches,
    )
    state = drive_state(ctx, config, store, d, recs, disk, now)
    state.save_cache()
    prune = [i for i in items if i.action == stale.PRUNE]
    hold = [i for i in items if i.action == stale.HOLD]
    unexplained = [i for i in items if i.action == stale.UNEXPLAINED]
    log = new_log(ctx, f"prune-{d.name}")
    lines = [f"Prune plan for {d.name} ({d.path.name})", ""]
    for i in prune:
        lines.append(
            f"  - {names.display(i.path)}: {i.reason}: {count(i.files, 'file')}, "
            f"{human(i.size)}; {count(i.only_files, 'file')} only here"
        )
        lines.append(f"      ({_where_else(i)})")
    for i in hold:
        lines.append(f"  kept: {names.display(i.path)}: {i.reason}")
    if unexplained:
        lines.append("Unexplained data, never deleted by Baruch. To delete by hand:")
        for i in unexplained:
            lines.append(f"  {names.display(i.path)}: {i.reason}, {human(i.size)}")
            lines.append(
                f"    rm -rf -- {names.display(shlex.quote(str(d.path / i.path)))}"
            )
    text = "\n".join(lines) + "\n"
    log.write_text(text, encoding="utf-8")
    out.raw(text)
    if not prune:
        out("Nothing to prune.")
        end_writes(ctx, out, config, config_text, reg, store, state, nas, now)
        return 0
    only_files = sum(i.only_files for i in prune)
    only_bytes = sum(i.only_bytes for i in prune)
    out("")
    out(
        f"Deletions on {d.name} ({count(len(prune), 'unit')}, "
        f"{human(sum(i.size for i in prune))}):"
    )
    for i in prune:
        out(f"  {names.display(i.path)}: {count(i.files, 'file')}")
    if only_files:
        out(
            f"WARNING: {count(len(prune), 'unit')}: {count(only_files, 'file')}, "
            f"{human(only_bytes)} "
            "exist on no other drive and not on the NAS"
        )
    if not out.ask("Delete them?"):
        out("Nothing changed.")
        return 0
    deleted = 0
    for i in prune:
        assert i.unit  # only stale units are pruned
        ensure_mounted(ctx.host, d)
        rule = config.rule(rule_of(i.unit))
        folder = folder_of(i.unit)
        if i.kind == stale.GONE and os.path.lexists(rule.source / folder):
            out(f"{names.display(i.unit)}: back on the NAS, kept")
            continue
        clear_unit(state, i.unit)
        remove_unit(d.path / rule.name, folder)
        deleted += 1
        out(f"{names.display(i.path)}: deleted")
    # Gone units whose data no drive holds any more lose their entry.
    all_caches = dict(caches) | {d.name: state.cache}
    for i in prune:
        if (
            i.unit
            and i.kind == stale.GONE
            and not any(c.has_data(i.unit) for c in all_caches.values())
        ):
            reg.homes.pop(i.unit, None)
    recs_now, disk_now = (
        records.read(d.path),
        scan_drive(d.path, config.settings.exclude),
    )
    state.records, state.cache.contents = recs_now, disk_now
    state.cache.records = dict(recs_now)
    end_writes(ctx, out, config, config_text, reg, store, state, nas, ctx.host.now())
    _append(log, f"deleted {count(deleted, 'unit')}")
    out(f"Deleted {count(deleted, 'unit')}.")
    return 0


def _where_else(i: stale.Item) -> str:
    """Where Baruch believes the other copies are, and how old that is, so a
    drive that's broken or changed since stands out."""
    places = [
        f"{d}, last seen {show_date(seen)}" for d, seen in sorted(i.elsewhere.items())
    ]
    if i.on_nas:
        places.insert(0, "the NAS")
    if not places:
        return "on no other drive"
    return "also on " + "; ".join(places)


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
    config_text = config_path(ctx).read_text(encoding="utf-8")
    store = store_of(ctx)
    lock(ctx, store)
    reg = get_registry(store)
    d = choose_drive(ctx, out, reg, args.drive)
    assert d.name  # a usable drive is registered
    now = ctx.host.now()
    recs, disk = read_drive(config, d)
    state = drive_state(ctx, config, store, d, recs, disk, now)
    nas = scan_nas(config)
    read_errors: dict[str, list[str]] = {}  # unit -> rels
    changed: list[str] = []
    unclear: list[tuple[Path, Path]] = []
    for unit in sorted(u for u, h in reg.homes.items() if h == d.name):
        rec = recs.get(unit)
        rule = config.find_rule(rule_of(unit))
        if rule is None or rec is None or not rec.complete:
            continue
        scan = nas.get(rule.name)
        if scan is None:
            continue
        nu = scan.units.get(folder_of(unit))
        if nu is None:
            continue
        ensure_mounted(ctx.host, d)
        for rel, e in nu.entries.items():
            if e.kind != FILE:
                continue
            drive_file = d.path / rule.name / rel
            nas_file = rule.source / rel
            try:
                st = os.lstat(drive_file)
            except (FileNotFoundError, NotADirectoryError):
                changed.append(f"{rule.name}/{rel}")
                continue
            except OSError:
                read_errors.setdefault(unit, []).append(rel)
                continue
            if st.st_size != e.size or int(st.st_mtime) != e.mtime:
                changed.append(f"{rule.name}/{rel}")
                continue
            try:
                drive_hash = _hash(drive_file)
            except OSError:
                read_errors.setdefault(unit, []).append(rel)
                continue
            try:
                nas_hash = _hash(nas_file)
            except OSError:
                continue
            if drive_hash != nas_hash:
                unclear.append((nas_file, drive_file))
    if changed:
        out(
            f"{count(len(changed), 'file')} changed on the NAS since the last sync "
            "(normal; "
            "the next run takes care of them):"
        )
        for rel in changed:
            out(f"  {names.display(rel)}")
    if unclear:
        out(
            f"{count(len(unclear), 'file')} "
            f"{'differs' if len(unclear) == 1 else 'differ'} "
            "with the same size and time; it's unknown "
            "which side is right. Decide each one:"
        )
        for nas_file, drive_file in unclear:
            out(f"  {names.display(str(nas_file))}")
            nas_q = names.display(shlex.quote(str(nas_file)))
            drive_q = names.display(shlex.quote(str(drive_file)))
            out(f"    NAS → drive: rsync -a {nas_q} {drive_q}")
            out(f"    drive → NAS: rsync -a {drive_q} {nas_q}")
    n = sum(len(v) for v in read_errors.values())
    if read_errors:
        out(f"{count(n, 'file')} can't be read on {d.name}:")
        for unit, rels in sorted(read_errors.items()):
            for rel in rels:
                out(f"  {names.display(rule_of(unit) + '/' + rel)}")
        out("Read errors are a sign the drive is failing; consider replacing it.")
        need_terminal(ctx, "verify")
        if out.ask(f"copy these {n} unreadable files again from the NAS?"):
            rsync = ctx.host.rsync()
            for unit, rels in sorted(read_errors.items()):
                ensure_mounted(ctx.host, d)
                old = state.records[unit]
                start_unit(state, unit, ctx.host.now())
                rule = config.rule(rule_of(unit))
                code, err = run_rsync(
                    rsync,
                    rule.source,
                    d.path / rule.name,
                    rels,
                    d.path / BARUCH_DIR / "partial",
                )
                if code != 0:
                    out(err)
                # A re-copy isn't a sync: `synced` and the result stay as they were.
                state.records[unit] = Record(
                    state.records[unit].started, old.synced, old.result
                )
                state.save_records()
                state.cache.set_unit(unit, state.records[unit], state.rescan(unit))
                state.save_cache()
            out("Copied again.")
    if not (changed or unclear or read_errors):
        out(f"All files of {d.name}'s home units match the NAS.")
    entry = reg.drives[d.name]
    entry.last_verify = ctx.host.now()
    entry.read_errors = n
    end_writes(ctx, out, config, config_text, reg, store, state, nas, ctx.host.now())
    return 0
