"""Test harness: a fake NAS, fake drives and a fake host, all in a temp folder.

Drives live on a "shelf" and are docked by symlinking them into the folder
that Baruch searches for USB drives. Free space is computed from the files on
the drive against a fake capacity, not from the real filesystem.
"""

from __future__ import annotations

import hashlib
import io
import os
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


@dataclass
class Result:
    code: int
    out: str

    @property
    def lines(self) -> list[str]:
        return self.out.rstrip("\n").splitlines()


def pin(name: str, source: str, drive: str) -> dict:
    return {"name": name, "source": source, "pin": drive}


def spread(name: str, source: str, drives: list[str]) -> dict:
    return {"name": name, "source": source, "spread": drives}


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
        code = main(list(argv), ctx)
        return Result(code, stdout.getvalue())

    def ok(self, *argv: str, answers: str = "") -> Result:
        result = self.run(*argv, answers=answers)
        assert result.code == 0, result.out
        return result

    def registry(self) -> dict:
        return yaml.safe_load((self.state_dir / "registry.yaml").read_text())

    def unit(self, name: str) -> dict:
        return self.registry()["units"][name]

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
