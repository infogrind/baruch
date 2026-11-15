"""Sizes, names, configuration, registry format, the rsync check."""

import os
import stat
from datetime import UTC, date, datetime, timedelta

import pytest

from baruch import names
from baruch.config import ConfigError, load
from baruch.execute import RsyncError, check_rsync
from baruch.registry import OK, Copy, Drive, Registry, Unit, dump, parse
from baruch.sizes import human, parse_duration, parse_share, parse_size


def test_sizes_are_binary():
    assert parse_size("500G") == 500 * 1024**3
    assert parse_size("1.5T") == int(1.5 * 1024**4)
    assert parse_size("10") == 10
    assert parse_share("10%") == 0.1
    assert parse_duration("30d") == timedelta(days=30)
    assert human(1536) == "1.5 KiB"
    assert human(5 * 1024**4) == "5.0 TiB"


@pytest.mark.parametrize(
    "name",
    ["plain", "Bär", "back\\slash", "\\x41 literal", os.fsdecode(b"Caf\xe9")],
)
def test_names_round_trip(name):
    assert names.unescape(names.escape(name)) == name


def test_invalid_utf8_is_stored_escaped():
    assert names.escape(os.fsdecode(b"Caf\xe9")) == "Caf\\xe9"


def test_display_escapes_unprintable_characters():
    assert names.display("a\nb") == "a\\x0ab"


def test_similar_names_ignore_case_and_unicode_form():
    assert names.similar("tv/lost", ["tv/Lost", "tv/Wire"]) == ["tv/Lost"]
    assert names.similar("Bär", ["Bär"]) == ["Bär"]


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_config_loads(tmp_path):
    cfg = load(
        write(
            tmp_path,
            """
settings:
  exclude: ["@eaDir"]
  stale_after: 30d
drives:
  BK-20A: { reserve: 5% }
  BK-6A: { reserve: 500G }
rules:
  - { name: movies, source: /v/movies, pin: BK-20A }
  - { name: tv, source: /v/tv, spread: [BK-6A, BK-20A] }
""",
        )
    )
    assert cfg.drives["BK-20A"].reserve(1000) == 50
    assert cfg.drives["BK-6A"].reserve(10) == 500 * 1024**3
    assert cfg.rule("tv").drives == ["BK-6A", "BK-20A"]
    assert cfg.rule("movies").pinned


@pytest.mark.parametrize(
    "rules, problem",
    [
        ("- { name: a, source: /v/a, pin: X, spread: [X] }", "exactly one"),
        ("- { name: a, source: /v/a }", "exactly one"),
        ("- { name: 'a b', source: /v/a, pin: X }", "names may only use"),
        ("- { name: a, source: /v/a, pin: X, exclude: [x] }", "global only"),
        (
            "- { name: a, source: /v, pin: X }\n- { name: b, source: /v/b, pin: X }",
            "inside",
        ),
        (
            "- { name: a, source: /v, pin: X }\n- { name: a, source: /w, pin: X }",
            "more than once",
        ),
    ],
)
def test_config_problems(tmp_path, rules, problem):
    with pytest.raises(ConfigError) as e:
        load(write(tmp_path, "rules:\n" + rules))
    assert problem in str(e.value)


def test_unknown_settings_are_refused(tmp_path):
    with pytest.raises(ConfigError, match="verify_after"):
        load(write(tmp_path, "settings:\n  verify_after: 90d\n"))


def test_registry_round_trip():
    t = datetime(2026, 11, 14, 10, 5, tzinfo=UTC)
    reg = Registry()
    reg.drives["BK-6A"] = Drive("abc", 100, 50, t, t, None)
    reg.units[os.fsdecode(b"tv/Caf\xe9")] = Unit(
        "BK-6A", date(2026, 11, 1), {"BK-6A": Copy(t, OK, 10, unverified=True)}
    )
    text = dump(reg)
    assert "2026-11-14T10:05:00Z" in text
    assert parse(text) == reg


def fake_rsync(tmp_path, output):
    p = tmp_path / "rsync"
    p.write_text(f"#!/bin/sh\necho '{output}'\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_openrsync_and_old_rsync_are_refused(tmp_path):
    with pytest.raises(RsyncError, match="openrsync"):
        check_rsync(fake_rsync(tmp_path, "openrsync: protocol version 29"))
    with pytest.raises(RsyncError, match="older than"):
        check_rsync(fake_rsync(tmp_path, "rsync  version 3.1.2  protocol version 31"))
    check_rsync(fake_rsync(tmp_path, "rsync  version 3.2.7  protocol version 31"))


def test_rsync_messages_are_classified():
    from baruch.execute import DriveError, DriveFull, classify

    sender = 'rsync: [sender] send_files failed to open "/n/a": Permission denied (13)'
    gone = 'rsync: [sender] link_stat "/n/b" failed: No such file or directory (2)'
    receiver = 'rsync: [receiver] rename "/d/.a" -> "a": Operation not permitted (1)'
    assert classify(0, "").result == OK
    assert classify(23, sender).result == "errors"
    assert classify(23, gone).result == OK
    assert classify(24, 'file has vanished: "/n/c"').result == OK
    with pytest.raises(DriveError):
        classify(23, receiver)
    with pytest.raises(DriveFull):
        classify(11, "rsync: write failed on /d/a: No space left on device (28)")
    with pytest.raises(KeyboardInterrupt):
        classify(20, "rsync error: received SIGINT")
