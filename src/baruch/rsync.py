"""rsync: the version check, a copy with an exact file list, and what its
exit code and messages mean for a unit."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from baruch.records import ERRORS, OK

RSYNC_PARTIAL = 23
RSYNC_VANISHED = 24
MIN_RSYNC = (3, 2)


class RsyncError(Exception):
    pass


class DriveGone(Exception):
    pass


class DriveError(Exception):
    """Read or write errors on the drive: the run stops for this drive."""


class DriveFull(DriveError):
    pass


RSYNC_INTERRUPTED = 20


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


def run_rsync(
    rsync: str, source: Path, dest: Path, rels: list[str], partial: Path
) -> tuple[int, str]:
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


def classify(code: int, err: str) -> UnitResult:
    """What rsync's exit code and messages mean for the unit.

    Errors on the receiving side (the drive) stop the run. Files that are gone
    on the NAS by the time rsync gets to them count as vanished. Anything else
    reported by the sending side means files on the NAS couldn't be read."""
    lines = [line for line in err.splitlines() if line.strip()]
    if code == 0:
        return UnitResult(OK)
    if code == RSYNC_INTERRUPTED:
        raise KeyboardInterrupt
    full = [line for line in lines if "No space left" in line]
    if full:
        raise DriveFull("\n".join(full))
    messages = [line for line in lines if line.startswith("rsync:")]
    drive = [
        line
        for line in messages
        if "[receiver]" in line or "[generator]" in line or "Read-only" in line
    ]
    if drive:
        raise DriveError("errors writing to the drive:\n" + "\n".join(drive))
    if code not in (RSYNC_PARTIAL, RSYNC_VANISHED):
        raise RsyncError(f"rsync failed (exit {code}):\n" + "\n".join(lines))
    vanished = [line for line in lines if line.startswith("file has vanished")]
    vanished += [line for line in messages if "No such file or directory" in line]
    unreadable = [line for line in messages if line not in vanished]
    if unreadable:
        return UnitResult(ERRORS, unreadable)
    return UnitResult(OK, ["files vanished during copying:", *vanished])
