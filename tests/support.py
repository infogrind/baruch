"""Test harness: a fake NAS, fake drives and a fake host, all in a temp folder.

Drives live on a "shelf" and are docked by symlinking them into the folder
that Baruch searches for USB drives. Free space is computed from the files on
the drive against a fake capacity, not from the real filesystem.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from baruch.cli import main
from baruch.context import Context

BASE_MTIME = 1_700_000_000
MARKER = ".backup-source"


def content(rel: str, version: int, size: int) -> bytes:
    """Deterministic content derived from path and version, never zero-filled."""
    block = hashlib.sha256(
        f"{rel}#{version}".encode("utf-8", "surrogateescape")
    ).digest()
    out = bytearray()
    while len(out) < size:
        out += block
        block = hashlib.sha256(block).digest()
    return bytes(out[:size])


def tree_size(root: Path) -> int:
    total = 0
    seen: set[tuple[int, int]] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames + dirnames:
            st = os.lstat(os.path.join(dirpath, name))
            if not os.path.isdir(os.path.join(dirpath, name)) or os.path.islink(
                os.path.join(dirpath, name)
            ):
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                total += st.st_size
    return total


class FakeNas:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.versions: dict[str, int] = {}

    def path(self, rel: str) -> Path:
        return self.root / rel

    def add(
        self, rel: str, size: int = 1000, version: int = 1, mtime: int | None = None
    ):
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content(rel, version, size))
        t = mtime if mtime is not None else BASE_MTIME + version * 1000
        os.utime(p, (t, t))
        self.versions[rel] = version

    def replace(self, rel: str, size: int | None = None, keep_mtime: bool = False):
        p = self.path(rel)
        st = p.stat()
        version = self.versions.get(rel, 1) + 1
        new_size = st.st_size if size is None else size
        p.write_bytes(content(rel, version, new_size))
        t = st.st_mtime if keep_mtime else BASE_MTIME + version * 1000
        os.utime(p, (t, t))
        self.versions[rel] = version

    def delete(self, rel: str):
        p = self.path(rel)
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()

    def rename(self, old: str, new: str):
        self.path(old).rename(self.path(new))

    def mkdir(self, rel: str):
        self.path(rel).mkdir(parents=True, exist_ok=True)

    def mark(self, rel: str):
        self.add(f"{rel}/{MARKER}", size=0)

    def remove_marker(self, rel: str):
        self.path(f"{rel}/{MARKER}").unlink()

    def add_show(self, source: str, name: str, episodes: int = 3, size: int = 3000):
        """A folder with `episodes` files totalling `size` bytes."""
        each = size // episodes
        for i in range(1, episodes + 1):
            self.add(f"{source}/{name}/S01E{i:02d}.mkv", size=each)

    def read(self, rel: str) -> bytes:
        return self.path(rel).read_bytes()


class FakeDrive:
    def __init__(self, root: Path, capacity: int, fs_type: str = "ext4"):
        self.root = root
        self.capacity = capacity
        self.fs_type = fs_type
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, rel: str) -> Path:
        return self.root / rel

    def read(self, rel: str) -> bytes:
        return self.path(rel).read_bytes()

    def exists(self, rel: str) -> bool:
        return os.path.lexists(self.path(rel))

    def files(self) -> set[str]:
        """All files and symlinks on the drive, outside Baruch's own folder."""
        out = set()
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel_dir = os.path.relpath(dirpath, self.root)
            if rel_dir == ".baruch" or rel_dir.startswith(".baruch" + os.sep):
                continue
            for name in filenames + [
                d for d in dirnames if os.path.islink(os.path.join(dirpath, d))
            ]:
                out.add(os.path.normpath(os.path.join(rel_dir, name)))
        return out

    def used(self) -> int:
        return tree_size(self.root)


class FakeHost:
    """The NAS-specific parts: drive discovery, mount check, free space, time."""

    def __init__(self, usb: Path):
        self.usb = usb
        self.usb.mkdir(parents=True, exist_ok=True)
        self.drives: dict[Path, FakeDrive] = {}
        self.clock = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)

    def drive_paths(self) -> list[Path]:
        return sorted(p for p in self.usb.iterdir() if p.is_dir())

    def _drive(self, path: Path) -> FakeDrive | None:
        try:
            real = Path(os.path.realpath(path))
        except OSError:
            return None
        return self.drives.get(real)

    def is_mount(self, path: Path) -> bool:
        return path.is_dir() and self._drive(path) is not None

    def disk_usage(self, path: Path) -> tuple[int, int]:
        drive = self._drive(path)
        assert drive is not None, path
        return drive.capacity, max(0, drive.capacity - drive.used())

    def fs_type(self, path: Path) -> str:
        drive = self._drive(path)
        return drive.fs_type if drive else "unknown"

    def now(self) -> datetime:
        self.clock += timedelta(minutes=1)
        return self.clock

    def advance(self, **kwargs):
        self.clock += timedelta(**kwargs)

    def rsync(self) -> str:
        for candidate in (
            "/opt/homebrew/opt/rsync/bin/rsync",
            "/home/linuxbrew/.linuxbrew/opt/rsync/bin/rsync",
        ):
            if os.path.exists(candidate):
                return candidate
        found = shutil.which("rsync")
        assert found, "rsync not found"
        return found


class Drives:
    def __init__(self, shelf: Path, host: FakeHost):
        self.shelf = shelf
        self.host = host
        self.shelf.mkdir(parents=True, exist_ok=True)
        self.slots: dict[str, Path] = {}

    def new(
        self, disk: str, capacity: int = 100_000, fs_type: str = "ext4"
    ) -> FakeDrive:
        drive = FakeDrive(self.shelf / disk, capacity, fs_type)
        self.host.drives[Path(os.path.realpath(drive.root))] = drive
        return drive

    def dock(self, drive: FakeDrive) -> Path:
        n = 1
        while (self.host.usb / f"usbshare{n}").exists():
            n += 1
        slot = self.host.usb / f"usbshare{n}"
        slot.symlink_to(drive.root, target_is_directory=True)
        self.slots[str(drive.root)] = slot
        return slot

    def undock(self, drive: FakeDrive):
        slot = self.slots.pop(str(drive.root))
        slot.unlink()

    def undock_all(self):
        for slot in list(self.slots.values()):
            slot.unlink()
        self.slots.clear()


# The lost-data check, applied after every Baruch command a test runs.


def _hash(path: Path) -> str:
    if path.is_symlink():
        return "link:" + os.readlink(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def walk(root: Path, skip: tuple[Path, ...] = ()) -> dict[str, str]:
    """Every file and symlink under `root`, with its content hash."""
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if here / d not in skip]
        for name in filenames + [d for d in dirnames if (here / d).is_symlink()]:
            p = here / name
            try:
                out[os.path.relpath(p, root)] = _hash(p)
            except OSError:
                out[os.path.relpath(p, root)] = "unreadable"
    return out


def explained(rule: str, inner: str, output: str) -> bool:
    """Did the command's output name this loss before it happened? Listed as
    deleted or overwritten in a run's plan (or summarised as excluded names or
    Baruch's own state), or counted in prune's "files only here"."""
    folder = inner.split("/", 1)[0] if "/" in inner else "(files)"
    rest = inner.split("/", 1)[1] if "/" in inner else inner
    unit = f"{rule}/{folder}"
    if unit not in output:
        return False
    if f"- {rest}" in output or f"~ {rest}" in output:
        return True
    after = output.split(unit, 1)[1][:3000]
    if "excluded names" in after or "Baruch's own state" in after:
        return True
    m = re.search(re.escape(unit) + r":[^\n]*?(\d+) files only here", output)
    return bool(m and int(m.group(1)) > 0)


def listed(rule: str, inner: str, output: str) -> bool:
    """A run's plan listed this file as deleted or overwritten (or summarised
    it as excluded names or Baruch's own state)."""
    folder = inner.split("/", 1)[0] if "/" in inner else "(files)"
    rest = inner.split("/", 1)[1] if "/" in inner else inner
    unit = f"{rule}/{folder}"
    if unit not in output:
        return False
    if f"- {rest}" in output or f"~ {rest}" in output:
        return True
    after = output.split(unit, 1)[1][:3000]
    return "excluded names" in after or "Baruch's own state" in after


class SilentLoss(AssertionError):
    pass


@dataclass
class Result:
    code: int
    out: str

    @property
    def lines(self) -> list[str]:
        return self.out.rstrip("\n").splitlines()


def rule(name: str, source: str, *drives: str) -> dict:
    return {"name": name, "source": source, "drives": list(drives)}


def pin(name: str, source: str, drive: str) -> dict:
    """A rule with one drive: everything goes there."""
    return rule(name, source, drive)


def spread(name: str, source: str, drives: list[str]) -> dict:
    """A rule with several drives, in order of preference."""
    return rule(name, source, *drives)


DEFAULT_SETTINGS = {
    "exclude": ["@eaDir", "#recycle", ".DS_Store"],
    "stale_after": "30d",
    "warn_changed_files": "10%",
    "warn_changed_bytes": "10%",
    "warn_unit_loss": "50%",
}


@dataclass
class Env:
    tmp: Path
    nas: FakeNas = field(init=False)
    host: FakeHost = field(init=False)
    drives: Drives = field(init=False)
    config_dir: Path = field(init=False)
    state_dir: Path = field(init=False)
    sources: dict[str, str] = field(default_factory=dict)  # rule -> NAS path
    check_loss: bool = True  # the lost-data check after every command

    def __post_init__(self):
        self.nas = FakeNas(self.tmp / "volume1")
        self.host = FakeHost(self.tmp / "usb")
        self.drives = Drives(self.tmp / "shelf", self.host)
        self.config_dir = self.tmp / "config"
        self.state_dir = self.tmp / "state"

    def src(self, rel: str) -> str:
        return str(self.nas.path(rel))

    def config(self, rules: list[dict], drives: dict[str, str], **settings):
        merged = dict(DEFAULT_SETTINGS)
        merged.update(settings)
        self.sources = {r["name"]: r["source"] for r in rules}
        doc = {
            "settings": merged,
            "drives": {name: {"reserve": reserve} for name, reserve in drives.items()},
            "rules": [
                {**rule, "source": str(self.nas.path(rule["source"]))} for rule in rules
            ],
        }
        self.config_dir.mkdir(parents=True, exist_ok=True)
        (self.config_dir / "config.yaml").write_text(
            yaml.safe_dump(doc, sort_keys=False)
        )

    def run(self, *argv: str, answers: str = "", interactive: bool = True) -> Result:
        stdout = io.StringIO()
        ctx = Context(
            config_dir=self.config_dir,
            state_dir=self.state_dir,
            host=self.host,
            stdin=io.StringIO(answers),
            stdout=stdout,
            interactive=interactive,
        )
        before = self.snapshot() if self.check_loss else None
        try:
            code = main(list(argv), ctx)
        finally:
            if before is not None:
                problems = self.loss_problems(before, stdout.getvalue())
                if problems:
                    raise SilentLoss(
                        f"baruch {' '.join(argv)}:\n  "
                        + "\n  ".join(problems)
                        + "\n--- output ---\n"
                        + stdout.getvalue()
                    )
        return Result(code, stdout.getvalue())

    def _own(self) -> tuple[Path, ...]:
        return (self.config_dir, self.state_dir)

    def snapshot(self) -> tuple[set, dict, set]:
        """(rule, path in the rule's folder, content) on the NAS and on all
        drives, docked or not; the NAS's files; and the same triples on the
        drives only (the backups)."""
        nas = walk(self.nas.root, skip=self._own())
        found = set()
        for rule, source in self.sources.items():
            prefix = source.rstrip("/") + "/"
            for rel, h in nas.items():
                if rel.startswith(prefix) and h != "unreadable":
                    found.add((rule, rel[len(prefix) :], h))
        on_drives = set()
        if self.drives.shelf.exists():
            for disk in self.drives.shelf.iterdir():
                skip = (disk / ".baruch", disk / "lost+found")
                for rel, h in walk(disk, skip=skip).items():
                    if "/" in rel and h != "unreadable":
                        rule, inner = rel.split("/", 1)
                        on_drives.add((rule, inner, h))
        return found | on_drives, nas, on_drives

    def loss_problems(self, before: tuple, output: str) -> list[str]:
        versions_before, nas_before, drives_before = before
        versions_after, nas_after, drives_after = self.snapshot()
        problems = []
        if nas_after != nas_before:
            diff = sorted(set(nas_before.items()) ^ set(nas_after.items()))
            problems.append(f"Baruch changed the NAS: {diff[:5]}")
        if self.host.usb.exists():
            for slot in self.host.usb.iterdir():
                if not slot.is_symlink() and slot.is_dir() and any(slot.iterdir()):
                    problems.append(f"written into the empty mount point {slot}")
        # Content counts, not paths: a renamed folder copied again isn't a loss.
        still = {(rule, h) for rule, _, h in versions_after}
        lost: dict[tuple[str, str], list[str]] = {}
        for rule, inner, h in sorted(versions_before):
            if (rule, h) not in still:
                lost.setdefault((rule, h), []).append(inner)
        for (rule, h), paths in sorted(lost.items()):
            if not any(explained(rule, inner, output) for inner in paths):
                problems.append(
                    f"LOST without warning: {rule}/{paths[0]} ({h}) is on no "
                    "drive and not on the NAS any more"
                )
        # The last backup of something the NAS still has may only go if the
        # output listed it (a run mirroring the NAS, or excluded names).
        backed_before = {(rule, h) for rule, _, h in drives_before}
        backed_after = {(rule, h) for rule, _, h in drives_after}
        on_nas = {(rule, h) for rule, _, h in versions_after - drives_after}
        gone_backups: dict[tuple[str, str], list[str]] = {}
        for rule, inner, h in sorted(drives_before):
            key = (rule, h)
            if key in on_nas and key in backed_before and key not in backed_after:
                gone_backups.setdefault(key, []).append(inner)
        for (rule, h), paths in sorted(gone_backups.items()):
            if not any(explained(rule, inner, output) for inner in paths):
                problems.append(
                    f"BACKUP LOST without warning: {rule}/{paths[0]} ({h}) is still "
                    "on the NAS but on no drive any more"
                )
        return problems

    def ok(self, *argv: str, answers: str = "") -> Result:
        result = self.run(*argv, answers=answers)
        assert result.code == 0, result.out
        return result

    def registry(self) -> dict:
        return yaml.safe_load((self.state_dir / "registry.yaml").read_text())

    def home(self, name: str) -> str | None:
        return self.registry()["units"].get(name)

    def records(self, drive: FakeDrive) -> dict:
        """The records on a drive (it needn't be docked)."""
        from baruch import records

        return records.parse((drive.root / ".baruch" / "units.yaml").read_text())

    def record(self, drive: FakeDrive, unit: str):
        return self.records(drive).get(unit)

    def register(
        self, name: str, disk: str | None = None, capacity: int = 100_000
    ) -> FakeDrive:
        """Create a disk, dock it, register it, and undock it again."""
        drive = self.drives.new(disk or name, capacity)
        self.drives.dock(drive)
        self.ok("drive", "register", name)
        self.drives.undock(drive)
        return drive

    def backup(self, drive: FakeDrive, answers: str = "y", *extra: str) -> Result:
        """Dock, run, answer, undock."""
        self.drives.dock(drive)
        try:
            return self.run("run", *extra, answers=answers)
        finally:
            self.drives.undock(drive)


needs_linux = pytest.mark.skipif(
    sys.platform == "darwin", reason="the Mac's file system can't represent these names"
)
not_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can read everything"
)
