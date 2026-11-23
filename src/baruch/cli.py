"""Command line: `baruch run`, `baruch status`, …"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from baruch import commands, style
from baruch.commands import Fail
from baruch.context import Context
from baruch.style import Line


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="baruch",
        description="Rule-based backup of a NAS to several drives, docked one "
        "at a time.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="back up the docked drive (plan first, then y/n)")
    run.add_argument("--dry-run", action="store_true", help="show the plan and exit")
    run.add_argument("--drive", help="the drive to use if several are docked")
    run.add_argument(
        "--skip", action="append", metavar="UNIT", help="leave a unit out (repeatable)"
    )
    run.set_defaults(func=commands.cmd_run)

    status = sub.add_parser("status", help="what needs attention")
    status.set_defaults(func=commands.cmd_status)

    find = sub.add_parser("find", help="which drives hold matching files")
    find.add_argument("pattern")
    find.set_defaults(func=commands.cmd_find)

    move = sub.add_parser("move", help="move a unit to another drive")
    move_sub = move.add_subparsers(dest="move_command", required=True)
    move_set = move_sub.add_parser("set", help="give a unit a new home")
    move_set.add_argument("unit", help="e.g. 'tv/The Wire'")
    move_set.add_argument("--to", required=True, metavar="DRIVE")
    move_set.set_defaults(func=commands.cmd_move_set)

    drive = sub.add_parser("drive", help="register or forget drives")
    drive_sub = drive.add_subparsers(dest="drive_command", required=True)
    register = drive_sub.add_parser("register", help="label the docked disk")
    register.add_argument("name")
    register.add_argument(
        "--force", action="store_true", help="overwrite an unknown or invalid label"
    )
    register.set_defaults(func=commands.cmd_register)
    forget = drive_sub.add_parser("forget", help="the data on a drive no longer counts")
    forget.add_argument("name")
    forget.set_defaults(func=commands.cmd_forget)

    prune = sub.add_parser("prune", help="delete stale data on the docked drive")
    prune.add_argument("drive")
    prune.set_defaults(func=commands.cmd_prune)

    verify = sub.add_parser("verify", help="compare the docked drive with the NAS")
    verify.add_argument("--drive", help="the drive to use if several are docked")
    verify.set_defaults(func=commands.cmd_verify)

    config = sub.add_parser("config", help="check the configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    check = config_sub.add_parser("check", help="check the configuration")
    check.set_defaults(func=commands.cmd_config_check)

    init = sub.add_parser("init", help="create an empty registry")
    init.set_defaults(func=commands.cmd_init)

    return p


def main(argv: list[str], ctx: Context) -> int:
    try:
        args = parser().parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    try:
        return args.func(ctx, args)
    except Fail as e:
        first, _, rest = str(e).partition("\n")
        st = style.for_stream(ctx.stdout)
        ctx.stdout.write(st.paint(Line(first, style.FAIL)) + "\n")
        if rest:
            ctx.stdout.write(rest + "\n")
        return e.code
    finally:
        for store in ctx.locks:
            store.unlock()
        ctx.locks.clear()


def _home(var: str, default: str) -> Path:
    base = os.environ.get(var) or os.path.expanduser(default)
    return Path(base) / "baruch"


def entry() -> None:
    from baruch.host import RealHost

    ctx = Context(
        config_dir=_home("XDG_CONFIG_HOME", "~/.config"),
        state_dir=_home("XDG_STATE_HOME", "~/.local/state"),
        host=RealHost(),
        stdin=sys.stdin,
        stdout=sys.stdout,
        interactive=sys.stdin.isatty() and sys.stdout.isatty(),
    )
    sys.exit(main(sys.argv[1:], ctx))
