"""Planner and stale-data logic: scans, records and caches in, plan out."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from baruch import planner, stale
from baruch.cache import DriveCache
from baruch.config import Config, DriveConf, Rule, Settings
from baruch.records import ERRORS, IN_PROGRESS, OK, Record
from baruch.registry import Drive, Registry
from baruch.scan import DIR, FILE, FILES_UNIT, DriveScan, Entry, TreeScan, UnitScan

T0 = datetime(2026, 10, 1, tzinfo=UTC)
T1 = datetime(2026, 11, 1, tzinfo=UTC)
T2 = datetime(2026, 11, 20, tzinfo=UTC)
GIB = 1024**3


def unit(folder: str, files: dict) -> UnitScan:
    """files: rel inside the unit -> size or (size, mtime)."""
    u = UnitScan(folder)
    if folder != FILES_UNIT:
        u.entries[folder] = Entry(DIR, 0, 1)
    for rel, spec in files.items():
        size, mtime = spec if isinstance(spec, tuple) else (spec, 1)
        path = rel if folder == FILES_UNIT else f"{folder}/{rel}"
        u.entries[path] = Entry(FILE, size, mtime)
    return u


def tree(units: dict[str, dict], marker: bool = False) -> TreeScan:
    t = TreeScan()
    for folder, files in units.items():
        t.units[folder] = unit(folder, files)
    files = t.units.setdefault(FILES_UNIT, UnitScan(FILES_UNIT))
    if marker:
        files.entries[".backup-source"] = Entry(FILE, 0, 1)
    return t


def nas(**rules: dict) -> dict[str, TreeScan]:
    return {name: tree(units, marker=True) for name, units in rules.items()}


def drive_scan(**rules: dict) -> DriveScan:
    d = DriveScan()
    for name, units in rules.items():
        d.folders[name] = tree(units)
    return d


def cache_of(disk_id: str, scan: DriveScan, recs: dict) -> DriveCache:
    return DriveCache(disk_id, T2, recs, scan)


def config(rules: dict[str, list[str]], reserves: dict[str, int], exclude=()):
    return Config(
        Settings(exclude=list(exclude)),
        {n: DriveConf(n, reserve_bytes=r) for n, r in reserves.items()},
        [Rule(name, Path(f"/v/{name}"), drives) for name, drives in rules.items()],
    )


def registry(free: dict[str, int], homes: dict | None = None) -> Registry:
    reg = Registry()
    for name, f in free.items():
        reg.drives[name] = Drive(name.lower(), capacity=100 * GIB, free=f)
    reg.homes = dict(homes or {})
    return reg


def plan(cfg, reg, drive, free, nas_scan, disk=None, recs=None, caches=None, **kw):
    # Every rule's (files) unit holds its marker; give it a home up front so
    # the tests are about the other units.
    for rule in cfg.rules:
        home = drive if drive in rule.drives else rule.drives[0]
        reg.homes.setdefault(f"{rule.name}/{FILES_UNIT}", home)
    return planner.make_plan(
        config=cfg,
        reg=reg,
        drive=drive,
        capacity=100 * GIB,
        free=free,
        nas=nas_scan,
        disk=disk or DriveScan(),
        records=recs or {},
        caches=caches or {},
        **kw,
    )


def ops(p):
    return [o for o in p.ops if not o.unit.endswith(FILES_UNIT)]


def ok(at=T1):
    return Record(at, at, OK)


TV = {"tv": ["BK-6A", "BK-6B", "BK-20A"]}
RES = {"BK-6A": 10, "BK-6B": 10, "BK-20A": 10}


# Placement


def test_new_unit_goes_to_the_first_listed_drive_with_room():
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 1000, "BK-6B": 1000, "BK-20A": 1000})
    p = plan(cfg, reg, "BK-6A", 1000, nas(tv={"Lost": {"e1": 100}}))
    assert p.new_homes == {"tv/Lost": "BK-6A"}
    (op,) = [o for o in ops(p) if o.unit == "tv/Lost"]
    assert op.kind == planner.FIRST and op.write_bytes == 100


def test_new_unit_goes_to_a_later_drive_if_the_earlier_one_is_full():
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 50, "BK-6B": 1000, "BK-20A": 1000})
    p = plan(cfg, reg, "BK-20A", 1000, nas(tv={"Lost": {"e1": 100}}))
    assert p.new_homes == {"tv/Lost": "BK-6B"}  # recorded free space of BK-6B
    assert not any(o.unit == "tv/Lost" for o in ops(p))


def test_placement_counts_units_placed_earlier_in_the_same_plan():
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 0, "BK-6B": 300, "BK-20A": 0})
    shows = {f"S{i}": {"e": 100} for i in range(5)}
    p = plan(cfg, reg, "BK-20A", 10_000, nas(tv=shows))
    homes = sorted(p.new_homes.values())
    assert homes.count("BK-6B") == 2  # 300 - 2*100 >= 10, a third wouldn't fit
    assert homes.count("BK-20A") == 3


def test_growth_of_a_drives_units_counts_against_its_room():
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 300, "BK-6B": 1000, "BK-20A": 0}, {"tv/Old": "BK-6A"})
    cached = cache_of("bk-6a", drive_scan(tv={"Old": {"e": 100}}), {"tv/Old": ok()})
    # tv/Old has grown by 250 on the NAS; BK-6A has 50 left for new units.
    nas_scan = nas(tv={"Old": {"e": 100, "f": 250}, "New": {"e": 100}})
    p = plan(cfg, reg, "BK-20A", 0, nas_scan, caches={"BK-6A": cached})
    assert p.new_homes == {"tv/New": "BK-6B"}


def test_a_drive_already_holding_the_data_wins_even_if_later_in_the_list():
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 1000, "BK-6B": 1000, "BK-20A": 1000})
    disk = drive_scan(tv={"Lost": {"e1": 100}})
    p = plan(cfg, reg, "BK-20A", 1000, nas(tv={"Lost": {"e1": 100}}), disk=disk)
    assert p.new_homes == {"tv/Lost": "BK-20A"}
    (op,) = [o for o in ops(p) if o.unit == "tv/Lost"]
    assert op.kind == planner.SYNC and op.unrecorded and not op.has_changes


def test_of_two_drives_holding_data_the_newest_record_wins():
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 1000, "BK-6B": 1000, "BK-20A": 1000})
    data = drive_scan(tv={"Lost": {"e1": 100}})
    caches = {
        "BK-6A": cache_of("bk-6a", data, {}),  # no record: ranks lowest
        "BK-6B": cache_of("bk-6b", data, {"tv/Lost": ok(T1)}),
        "BK-20A": cache_of("bk-20a", data, {"tv/Lost": ok(T0)}),
    }
    p = plan(cfg, reg, "BK-6A", 1000, nas(tv={"Lost": {"e1": 100}}), caches=caches)
    assert p.new_homes == {"tv/Lost": "BK-6B"}


def test_unit_that_fits_nowhere_is_unplaced():
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 50, "BK-6B": 50, "BK-20A": 50})
    p = plan(cfg, reg, "BK-6A", 50, nas(tv={"Big": {"e": 100}}))
    assert p.unplaced == [("tv/Big", 100)]
    assert not p.new_homes


def test_unregistered_drives_and_drives_not_configured_are_skipped():
    cfg = config(TV, {"BK-6B": 10, "BK-20A": 10})  # BK-6A not under drives
    reg = registry({"BK-6A": 1000, "BK-20A": 1000})  # BK-6B not registered
    p = plan(cfg, reg, "BK-20A", 1000, nas(tv={"Lost": {"e": 100}}))
    assert p.new_homes == {"tv/Lost": "BK-20A"}


# Config changes


def test_drive_removed_from_the_list_places_its_units_again():
    cfg = config({"tv": ["BK-6B", "BK-20A"]}, RES)
    reg = registry({"BK-6A": 0, "BK-6B": 1000, "BK-20A": 1000}, {"tv/X": "BK-6A"})
    p = plan(cfg, reg, "BK-20A", 1000, nas(tv={"X": {"e": 100}}))
    assert p.rehomed == {"tv/X": ("BK-6A", "BK-6B")}
    assert "tv/X: home BK-6A → BK-6B (BK-6A isn't in its rule)" in p.registry_changes()


def test_removed_rule_drops_its_units_with_a_warning():
    cfg = config({"movies": ["BK-20A"]}, RES)
    reg = registry({"BK-20A": 1000}, {"tv/A": "BK-20A", "tv/B": "BK-20A"})
    p = plan(cfg, reg, "BK-20A", 1000, nas(movies={}))
    assert {u for u, _ in p.dropped} == {"tv/A", "tv/B"}
    assert any("rule tv removed: 2 units dropped" in w for w in p.warnings)
    assert p.needs_answer


def test_newly_excluded_unit_is_dropped_and_its_data_listed():
    cfg = config(TV, RES, exclude=["Old*"])
    reg = registry({"BK-6A": 1000, "BK-6B": 1000, "BK-20A": 1000}, {"tv/Old": "BK-6B"})
    caches = {"BK-6B": cache_of("bk-6b", drive_scan(tv={"Old": {"e": 1}}), {})}
    nas_scan = nas(tv={})  # the NAS scan doesn't list excluded folders
    p = plan(cfg, reg, "BK-6A", 1000, nas_scan, caches=caches)
    assert p.dropped == [("tv/Old", "its folder is now excluded")]
    assert p.excluded_data == {"tv/Old": ["BK-6B"]}
    assert not p.gone


def test_rule_with_missing_marker_is_left_alone():
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 1000}, {"tv/X": "BK-6A"})
    p = plan(cfg, reg, "BK-6A", 1000, {"tv": None})
    assert p.markers_missing == ["tv"]
    assert not p.gone and not ops(p) and not p.registry_changes()


# Runs only sync inside home units, never delete a whole unit


def test_gone_unit_is_not_deleted_by_a_run():
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 1000}, {"tv/Gone": "BK-6A"})
    disk = drive_scan(tv={"Gone": {"e": 100}})
    p = plan(cfg, reg, "BK-6A", 1000, nas(tv={}), disk=disk, recs={"tv/Gone": ok()})
    assert not ops(p)
    assert p.gone == {"tv": ["tv/Gone"]}
    (item,) = p.stale
    assert item.kind == stale.GONE and item.action == stale.PRUNE
    assert item.only_files == 1


def test_gone_unit_without_data_anywhere_loses_its_entry():
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 1000}, {"tv/Gone": "BK-6A"})
    p = plan(cfg, reg, "BK-6A", 1000, nas(tv={}))
    assert p.drop_gone == ["tv/Gone"]


def test_data_of_a_unit_homed_elsewhere_is_never_touched_by_a_run():
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 1000, "BK-6B": 1000}, {"tv/X": "BK-6B"})
    disk = drive_scan(tv={"X": {"e": 100, "old": 50}})
    p = plan(cfg, reg, "BK-6A", 1000, nas(tv={"X": {"e": 100}}), disk=disk)
    assert not ops(p)
    (item,) = p.stale
    assert item.action == stale.HOLD and "BK-6B has no cache" in item.reason


def test_files_deleted_on_the_nas_are_deleted_in_the_home_unit():
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 1000}, {"tv/X": "BK-6A"})
    disk = drive_scan(tv={"X": {"e1": 100, "e2": 100}})
    p = plan(
        cfg,
        reg,
        "BK-6A",
        1000,
        nas(tv={"X": {"e1": 100}}),
        disk=disk,
        recs={"tv/X": ok()},
    )
    (op,) = ops(p)
    assert op.delete == ["X/e2"] and not op.unrecorded


def test_excluded_files_inside_a_unit_are_deleted_and_listed_apart():
    cfg = config(TV, RES, exclude=["@eaDir"])
    reg = registry({"BK-6A": 1000}, {"tv/X": "BK-6A"})
    disk = drive_scan(tv={"X": {"e": 100, "@eaDir/thumb": 5}})
    disk.folders["tv"].units["X"].entries["X/@eaDir"] = Entry(DIR, 0, 1)
    p = plan(
        cfg,
        reg,
        "BK-6A",
        1000,
        nas(tv={"X": {"e": 100}}),
        disk=disk,
        recs={"tv/X": ok()},
    )
    (op,) = ops(p)
    assert op.excluded == ["X/@eaDir", "X/@eaDir/thumb"]


def test_unchanged_home_units_are_in_the_plan_for_their_record():
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 1000}, {"tv/X": "BK-6A"})
    disk = drive_scan(tv={"X": {"e": 100}})
    disk.folders["tv"].units[FILES_UNIT] = unit(FILES_UNIT, {".backup-source": 0})
    p = plan(
        cfg,
        reg,
        "BK-6A",
        1000,
        nas(tv={"X": {"e": 100}}),
        disk=disk,
        recs={"tv/X": ok()},
    )
    assert [o.unit for o in ops(p)] == ["tv/X"]
    assert not p.file_ops and not p.needs_answer


def test_interrupted_unit_goes_first():
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 1000}, {"tv/A": "BK-6A", "tv/B": "BK-6A"})
    disk = drive_scan(tv={"A": {"e": 100}, "B": {"e": 1}})
    recs = {"tv/A": ok(), "tv/B": Record(T2, T1, IN_PROGRESS)}
    nas_scan = nas(tv={"A": {"e": (100, 2)}, "B": {"e": (1, 2)}})
    p = plan(cfg, reg, "BK-6A", 1000, nas_scan, disk=disk, recs=recs)
    assert [o.unit for o in ops(p)] == ["tv/B", "tv/A"]


def test_unit_that_doesnt_fit_is_skipped():
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 50}, {"tv/X": "BK-6A"})
    p = plan(cfg, reg, "BK-6A", 50, nas(tv={"X": {"e": 100}}))
    assert not ops(p)
    assert p.skipped[0][0] == "tv/X" and "doesn't fit" in p.skipped[0][1]


def test_folder_and_file_of_the_same_name_collide_without_deleting():
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 1000}, {"tv/X": "BK-6A", f"tv/{FILES_UNIT}": "BK-6A"})
    disk = drive_scan(tv={})
    disk.folders["tv"].units[FILES_UNIT].entries["X"] = Entry(FILE, 5, 1)
    nas_scan = nas(tv={"X": {"e": 1}})
    p = plan(cfg, reg, "BK-6A", 1000, nas_scan, disk=disk)
    assert ("tv/X", "skipped: name collides with data on the drive, see the plan") in (
        p.skipped
    )
    files_op = next(o for o in p.ops if o.unit == f"tv/{FILES_UNIT}")
    assert "X" in files_op.delete  # the old file goes the normal way


# Warnings


def test_files_overwritten_in_place_count_towards_the_unit_loss():
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 1000}, {"tv/X": "BK-6A"})
    disk = drive_scan(tv={"X": {"a": (100, 1), "b": (100, 1)}})
    encrypted = nas(tv={"X": {"a": (100, 9), "b": (100, 9)}})
    p = plan(cfg, reg, "BK-6A", 1000, encrypted, disk=disk, recs={"tv/X": ok()})
    assert any("tv/X would lose 100%" in w for w in p.warnings)


def test_baruchs_own_state_doesnt_count_towards_the_warnings():
    cfg = config({"home": ["BK-20A"]}, {"BK-20A": 10})
    reg = registry({"BK-20A": 1000}, {"home/.local": "BK-20A"})
    files = {"state/baruch/registry.yaml": (100, 1)}
    disk = drive_scan(home={".local": files})
    changed = nas(home={".local": {"state/baruch/registry.yaml": (100, 2)}})
    p = plan(
        cfg,
        reg,
        "BK-20A",
        1000,
        changed,
        disk=disk,
        recs={"home/.local": ok()},
        own_state=("home", ".local/state/baruch"),
    )
    (op,) = ops(p)
    assert op.own == [".local/state/baruch/registry.yaml"]
    assert not p.warnings


# Stale data and prune's rule


def _moved(home_rec, here_rec, home_data=True):
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 1000, "BK-6B": 1000}, {"tv/X": "BK-6B"})
    data = drive_scan(tv={"X": {"e": 100}})
    home = cache_of("bk-6b", data if home_data else DriveScan(), {})
    if home_rec:
        home.records["tv/X"] = home_rec
    here = {"tv/X": here_rec} if here_rec else {}
    items = stale.classify(
        config=cfg,
        reg=reg,
        drive="BK-6A",
        contents=data,
        records=here,
        nas=nas(tv={"X": {"e": 100}}),
        caches={"BK-6B": home},
    )
    (item,) = items
    return item


def test_moved_unit_can_go_once_the_home_has_a_newer_copy():
    item = _moved(ok(T2), ok(T1))
    assert item.action == stale.PRUNE and "newer complete copy" in item.reason
    assert item.only_files == 0


def test_moved_unit_without_a_record_here_counts_as_older():
    assert _moved(ok(T2), None).action == stale.PRUNE


def test_moved_unit_is_held_for_every_reason_the_spec_names():
    assert "no copy yet" in _moved(None, ok()).reason
    assert "no copy yet" in _moved(ok(T2), ok(), home_data=False).reason
    assert "incomplete" in _moved(Record(T2, T1, IN_PROGRESS), ok()).reason
    assert "with errors" in _moved(Record(T2, T2, ERRORS), ok()).reason
    # Moved away and back: the copy here is the newer one.
    assert "older copy" in _moved(ok(T1), ok(T2)).reason
    # A newer interrupted attempt here isn't thrown away.
    assert "older copy" in _moved(ok(T1), Record(T2, T0, IN_PROGRESS)).reason


def test_home_not_registered_holds():
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 1000}, {"tv/X": "BK-6B"})
    data = drive_scan(tv={"X": {"e": 100}})
    (item,) = stale.classify(
        config=cfg,
        reg=reg,
        drive="BK-6A",
        contents=data,
        records={},
        nas=nas(tv={"X": {"e": 100}}),
        caches={},
    )
    assert item.action == stale.HOLD and "isn't registered" in item.reason


def _renamed(caches):
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 1000, "BK-6B": 1000}, {"tv/New Name": "BK-6B"})
    reg.homes["tv/Old Name"] = "BK-6A"  # gone on the NAS: renamed
    data = drive_scan(tv={"Old Name": {"e1": (100, 5), "deleted": (7, 5)}})
    nas_scan = nas(tv={"New Name": {"e1": (100, 5)}})
    (item,) = stale.classify(
        config=cfg,
        reg=reg,
        drive="BK-6A",
        contents=data,
        records={},
        nas=nas_scan,
        caches=caches,
    )
    assert item.kind == stale.GONE
    return item


def test_a_renamed_folder_counts_only_once_it_is_backed_up_somewhere():
    # The NAS doesn't count: it's what the backup is for.
    assert _renamed({}).only_files == 2
    synced = cache_of("bk-6b", drive_scan(tv={"New Name": {"e1": (100, 5)}}), {})
    item = _renamed({"BK-6B": synced})
    assert (item.only_files, item.only_bytes) == (1, 7)


def test_unexplained_data_is_never_pruned():
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 1000})
    data = drive_scan(tv={"Stray": {"e": 1}}, tv2={"X": {"e": 1}})
    data.loose["notes.txt"] = Entry(FILE, 3, 1)
    items = stale.classify(
        config=cfg,
        reg=reg,
        drive="BK-6A",
        contents=data,
        records={},
        nas=nas(tv={}),
        caches={},
    )
    assert {(i.path, i.action) for i in items} == {
        ("notes.txt", stale.UNEXPLAINED),
        ("tv2", stale.UNEXPLAINED),
        ("tv/Stray", stale.UNEXPLAINED),
    }


def test_restore_second_pass_takes_held_units_but_not_incomplete_ones():
    held = _moved(None, ok())
    interrupted = _moved(None, Record(T2, T1, IN_PROGRESS))
    assert stale.for_restore([held, interrupted]) == [held]


def test_units_copy_pending_on_a_drive_not_docked_for_long_are_listed():
    cfg = config(TV, RES)
    reg = registry({"BK-6A": 1000, "BK-6B": 1000}, {"tv/X": "BK-6B"})
    reg.drives["BK-6B"].last_seen = T0
    p = plan(
        cfg, reg, "BK-6A", 1000, nas(tv={"X": {"e": 1}}), now=T0 + timedelta(days=40)
    )
    assert p.pending_elsewhere == [("BK-6B", 1, T0)]
