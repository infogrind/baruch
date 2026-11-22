"""Finding docked drives by their label, never by mount path."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

import yaml

from baruch.registry import Registry, write_atomic

BARUCH_DIR = ".baruch"
LABEL = "label.yaml"


@dataclass
class Label:
    name: str
    disk_id: str


@dataclass
class Docked:
    path: Path
    label: Label | None
    status: str  # "ok", "no label", "bad label", "unknown", "forgotten", "mismatch"
    name: str | None = None  # the registered drive, if status == "ok"

    @property
    def usable(self) -> bool:
        return self.status == "ok"

    def reason(self) -> str:
        if self.status == "no label":
            return "no label (register it with `baruch drive register NAME`)"
        if self.status == "bad label":
            return "label can't be read"
        assert self.label
        if self.status == "unknown":
            return f"label {self.label.name} isn't registered"
        if self.status == "forgotten":
            return f"label of {self.label.name}, which was forgotten"
        if self.status == "mismatch":
            return f"label says {self.label.name}, but the disk ID doesn't match"
        return f"registered drive {self.name}"


def read_label(root: Path) -> Label | None | str:
    path = root / BARUCH_DIR / LABEL
    try:
        doc = yaml.safe_load(path.read_text())
        return Label(str(doc["name"]), str(doc["disk_id"]))
    except FileNotFoundError:
        return None
    except (OSError, yaml.YAMLError, KeyError, TypeError):
        return "bad"


def write_label(root: Path, name: str) -> Label:
    label = Label(name, secrets.token_hex(8))
    write_to_drive(
        root / BARUCH_DIR / LABEL,
        yaml.safe_dump({"name": label.name, "disk_id": label.disk_id}),
    )
    return label


def write_to_drive(path: Path, text: str) -> None:
    """Write one of Baruch's files under /.baruch/ on a drive. Never creates
    folders above it, so nothing lands in an empty mount point."""
    path.parent.mkdir(exist_ok=True)
    write_atomic(path, text, parents=False)


def classify(path: Path, reg: Registry) -> Docked:
    label = read_label(path)
    if label is None:
        return Docked(path, None, "no label")
    if isinstance(label, str):
        return Docked(path, None, "bad label")
    if label.disk_id in reg.forgotten:
        return Docked(path, label, "forgotten")
    drive = reg.drives.get(label.name)
    if drive is None:
        return Docked(path, label, "unknown")
    if drive.disk_id != label.disk_id:
        return Docked(path, label, "mismatch")
    return Docked(path, label, "ok", label.name)


def docked(host, reg: Registry) -> list[Docked]:
    return [classify(p, reg) for p in host.drive_paths() if host.is_mount(p)]


def still_there(host, d: Docked) -> bool:
    """Mount check before each unit: still mounted, same label and disk ID."""
    if not host.is_mount(d.path):
        return False
    label = read_label(d.path)
    return isinstance(label, Label) and d.label is not None and label == d.label
