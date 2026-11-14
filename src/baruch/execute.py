"""Carrying out a plan: deletions by Baruch, copies by rsync with an exact
file list, the mount check before each unit, and the registry after each."""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from baruch.drives import BARUCH_DIR, Docked, still_there
from baruch.planner import CLEANUP, GONE, UnitOp
from baruch.registry import ERRORS, INCOMPLETE, OK, Copy

RSYNC_PARTIAL = 23
RSYNC_VANISHED = 24
MIN_RSYNC = (3, 2)


class RsyncError(Exception):
    pass


class DriveGone(Exception):
    pass


class DriveFull(Exception):
    pass


def check_rsync(path: str) -> None:
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, check=False
        ).stdout
    except OSError as e:
        raise RsyncError(f"can't run rsync at {path}: {e}") from None
    if "openrsync" in out:
        raise RsyncError(
            f"{path} is openrsync; install rsync with `brew install rsync`"
        )
    m = re.search(r"version\s+(\d+)\.(\d+)", out)
    if not m or (int(m.group(1)), int(m.group(2))) < MIN_RSYNC:
        raise RsyncError(
            f"{path} is older than rsync 3.2; install it with `brew install rsync`"
        )


@dataclass
class UnitResult:
    result: str
    messages: list[str] = field(default_factory=list)


def delete_entries(root: Path, rels: list[str]) -> None:
    """Files and links first, then folders deepest first. Folders that still
    hold something Baruch keeps (e.g. under a scan error) stay."""
    dirs = []
    for rel in rels:
        p = root / rel
        if p.is_dir() and not p.is_symlink():
            dirs.append(p)
        elif os.path.lexists(p):
            p.unlink()
    for p in sorted(dirs, key=lambda d: len(d.parts), reverse=True):
        with contextlib.suppress(FileNotFoundError, OSError):
            p.rmdir()  # fails if it still holds something Baruch keeps


def run_rsync(rsync: str, source: Path, dest: Path, rels: list[str], partial: Path):
    dest.mkdir(parents=True, exist_ok=True)
    partial.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", delete=False, suffix=".list") as f:
        for rel in rels:
            f.write(os.fsencode(rel) + b"\0")
        listing = f.name
    try:
        proc = subprocess.Popen(
            [
                rsync,
                "-a",
                "-H",
                "--ignore-times",
                f"--partial-dir={partial}",
                f"--files-from={listing}",
                "--from0",
                f"{source}/",
                f"{dest}/",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            _, err = proc.communicate()
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait()
            raise
        return proc.returncode, os.fsdecode(err)
    finally:
        os.unlink(listing)


def sync_unit(op: UnitOp, d: Docked, source: Path, rsync: str) -> UnitResult:
    dest = d.path / op.rule
    delete_entries(dest, op.delete)
    if op.kind in (CLEANUP, GONE):
        _prune_empty(dest)
        return UnitResult(OK)
    transfer = op.transfer
    if not transfer:
        return UnitResult(OK)
    code, err = run_rsync(
        rsync, source, dest, transfer, d.path / BARUCH_DIR / "partial"
    )
    lines = [line for line in err.splitlines() if line.strip()]
    if code == 0:
        return UnitResult(OK)
    if code == RSYNC_VANISHED:
        return UnitResult(OK, ["files vanished during copying:", *lines])
    if code == RSYNC_PARTIAL and not _drive_trouble(lines):
        return UnitResult(ERRORS, lines)
    if any("No space left" in line for line in lines):
        raise DriveFull("\n".join(lines))
    raise RsyncError(f"rsync failed (exit {code}):\n" + "\n".join(lines))


def _drive_trouble(lines: list[str]) -> bool:
    return any(
        "No space left" in line or "Read-only file system" in line for line in lines
    )


def _prune_empty(dest: Path) -> None:
    """Remove the rule folder if a cleanup emptied it."""
    try:
        if dest.is_dir() and not any(dest.iterdir()):
            dest.rmdir()
    except OSError:
        pass


def record(reg, op: UnitOp, drive: str, result: str, now) -> None:
    unit = reg.units.get(op.unit)
    if unit is None:
        return
    if op.kind in (CLEANUP, GONE):
        unit.copies.pop(drive, None)
        return
    old = unit.copies.get(drive)
    if result in (OK, ERRORS):
        unit.copies[drive] = Copy(now, result, op.size)
    else:
        synced = old.synced if old else None
        size = old.size if old else op.size
        unit.copies[drive] = Copy(synced, result, size)


def mark_incomplete(reg, op: UnitOp, drive: str) -> None:
    record(reg, op, drive, INCOMPLETE, None)


def ensure_mounted(host, d: Docked) -> None:
    if not still_there(host, d):
        raise DriveGone(
            f"{d.name} is no longer mounted (or its label changed): stopping"
        )
