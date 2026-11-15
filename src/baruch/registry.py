"""The registry: per unit where it should be (home) and where copies exist.

States are never stored; they're derived from `home`, `copies` and the last
scan of the NAS (see `state()`).
"""

from __future__ import annotations

import fcntl
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import yaml

from baruch import names
from baruch.timefmt import from_text, to_text

KEEP_VERSIONS = 10

OK, ERRORS, SKIPPED, INCOMPLETE = "ok", "errors", "skipped", "incomplete"


class RegistryError(Exception):
    pass


@dataclass
class Copy:
    synced: datetime | None
    result: str
    size: int
    unverified: bool = False

    @property
    def confirmed(self) -> bool:
        return self.result == OK and not self.unverified


@dataclass
class Unit:
    home: str | None
    first_seen: date
    copies: dict[str, Copy] = field(default_factory=dict)


@dataclass
class Drive:
    disk_id: str | None
    capacity: int = 0
    free: int = 0
    last_seen: datetime | None = None
    last_complete_run: datetime | None = None
    last_verify: datetime | None = None
    read_errors: int = 0  # at the last verify

    @property
    def forgotten(self) -> bool:
        return self.disk_id is None


@dataclass
class Registry:
    units: dict[str, Unit] = field(default_factory=dict)
    drives: dict[str, Drive] = field(default_factory=dict)

    def active_drive(self, name: str) -> Drive | None:
        d = self.drives.get(name)
        return d if d and not d.forgotten else None

    def units_of_rule(self, rule: str) -> dict[str, Unit]:
        return {n: u for n, u in self.units.items() if rule_of(n) == rule}


def rule_of(unit_name: str) -> str:
    return unit_name.split("/", 1)[0]


def folder_of(unit_name: str) -> str:
    return unit_name.split("/", 1)[1]


# Serialisation


def to_doc(reg: Registry) -> dict:
    units = {}
    for name in sorted(reg.units):
        u = reg.units[name]
        copies = {}
        for drive in sorted(u.copies):
            c = u.copies[drive]
            entry = {"synced": to_text(c.synced), "result": c.result, "size": c.size}
            if c.unverified:
                entry["unverified"] = True
            copies[drive] = entry
        units[names.escape(name)] = {
            "home": u.home,
            "first_seen": u.first_seen.isoformat(),
            "copies": copies,
        }
    drives = {}
    for name in sorted(reg.drives):
        d = reg.drives[name]
        drives[name] = {
            "disk_id": d.disk_id,
            "capacity": d.capacity,
            "free": d.free,
            "last_seen": to_text(d.last_seen),
            "last_complete_run": to_text(d.last_complete_run),
            "last_verify": to_text(d.last_verify),
            "read_errors": d.read_errors,
        }
    return {"units": units, "drives": drives}


def from_doc(doc: dict) -> Registry:
    if not isinstance(doc, dict) or "units" not in doc or "drives" not in doc:
        raise RegistryError("not a Baruch registry (needs units and drives)")
    reg = Registry()
    for key, raw in (doc["units"] or {}).items():
        copies = {
            str(drive): Copy(
                from_text(c.get("synced")),
                str(c["result"]),
                int(c.get("size") or 0),
                bool(c.get("unverified", False)),
            )
            for drive, c in (raw.get("copies") or {}).items()
        }
        first = raw.get("first_seen")
        first_seen = (
            first if isinstance(first, date) else date.fromisoformat(str(first))
        )
        reg.units[names.unescape(str(key))] = Unit(raw.get("home"), first_seen, copies)
    for name, raw in (doc["drives"] or {}).items():
        reg.drives[str(name)] = Drive(
            raw.get("disk_id"),
            int(raw.get("capacity") or 0),
            int(raw.get("free") or 0),
            from_text(raw.get("last_seen")),
            from_text(raw.get("last_complete_run")),
            from_text(raw.get("last_verify")),
            int(raw.get("read_errors") or 0),
        )
    return reg


def dump(reg: Registry) -> str:
    return yaml.safe_dump(to_doc(reg), sort_keys=False, allow_unicode=True, width=1000)


def parse(text: str) -> Registry:
    try:
        return from_doc(yaml.safe_load(text))
    except (yaml.YAMLError, KeyError, ValueError, TypeError, AttributeError) as e:
        raise RegistryError(f"can't read the registry: {e}") from None


def write_atomic(path: Path, text: str, parents: bool = True) -> None:
    if parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class Store:
    """The registry file in the state folder, with versions and a lock."""

    def __init__(self, state_dir: Path, clock):
        self.state_dir = state_dir
        self.path = state_dir / "registry.yaml"
        self.versions = state_dir / "versions"
        self.clock = clock
        self._versioned = False
        self._lock_file = None

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> Registry:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise RegistryError(f"No registry at {self.path}.") from None
        return parse(text)

    def save(self, reg: Registry) -> None:
        if not self._versioned and self.path.exists():
            self._keep_version()
        self._versioned = True
        write_atomic(self.path, dump(reg))

    def create(self, reg: Registry) -> None:
        write_atomic(self.path, dump(reg))
        self._versioned = True

    def _keep_version(self) -> None:
        self.versions.mkdir(parents=True, exist_ok=True)
        stamp = self.clock().strftime("%Y%m%dT%H%M%S")
        target = self.versions / f"registry-{stamp}.yaml"
        n = 1
        while target.exists():
            n += 1
            target = self.versions / f"registry-{stamp}-{n}.yaml"
        shutil.copy2(self.path, target)
        old = sorted(self.versions.iterdir(), key=lambda p: p.stat().st_mtime_ns)
        for p in old[:-KEEP_VERSIONS]:
            p.unlink()

    def lock(self) -> bool:
        """One Baruch command at a time. Held until the process ends."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock_file = open(self.state_dir / "lock", "w")  # noqa: SIM115
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def unlock(self) -> None:
        if self._lock_file:
            self._lock_file.close()
            self._lock_file = None
