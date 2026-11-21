#!/usr/bin/env python3
"""An interactive sandbox for Baruch: a fake NAS and fake drives in a folder,
kept between commands, with faults to inject and a check for lost data after
every Baruch command.

    uv run tools/sandbox.py new
    uv run tools/sandbox.py rule add tv archive/tv BK-6A BK-6B
    uv run tools/sandbox.py nas show archive/tv "The Wire" --episodes 3
    uv run tools/sandbox.py nas mark archive/tv
    uv run tools/sandbox.py drive new BK-6A --capacity 100K
    uv run tools/sandbox.py baruch init
    uv run tools/sandbox.py dock BK-6A
    uv run tools/sandbox.py baruch drive register BK-6A
    uv run tools/sandbox.py baruch run --answers y
    uv run tools/sandbox.py fault crash 2          # the next Baruch command
    uv run tools/sandbox.py ls

The sandbox lives in $BARUCH_SANDBOX (default: .sandbox/ in the repo). Every
`baruch` command is checked afterwards:

- Baruch didn't change anything on the NAS;
- nothing was written into an empty mount point;
- no file version vanished from every drive and the NAS unless the command's
  output named it: listed as a deletion or overwrite in a run's plan, or
  counted as "files only here" in prune's plan.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from support import DEFAULT_SETTINGS, Env, FakeDrive  # noqa: E402

import baruch.execute as execute  # noqa: E402
from baruch.scan import FILES_UNIT  # noqa: E402
from baruch.sizes import parse_size  # noqa: E402

DIR = Path(os.environ.get("BARUCH_SANDBOX", ROOT / ".sandbox"))
STATE = "sandbox.json"


class Crash(BaseException):
    """A simulated kill -9: nothing after it runs."""


# State


def load() -> dict:
    try:
        return json.loads((DIR / STATE).read_text())
    except FileNotFoundError:
        sys.exit(f"No sandbox at {DIR}: create one with `sandbox.py new`.")


def save(state: dict) -> None:
    (DIR / STATE).write_text(json.dumps(state, indent=1))


def env_of(state: dict) -> Env:
    env = Env(DIR)
    env.host.clock = datetime.fromisoformat(state["now"])
    env.nas.versions = state["versions"]
    for name, info in state["drives"].items():
        drive = FakeDrive(env.drives.shelf / name, info["capacity"], info["fs"])
        env.host.drives[Path(os.path.realpath(drive.root))] = drive
    for slot in env.host.usb.iterdir():
        if slot.is_symlink():
            env.drives.slots[str(Path(os.readlink(slot)))] = slot
    return env


def keep(state: dict, env: Env) -> None:
    state["now"] = env.host.clock.isoformat()
    state["versions"] = env.nas.versions
    save(state)


def write_config(state: dict, env: Env) -> None:
    rules = [
        {"name": r["name"], "source": r["source"], "drives": r["drives"]}
        for r in state["rules"]
    ]
    settings = dict(DEFAULT_SETTINGS)
    settings["exclude"] = state["exclude"]
    env.config(rules=rules, drives=state["reserves"], **settings)


def drive_of(env: Env, name: str) -> FakeDrive:
    drive = env.host.drives.get(Path(os.path.realpath(env.drives.shelf / name)))
    if drive is None:
        sys.exit(f"No drive {name} on the shelf.")
    return drive


# Snapshots for the checks


def _hash(path: Path) -> str:
    if path.is_symlink():
        return "link:" + os.readlink(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _walk(root: Path, skip_top: tuple[str, ...] = ()) -> dict[str, str]:
    out = {}
    if not root.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d not in skip_top]
        for name in filenames + [
            d for d in dirnames if os.path.islink(os.path.join(dirpath, d))
        ]:
            p = Path(dirpath) / name
            rel = os.path.normpath(os.path.join(rel_dir, name))
            try:
                out[rel] = _hash(p)
            except OSError:
                out[rel] = "unreadable"
    return out


def nas_files(env: Env) -> dict[str, str]:
    return _walk(env.nas.root)


def versions(state: dict, env: Env) -> set[tuple[str, str, str]]:
    """Every (rule, path in the rule's folder, content) on the NAS and on all
    drives, docked or not."""
    out = set()
    nas = nas_files(env)
    for r in state["rules"]:
        prefix = r["source"].rstrip("/") + "/"
        for rel, h in nas.items():
            if rel.startswith(prefix) and h != "unreadable":
                out.add((r["name"], rel[len(prefix) :], h))
    for name in state["drives"]:
        root = env.drives.shelf / name
        for rel, h in _walk(root, skip_top=(".baruch", "lost+found")).items():
            if "/" in rel and h != "unreadable":
                rule, inner = rel.split("/", 1)
                out.add((rule, inner, h))
    return out


def _unit_of(inner: str) -> str:
    return inner.split("/", 1)[0] if "/" in inner else FILES_UNIT


def explained(rule: str, inner: str, output: str) -> bool:
    """Did the command's output name this loss before it happened?"""
    unit = f"{rule}/{_unit_of(inner)}"
    rest = inner.split("/", 1)[1] if "/" in inner else inner
    if unit not in output:
        return False
    # A run's plan: the file listed as deleted or overwritten, or the
    # excluded names / Baruch's own state summarised for its unit.
    for mark in ("- ", "~ "):
        if f"{mark}{rest}" in output:
            return True
    block = output.split(unit, 1)[1][:2000]
    if "excluded names" in block or "Baruch's own state" in block:
        return True
    # prune's plan: the unit with a non-zero "files only here".
    m = re.search(re.escape(unit) + r":[^\n]*?(\d+) files only here", output)
    return bool(m and int(m.group(1)) > 0)


def check(state, env, before, nas_before, output, command) -> list[str]:
    problems = []
    nas_after = nas_files(env)
    if command == "baruch" and nas_after != nas_before:
        changed = sorted(
            set(nas_before.items()) ^ set(nas_after.items()), key=lambda t: t[0]
        )
        problems.append(f"Baruch changed the NAS: {changed[:5]}")
    if command == "restore":
        deleted = sorted(set(nas_before) - set(nas_after))
        if deleted:
            problems.append(f"restore.sh deleted on the NAS: {deleted[:5]}")
    for slot in env.host.usb.iterdir():
        if not slot.is_symlink() and slot.is_dir() and any(slot.iterdir()):
            problems.append(f"something was written into the empty mount point {slot}")
    after = versions(state, env)
    for rule, inner, h in sorted(before - after):
        if not explained(rule, inner, output):
            problems.append(
                f"LOST without warning: {rule}/{inner} (content {h}) is on no drive "
                "and not on the NAS any more"
            )
    return problems


# Faults


def install_fault(env: Env, fault: dict | None, slot_of) -> None:
    if not fault:
        return
    kind, n = fault["kind"], fault["n"]
    real = execute.run_rsync
    calls = {"n": 0}

    def fake(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] != n:
            return real(*args, **kwargs)
        print(f"[sandbox] fault: {kind} at rsync call {n}")
        if kind == "crash":
            raise Crash()
        if kind == "interrupt":
            raise KeyboardInterrupt
        if kind == "full":
            return 11, "rsync: write failed: No space left on device (28)"
        if kind == "write-error":
            return 23, (
                'rsync: [receiver] mkstemp "/x/.f" failed: Permission denied (13)\n'
            )
        if kind == "unplug":
            slot = slot_of()
            if slot is not None:
                slot.unlink()
                slot.mkdir()  # the empty mount point left behind
            return 12, "rsync error: error in rsync protocol data stream (code 12)"
        if kind == "after-copy-crash":
            real(*args, **kwargs)
            raise Crash()
        raise SystemExit(f"unknown fault {kind}")

    execute.run_rsync = fake


# Commands


def cmd_new(args) -> None:
    if DIR.exists():
        if not args.force:
            sys.exit(f"{DIR} exists; use --force to start over.")
        for p in DIR.rglob("*"):
            with contextlib.suppress(OSError):
                if p.is_dir() and not p.is_symlink():
                    p.chmod(0o755)
        shutil.rmtree(DIR)
    DIR.mkdir(parents=True)
    state = {
        "now": "2026-11-14T10:00:00+00:00",
        "versions": {},
        "drives": {},
        "rules": [],
        "reserves": {},
        "exclude": list(DEFAULT_SETTINGS["exclude"]),
        "fault": None,
    }
    save(state)
    env = env_of(state)
    write_config(state, env)
    print(f"New sandbox at {DIR}")


def cmd_nas(args) -> None:
    state = load()
    env = env_of(state)
    nas = env.nas
    a = args.args
    op = args.op
    if op == "add":
        nas.add(a[0], size=parse_size(args.size))
    elif op == "show":
        nas.add_show(a[0], a[1], episodes=args.episodes, size=parse_size(args.size))
    elif op == "replace":
        size = parse_size(args.size) if args.size != "1000" else None
        nas.replace(a[0], size=size, keep_mtime=args.keep_mtime)
    elif op == "delete":
        nas.delete(a[0])
    elif op == "rename":
        nas.rename(a[0], a[1])
    elif op == "mkdir":
        nas.mkdir(a[0])
    elif op == "mark":
        nas.mark(a[0])
    elif op == "unmark":
        nas.remove_marker(a[0])
    elif op == "chmod":
        os.chmod(nas.path(a[0]), int(a[1], 8))
    elif op == "symlink":
        os.symlink(a[1], nas.path(a[0]))
    else:
        sys.exit(f"unknown nas operation {op}")
    keep(state, env)


def cmd_drive(args) -> None:
    state = load()
    env = env_of(state)
    if args.op == "new":
        name = args.name
        state["drives"][name] = {"capacity": parse_size(args.capacity), "fs": args.fs}
        env.drives.new(name, parse_size(args.capacity), args.fs)
        state["reserves"].setdefault(name, args.reserve)
        write_config(state, env)
    elif args.op == "rm":
        p = drive_of(env, args.name).path(args.path)
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
    elif args.op == "write":
        p = drive_of(env, args.name).path(args.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(args.text)
    elif args.op == "chmod":
        os.chmod(drive_of(env, args.name).path(args.path), int(args.mode, 8))
    elif args.op == "clone":
        src = env.drives.shelf / args.name
        shutil.copytree(src, env.drives.shelf / args.path, symlinks=True)
        info = dict(state["drives"][args.name])
        state["drives"][args.path] = info
    keep(state, env)


def cmd_dock(args) -> None:
    state = load()
    env = env_of(state)
    slot = env.drives.dock(drive_of(env, args.name))
    print(f"{args.name} docked at {slot}")
    keep(state, env)


def cmd_undock(args) -> None:
    state = load()
    env = env_of(state)
    names = list(state["drives"]) if args.name == "all" else [args.name]
    for name in names:
        drive = drive_of(env, name)
        if str(drive.root) in env.drives.slots:
            env.drives.undock(drive)
    for slot in env.host.usb.iterdir():  # left-over empty mount points
        if not slot.is_symlink() and slot.is_dir() and not any(slot.iterdir()):
            slot.rmdir()
    keep(state, env)


def cmd_clock(args) -> None:
    state = load()
    env = env_of(state)
    m = re.fullmatch(r"\+?(\d+)([dhm])", args.delta)
    if not m:
        sys.exit("clock: e.g. +40d, +3h, +10m")
    unit = {"d": "days", "h": "hours", "m": "minutes"}[m.group(2)]
    env.host.advance(**{unit: int(m.group(1))})
    keep(state, env)
    print(env.host.clock.isoformat())


def cmd_rule(args) -> None:
    state = load()
    env = env_of(state)
    if args.op == "add":
        state["rules"] = [r for r in state["rules"] if r["name"] != args.name]
        state["rules"].append(
            {"name": args.name, "source": args.source, "drives": args.drives}
        )
    elif args.op == "remove":
        state["rules"] = [r for r in state["rules"] if r["name"] != args.name]
    write_config(state, env)
    keep(state, env)


def cmd_exclude(args) -> None:
    state = load()
    env = env_of(state)
    if args.op == "add":
        state["exclude"].append(args.pattern)
    else:
        state["exclude"].remove(args.pattern)
    write_config(state, env)
    keep(state, env)


def cmd_fault(args) -> None:
    state = load()
    state["fault"] = None if args.kind == "none" else {"kind": args.kind, "n": args.n}
    save(state)


def cmd_baruch(args) -> None:
    state = load()
    env = env_of(state)
    before = versions(state, env)
    nas_before = nas_files(env)
    fault, state["fault"] = state.get("fault"), None

    def slot_of():
        return next(iter(env.drives.slots.values()), None)

    install_fault(env, fault, slot_of)
    answers = "\n".join(args.answers.split(",")) + "\n" if args.answers else ""
    try:
        result = env.run(*args.argv, answers=answers)
        output, code = result.out, result.code
    except Crash:
        output, code = "", None
        print("[sandbox] simulated crash: the process died here")
    print(output, end="")
    if code is not None:
        print(f"[exit {code}]")
    keep(state, env)
    report(check(state, env, before, nas_before, output, "baruch"))


def cmd_restore(args) -> None:
    state = load()
    env = env_of(state)
    drive = drive_of(env, args.name)
    before = versions(state, env)
    nas_before = nas_files(env)
    rsync_dir = os.path.dirname(env.host.rsync())
    path = rsync_dir + os.pathsep + os.environ.get("PATH", "")
    res = subprocess.run(
        ["sh", str(drive.path(".baruch/restore.sh")), *args.script_args],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": path},
        check=False,
    )
    print(res.stdout + res.stderr, end="")
    print(f"[exit {res.returncode}]")
    report(check(state, env, before, nas_before, res.stdout, "restore"))


def cmd_state(args) -> None:
    """Lose Baruch's state, or put back a saved version (an older state)."""
    state = load()
    env = env_of(state)
    st = env.state_dir
    if args.op == "lose":
        shutil.rmtree(st, ignore_errors=True)
        print("Baruch's state is gone (registry, cache, versions, logs).")
    elif args.op == "old":
        saved = sorted((st / "versions").iterdir())
        if not saved:
            sys.exit("no saved versions")
        pick = saved[-args.n]
        shutil.copy2(pick / "registry.yaml", st / "registry.yaml")
        for f in (st / "cache").glob("*.json"):
            f.unlink()
        for f in (pick / "cache").glob("*.json"):
            shutil.copy2(f, st / "cache" / f.name)
        print(f"Registry and cached records put back from {pick.name}.")


def cmd_ls(args) -> None:
    state = load()
    env = env_of(state)
    print(f"clock {env.host.clock.isoformat()}  fault {state.get('fault')}")
    rules = [f"{r['name']}={r['source']}→{r['drives']}" for r in state["rules"]]
    print("rules:", ", ".join(rules))
    print("NAS:")
    for rel, h in sorted(nas_files(env).items()):
        print(f"  {rel}  {h}")
    docked = {Path(k).name for k in env.drives.slots}
    for name in state["drives"]:
        drive = drive_of(env, name)
        used = drive.used()
        print(
            f"{name}{' (docked)' if name in docked else ''}: "
            f"{used} of {drive.capacity} bytes used"
        )
        for rel, h in sorted(_walk(drive.root, skip_top=(".baruch",)).items()):
            print(f"  {rel}  {h}")
        units = drive.path(".baruch/units.yaml")
        if units.exists() and args.records:
            for line in units.read_text().splitlines()[1:]:
                print(f"  record {line}")


def cmd_check(args) -> None:
    state = load()
    env = env_of(state)
    v = versions(state, env)
    print(f"{len(v)} file versions on the NAS and the drives.")


def report(problems: list[str]) -> None:
    if problems:
        print("\n[sandbox] CHECK FAILED:")
        for p in problems:
            print(f"  !! {p}")
    else:
        print("[sandbox] check: ok")


def main() -> None:
    p = argparse.ArgumentParser(prog="sandbox.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("new")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_new)
    s = sub.add_parser("nas", help="change the NAS (paths relative to /volume1)")
    s.add_argument(
        "op",
        choices=[
            "add",
            "show",
            "replace",
            "delete",
            "rename",
            "mkdir",
            "mark",
            "unmark",
            "chmod",
            "symlink",
        ],
    )
    s.add_argument("args", nargs="*")
    s.add_argument("--size", default="1000")
    s.add_argument("--episodes", type=int, default=3)
    s.add_argument("--keep-mtime", action="store_true")
    s.set_defaults(func=cmd_nas)
    s = sub.add_parser("drive", help="new disks, and changes by hand on a disk")
    s.add_argument("op", choices=["new", "rm", "write", "chmod", "clone"])
    s.add_argument("name")
    s.add_argument("path", nargs="?")
    s.add_argument("text", nargs="?", default="by hand")
    s.add_argument("--capacity", default="100K")
    s.add_argument("--reserve", default="10%")
    s.add_argument("--fs", default="ext4")
    s.add_argument("--mode", default="000")
    s.set_defaults(func=cmd_drive)
    s = sub.add_parser("dock")
    s.add_argument("name")
    s.set_defaults(func=cmd_dock)
    s = sub.add_parser("undock")
    s.add_argument("name")
    s.set_defaults(func=cmd_undock)
    s = sub.add_parser("clock")
    s.add_argument("delta")
    s.set_defaults(func=cmd_clock)
    s = sub.add_parser("rule", help="edit the configuration's rules")
    s.add_argument("op", choices=["add", "remove"])
    s.add_argument("name")
    s.add_argument("source", nargs="?")
    s.add_argument("drives", nargs="*")
    s.set_defaults(func=cmd_rule)
    s = sub.add_parser("exclude")
    s.add_argument("op", choices=["add", "remove"])
    s.add_argument("pattern")
    s.set_defaults(func=cmd_exclude)
    s = sub.add_parser("fault", help="a fault for the next Baruch command")
    s.add_argument(
        "kind",
        choices=[
            "crash",
            "interrupt",
            "full",
            "write-error",
            "unplug",
            "after-copy-crash",
            "none",
        ],
    )
    s.add_argument("n", type=int, nargs="?", default=1, help="at the n-th rsync call")
    s.set_defaults(func=cmd_fault)
    s = sub.add_parser("baruch", help="run a Baruch command, then check")
    s.add_argument("--answers", default="", help="comma-separated, e.g. y or n,y")
    s.add_argument("argv", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_baruch)
    s = sub.add_parser("restore", help="run a drive's restore.sh, then check")
    s.add_argument("name")
    s.add_argument("script_args", nargs="*")
    s.set_defaults(func=cmd_restore)
    s = sub.add_parser("state", help="lose Baruch's state, or go back to a version")
    s.add_argument("op", choices=["lose", "old"])
    s.add_argument("n", type=int, nargs="?", default=1)
    s.set_defaults(func=cmd_state)
    s = sub.add_parser("ls")
    s.add_argument("--records", action="store_true")
    s.set_defaults(func=cmd_ls)
    s = sub.add_parser("check")
    s.set_defaults(func=cmd_check)
    args = p.parse_args()
    if getattr(args, "argv", None) is not None:
        argv = [a for a in args.argv if a != "--"]
        if "--answers" in argv:  # also when written after the Baruch command
            i = argv.index("--answers")
            args.answers = argv[i + 1]
            del argv[i : i + 2]
        args.argv = argv
    args.func(args)


if __name__ == "__main__":
    main()
