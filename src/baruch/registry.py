"""The registry on the NAS: the intentions, not the facts.

Per unit only its home (the drive it belongs on); per drive its disk ID and
what Baruch last measured there; and the disk IDs of forgotten drives. What
is actually on a drive is recorded on that drive (`records`) and cached on
the NAS (`cache`). States are derived, never stored.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from baruch import names
from baruch.timefmt import from_text, to_text

FORMAT = 2
KEEP_VERSIONS = 10


class RegistryError(Exception):
    pass


@dataclass
class Drive:
    disk_id: str
    capacity: int = 0
    free: int = 0
    last_seen: datetime | None = None
    last_complete_run: datetime | None = None
    last_verify: datetime | None = None
    read_errors: int = 0  # at the last verify
    left_out: dict[str, str] = field(default_factory=dict)  # last run: unit -> why


@dataclass
class Registry:
    homes: dict[str, str] = field(default_factory=dict)  # unit -> drive
    drives: dict[str, Drive] = field(default_factory=dict)
    forgotten: set[str] = field(default_factory=set)  # disk IDs

    def units_of_rule(self, rule: str) -> dict[str, str]:
        return {n: h for n, h in self.homes.items() if rule_of(n) == rule}


def rule_of(unit_name: str) -> str:
    return unit_name.split("/", 1)[0]


def folder_of(unit_name: str) -> str:
    return unit_name.split("/", 1)[1]


# Serialisation


def to_doc(reg: Registry) -> dict:
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
            "left_out": {names.escape(u): w for u, w in sorted(d.left_out.items())},
        }
    return {
        "format": FORMAT,
        "units": {names.escape(n): reg.homes[n] for n in sorted(reg.homes)},
        "drives": drives,
        "forgotten": sorted(reg.forgotten),
    }


def from_doc(doc: dict) -> Registry:
    if not isinstance(doc, dict):
        raise RegistryError("not a Baruch registry")
    if doc.get("format") != FORMAT:
        if isinstance(doc.get("units"), dict) and "drives" in doc:
            raise RegistryError(
                "this registry is from Baruch v0.1.0, which this version doesn't "
                "read; move it aside and run `baruch init`"
            )
        raise RegistryError("not a Baruch registry (no format 2)")
    reg = Registry()
    for key, home in (doc.get("units") or {}).items():
        reg.homes[names.unescape(str(key))] = str(home)
    for name, raw in (doc.get("drives") or {}).items():
        reg.drives[str(name)] = Drive(
            str(raw["disk_id"]),
            int(raw.get("capacity") or 0),
            int(raw.get("free") or 0),
            from_text(raw.get("last_seen")),
            from_text(raw.get("last_complete_run")),
            from_text(raw.get("last_verify")),
            int(raw.get("read_errors") or 0),
            {
                names.unescape(str(u)): str(w)
                for u, w in (raw.get("left_out") or {}).items()
            },
        )
    reg.forgotten = {str(x) for x in doc.get("forgotten") or []}
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
    """The state folder: the registry, the drives' cache, saved versions and
    the lock."""

    def __init__(self, state_dir: Path, clock):
        self.state_dir = state_dir
        self.path = state_dir / "registry.yaml"
        self.cache_dir = state_dir / "cache"
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

    def before_change(self) -> None:
        """Keep a version of the registry and the cached records, once per
        command, before the first change."""
        if not self._versioned and self.path.exists():
            self._keep_version()
        self._versioned = True

    def save(self, reg: Registry) -> None:
        self.before_change()
        write_atomic(self.path, dump(reg))

    def create(self, reg: Registry) -> None:
        write_atomic(self.path, dump(reg))
        self._versioned = True

    def _keep_version(self) -> None:
        self.versions.mkdir(parents=True, exist_ok=True)
        stamp = self.clock().strftime("%Y%m%dT%H%M%S")
        target = self.versions / stamp
        n = 1
        while target.exists():
            n += 1
            target = self.versions / f"{stamp}-{n}"
        target.mkdir()
        shutil.copy2(self.path, target / "registry.yaml")
        # The cached records too; the contents come back with the next scan.
        from baruch import cache

        if self.cache_dir.is_dir():
            for f in sorted(self.cache_dir.glob("*.json")):
                try:
                    text = cache.records_only(self.cache_dir, f.stem)
                except cache.CacheError:
                    continue
                if text is not None:
                    write_atomic(target / "cache" / f.name, text)
        old = sorted(self.versions.iterdir(), key=lambda p: p.stat().st_mtime_ns)
        for p in old[:-KEEP_VERSIONS]:
            shutil.rmtree(p) if p.is_dir() else p.unlink()

    def lock(self) -> bool:
        """One changing command at a time, held until the process ends."""
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
