"""Times are stored in UTC as ISO strings."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta


def to_text(t: datetime | None) -> str | None:
    if t is None:
        return None
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_text(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def show(t: datetime | None) -> str:
    if t is None:
        return "never"
    return t.astimezone(UTC).strftime("%Y-%m-%d %H:%M")


def show_date(t: datetime | None) -> str:
    return "never" if t is None else t.astimezone(UTC).strftime("%Y-%m-%d")


def duration(d: timedelta) -> str:
    """`45s`, `2m 51s`, `3h 05m`."""
    secs = max(0, int(d.total_seconds()))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    return f"{secs // 3600}h {secs % 3600 // 60:02d}m"
