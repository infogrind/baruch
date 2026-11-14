"""The configuration file: settings, drives and rules. Edited by hand."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import yaml

from baruch.sizes import FormatError, parse_duration, parse_share, parse_size

RULE_NAME = re.compile(r"[A-Za-z0-9_-]+")
MARKER = ".backup-source"


class ConfigError(Exception):
    """The configuration can't be used. Carries every problem found."""

    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass
class Settings:
    exclude: list[str] = field(default_factory=list)
    stale_after: timedelta = timedelta(days=30)
    warn_changed_files: float = 0.10
    warn_changed_bytes: float = 0.10
    warn_unit_loss: float = 0.50


@dataclass
class DriveConf:
    name: str
    reserve_share: float | None = None
    reserve_bytes: int | None = None

    def reserve(self, capacity: int) -> int:
        if self.reserve_bytes is not None:
            return self.reserve_bytes
        return int(capacity * (self.reserve_share or 0))


@dataclass
class Rule:
    name: str
    source: Path
    pin: str | None = None
    spread: list[str] | None = None

    @property
    def drives(self) -> list[str]:
        return [self.pin] if self.pin else list(self.spread or [])

    @property
    def pinned(self) -> bool:
        return self.pin is not None


@dataclass
class Config:
    settings: Settings
    drives: dict[str, DriveConf]
    rules: list[Rule]
    path: Path | None = None

    def rule(self, name: str) -> Rule | None:
        return next((r for r in self.rules if r.name == name), None)


def load(path: Path) -> Config:
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        raise ConfigError([f"No configuration at {path}."]) from None
    except yaml.YAMLError as e:
        raise ConfigError([f"{path}: not valid YAML: {e}"]) from None
    problems: list[str] = []
    settings = _settings(doc.get("settings") or {}, problems)
    drives = _drives(doc.get("drives") or {}, problems)
    rules = _rules(doc.get("rules") or [], problems)
    if problems:
        raise ConfigError(problems)
    return Config(settings, drives, rules, path)


def _settings(raw: dict, problems: list[str]) -> Settings:
    s = Settings()
    try:
        s.exclude = [str(x) for x in raw.get("exclude", [])]
        if "stale_after" in raw:
            s.stale_after = parse_duration(raw["stale_after"])
        for key in ("warn_changed_files", "warn_changed_bytes", "warn_unit_loss"):
            if key in raw:
                setattr(s, key, parse_share(raw[key]))
    except FormatError as e:
        problems.append(f"settings: {e}")
    unknown = set(raw) - {
        "exclude",
        "stale_after",
        "warn_changed_files",
        "warn_changed_bytes",
        "warn_unit_loss",
    }
    for key in sorted(unknown):
        problems.append(f"settings: unknown key {key!r}")
    return s


def _drives(raw: dict, problems: list[str]) -> dict[str, DriveConf]:
    out = {}
    for name, entry in raw.items():
        reserve = (entry or {}).get("reserve", "0%")
        conf = DriveConf(str(name))
        try:
            if str(reserve).strip().endswith("%"):
                conf.reserve_share = parse_share(reserve)
            else:
                conf.reserve_bytes = parse_size(reserve)
        except FormatError as e:
            problems.append(f"drives: {name}: {e}")
        out[str(name)] = conf
    return out


def _rules(raw: list, problems: list[str]) -> list[Rule]:
    rules: list[Rule] = []
    for entry in raw:
        name = str(entry.get("name", ""))
        if not RULE_NAME.fullmatch(name):
            problems.append(
                f"rule {name!r}: names may only use A-Z a-z 0-9 _ - (it's also the "
                "folder name on each drive)"
            )
        if "exclude" in entry:
            problems.append(f"rule {name!r}: excludes are global only (in settings)")
        if "source" not in entry:
            problems.append(f"rule {name!r}: no source")
            continue
        pin, spread = entry.get("pin"), entry.get("spread")
        if bool(pin) == bool(spread):
            problems.append(f"rule {name!r}: needs exactly one of pin or spread")
        if spread is not None and not isinstance(spread, list):
            problems.append(f"rule {name!r}: spread must be a list of drives")
            spread = None
        rules.append(
            Rule(
                name,
                Path(entry["source"]),
                str(pin) if pin else None,
                [str(d) for d in spread] if spread else None,
            )
        )
    names = [r.name for r in rules]
    for name in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"rule {name!r}: defined more than once")
    for i, a in enumerate(rules):
        for b in rules[i + 1 :]:
            if a.source == b.source:
                problems.append(
                    f"rules {a.name!r} and {b.name!r} have the same source; "
                    "sources must not overlap"
                )
                continue
            if b.source.is_relative_to(a.source):
                inner, outer = b, a
            elif a.source.is_relative_to(b.source):
                inner, outer = a, b
            else:
                continue
            problems.append(
                f"the source of rule {inner.name!r} ({inner.source}) is inside the "
                f"source of rule {outer.name!r} ({outer.source}); sources must not "
                "overlap"
            )
    return rules


def check(config: Config) -> list[str]:
    """Problems `config check` reports beyond those that stop loading."""
    problems = []
    for rule in config.rules:
        if not rule.source.is_dir():
            problems.append(f"rule {rule.name!r}: source {rule.source} doesn't exist")
        elif not (rule.source / MARKER).exists():
            problems.append(f"rule {rule.name!r}: {rule.source} has no {MARKER} marker")
        for drive in rule.drives:
            if drive not in config.drives:
                problems.append(
                    f"rule {rule.name!r}: drive {drive} isn't listed under drives"
                )
    return problems
