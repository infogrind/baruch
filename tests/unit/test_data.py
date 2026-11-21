"""The data layer: records on the drives, the registry, the NAS's cache."""

import os
import subprocess
from datetime import UTC, datetime

import pytest

from baruch import cache, records
from baruch.records import ERRORS, IN_PROGRESS, OK, Record, RecordsError
from baruch.registry import Drive, Registry, Store
from baruch.scan import DIR, FILE, Entry, UnitScan, scan_drive, scan_unit

T1 = datetime(2026, 11, 2, 20, 30, tzinfo=UTC)
T2 = datetime(2026, 11, 2, 21, 14, tzinfo=UTC)

ODD = [
    "tv/The Wire",
    "tv/It's Always Sunny",
    "tv/back\\slash",
    "tv/tab\there",
    "tv/new\nline",
    "tv/{braces}: colon, comma",
    os.fsdecode(b"tv/Caf\xe9"),  # not valid UTF-8
    "tv/Café",  # NFC
    "tv/Café",  # NFD
    "tv/(files)",
]


def test_records_round_trip_with_odd_names():
    recs = {n: Record(T1, T2 if i % 2 else None, OK) for i, n in enumerate(ODD)}
    recs["tv/Lost"] = Record(T2, T1, IN_PROGRESS)
    text = records.dump(recs)
    assert records.parse(text) == recs
    # One record per line, never wrapped.
    body = [line for line in text.splitlines() if not line.startswith("#")]
    assert len(body) == len(recs)


def test_record_lines_have_the_fixed_form():
    line = records.line("tv/It's", Record(T1, None, ERRORS))
    assert line == (
        "'tv/It''s': {started: 2026-11-02T20:30:00Z, synced: null, result: errors}"
    )


def test_the_three_forms_of_a_name_stay_distinct():
    keys = {records.key(n) for n in ODD}
    assert len(keys) == len(ODD)


def test_record_lines_are_valid_yaml_too():
    import yaml

    text = records.dump({"tv/It's": Record(T1, T2, OK)})
    assert yaml.safe_load(text) == {
        "tv/It's": {"started": T1, "synced": T2, "result": "ok"}
    }


def test_complete_means_a_finished_sync():
    assert Record(T1, T2, OK).complete
    assert Record(T1, T2, ERRORS).complete
    assert not Record(T2, T1, IN_PROGRESS).complete
    assert not Record(T1, None, OK).complete


def test_missing_or_broken_records_are_errors_never_empty(tmp_path):
    with pytest.raises(RecordsError, match="missing"):
        records.read(tmp_path)
    records.path(tmp_path).parent.mkdir()
    records.path(tmp_path).write_text("'tv/X': {started: soon}\n")
    with pytest.raises(RecordsError, match="can't be read"):
        records.read(tmp_path)


def test_records_write_never_creates_folders_above_baruch(tmp_path):
    missing = tmp_path / "unmounted"
    with pytest.raises(FileNotFoundError):
        records.write(missing, {})
    assert not missing.exists()


def test_records_can_be_read_with_plain_sh(tmp_path):
    """restore.sh finds a unit's line by its quoted key at the line start."""
    name = "tv/It's here"
    records.write(tmp_path, {name: Record(T1, T2, OK), "tv/Other": Record(T1, T2, OK)})
    key = records.key(name)
    script = (
        'awk -v k="$1" \'index($0, k ": {") == 1 && /result: (ok|errors)}$/'
        " {found=1} END {exit !found}' \"$2\""
    )
    ok = subprocess.run(
        ["sh", "-c", script, "sh", key, str(records.path(tmp_path))], check=False
    )
    assert ok.returncode == 0
    missing = subprocess.run(
        ["sh", "-c", script, "sh", "'tv/It''s'", str(records.path(tmp_path))],
        check=False,
    )
    assert missing.returncode != 0


def test_cache_round_trip(tmp_path):
    c = cache.DriveCache("abc", T2)
    unit = UnitScan("The Wire")
    unit.entries = {
        "The Wire": Entry(DIR, 0, 1),
        "The Wire/S01E01.mkv": Entry(FILE, 10, 5),
    }
    c.set_unit("tv/The Wire", Record(T1, T2, OK), unit)
    c.contents.loose["stray.txt"] = Entry(FILE, 3, 4)
    cache.save(tmp_path, "BK-6A", c)
    back = cache.load(tmp_path, "BK-6A")
    assert back.records == c.records
    assert back.unit("tv/The Wire").entries == unit.entries
    assert back.contents.loose == c.contents.loose
    assert back.complete("tv/The Wire") == Record(T1, T2, OK)


def test_a_record_without_data_is_no_complete_copy():
    c = cache.DriveCache("abc")
    c.records["tv/X"] = Record(T1, T2, OK)
    assert c.complete("tv/X") is None


def test_clearing_a_unit_removes_record_and_contents_together():
    c = cache.DriveCache("abc")
    unit = UnitScan("X", {"X": Entry(DIR), "X/a": Entry(FILE, 1, 1)})
    c.set_unit("tv/X", Record(T1, T2, OK), unit)
    c.set_unit("tv/X", None, UnitScan("X"))
    assert "tv/X" not in c.records
    assert c.unit("tv/X") is None


def test_missing_cache_is_none_broken_cache_is_an_error(tmp_path):
    assert cache.load(tmp_path, "BK-6A") is None
    cache.path_of(tmp_path, "BK-6A").write_text("{")
    with pytest.raises(cache.CacheError):
        cache.load(tmp_path, "BK-6A")


def test_saved_versions_keep_registry_and_cached_records(tmp_path):
    store = Store(tmp_path, lambda: T1)
    reg = Registry()
    reg.drives["BK-6A"] = Drive("abc")
    store.create(reg)
    c = cache.DriveCache("abc")
    unit = UnitScan("X", {"X": Entry(DIR), "X/a": Entry(FILE, 1, 1)})
    c.set_unit("tv/X", Record(T1, T2, OK), unit)
    cache.save(store.cache_dir, "BK-6A", c)
    reg.homes["tv/X"] = "BK-6A"
    store = Store(tmp_path, lambda: T2)  # the next command
    store.save(reg)
    (version,) = store.versions.iterdir()
    assert (version / "registry.yaml").exists()
    saved = cache.load(version / "cache", "BK-6A")
    assert saved.records == c.records
    assert saved.unit("tv/X") is None  # contents come back with the next scan


def test_drive_scan_keeps_excluded_names_only_inside_units(tmp_path):
    (tmp_path / ".baruch").mkdir()
    (tmp_path / "@eaDir").mkdir()
    (tmp_path / "tv" / "@eaDir").mkdir(parents=True)
    (tmp_path / "tv" / "Show" / "@eaDir").mkdir(parents=True)
    (tmp_path / "tv" / "Show" / "@eaDir" / "thumb").write_text("x")
    (tmp_path / "stray.txt").write_text("y")
    scan = scan_drive(tmp_path, ["@eaDir"])
    assert set(scan.folders) == {"tv"}
    assert set(scan.folders["tv"].units) == {"Show", "(files)"}
    assert "Show/@eaDir/thumb" in scan.folders["tv"].units["Show"].entries
    assert set(scan.loose) == {"stray.txt"}


def test_only_a_folder_is_a_folder_units_data(tmp_path):
    (tmp_path / "X").write_text("a file named like a unit")
    assert scan_unit(tmp_path, "X", [], inner=False).entries == {}
    assert "X" in scan_unit(tmp_path, "(files)", [], inner=False).entries
