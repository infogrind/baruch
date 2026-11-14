"""Logic tests: configuration + registry + listings in, plan out."""

from datetime import UTC, date, datetime
from pathlib import Path

from baruch.config import Config, DriveConf, Rule, Settings
from baruch.planner import (
    CLEANUP,
    FIRST,
    G_NEW,
    G_PINNED,
    G_SPREAD,
    GONE,
    SYNC,
    UNTRACKED,
    make_plan,
)
from baruch.registry import OK, Copy, Drive, Registry, Unit
from baruch.scan import DIR, FILE, FILES_UNIT, Entry, TreeScan, UnitScan

T0 = datetime(2026, 11, 14, tzinfo=UTC)


def unit(name: str, files: dict[str, int], mtime: int = 100) -> UnitScan:
    u = UnitScan(name)
    u.entries[name] = Entry(DIR)
    for i, (rel, size) in enumerate(files.items()):
        u.entries[f"{name}/{rel}"] = Entry(
            FILE, size, mtime, inode=(1, hash((name, i)))
        )
    return u


def tree(*units: UnitScan) -> TreeScan:
    t = TreeScan()
    for u in units:
        t.units[u.name] = u
    t.units.setdefault(FILES_UNIT, UnitScan(FILES_UNIT))
    return t


def config(rules, drives=None, **settings) -> Config:
    drives = drives or {}
    return Config(
        Settings(**settings),
        {n: DriveConf(n, reserve_share=s) for n, s in drives.items()},
        rules,
    )


def tv(*drives):
    return Rule("tv", Path("/nas/tv"), spread=list(drives))


def plan(cfg, reg, drive, nas, disk=None, capacity=1000, free=1000, **kw):
    disk = disk or {}
    return make_plan(
        config=cfg,
        reg=reg,
        drive=drive,
        capacity=capacity,
        free=free,
        nas=nas,
        disk=disk,
        root_names=sorted(disk),
        **kw,
    )


def reg_with(*drives, **units) -> Registry:
    r = Registry()
    for d in drives:
        r.drives[d] = Drive("id-" + d, 1000, 1000)
    r.units.update(units)
    return r


def ops(p):
    return {o.unit: o for o in p.ops}


def test_new_units_largest_first_until_closed():
    cfg = config([tv("A", "B")], {"A": 0.1, "B": 0.1})
    nas = {
        "tv": tree(unit("S", {"e": 200}), unit("L", {"e": 600}), unit("M", {"e": 300}))
    }
    p = plan(cfg, reg_with("A", "B"), "A", nas)
    # 1000 free, reserve 100: L (600) fits, M (300) fits exactly (100 left), S doesn't.
    assert set(ops(p)) == {"tv/L", "tv/M"}
    assert [w.unit for w in p.waiting] == ["tv/S"]
    assert p.waiting[0].drive == "B"


def test_a_lower_drive_takes_a_unit_only_if_every_drive_above_is_closed():
    cfg = config([tv("A", "B")], {"A": 0.1, "B": 0.1})
    nas = {"tv": tree(unit("S", {"e": 200}))}
    reg = reg_with("A", "B")
    p = plan(cfg, reg, "B", nas)
    assert not p.ops and p.waiting[0].drive == "A" and p.waiting[0].here

    reg.drives["A"].free = 250  # 250 - 100 reserve < 200: closed for S
    p = plan(cfg, reg, "B", nas)
    assert ops(p)["tv/S"].kind == FIRST
    assert p.new_units["tv/S"] == "B"


def test_a_unit_that_fits_on_no_drive_is_reported():
    cfg = config([tv("A")], {"A": 0.1})
    p = plan(cfg, reg_with("A"), "A", {"tv": tree(unit("X", {"e": 5000}))})
    assert p.nowhere == [("tv/X", 5000)]


def test_existing_units_may_grow_into_the_reserve_but_new_ones_may_not():
    cfg = config([tv("A")], {"A": 0.1})
    reg = reg_with(
        "A", **{"tv/Old": Unit("A", date(2026, 1, 1), {"A": Copy(T0, OK, 100)})}
    )
    nas = {"tv": tree(unit("Old", {"e": 100, "f": 50}), unit("New", {"e": 10}))}
    disk = {"tv": tree(unit("Old", {"e": 100}))}
    p = plan(cfg, reg, "A", nas, disk, free=60)
    assert ops(p)["tv/Old"].kind == SYNC  # grows by 50, into the reserve
    assert "tv/New" not in ops(p)
    assert p.reserve_used == 100 - 10


def test_pinned_units_are_synced_before_spread_units():
    pin = Rule("movies", Path("/nas/movies"), pin="A")
    cfg = config([tv("A"), pin], {"A": 0.0})
    reg = reg_with(
        "A",
        **{
            "tv/T": Unit("A", date(2026, 1, 1), {"A": Copy(T0, OK, 10)}),
            "movies/M": Unit("A", date(2026, 1, 1), {"A": Copy(T0, OK, 10)}),
        },
    )
    nas = {"tv": tree(unit("T", {"e": 20})), "movies": tree(unit("M", {"e": 20}))}
    disk = {"tv": tree(unit("T", {"e": 10})), "movies": tree(unit("M", {"e": 10}))}
    p = plan(cfg, reg, "A", nas, disk)
    assert [o.unit for o in p.ops] == ["movies/M", "tv/T"]
    assert [o.group for o in p.ops] == [G_PINNED, G_SPREAD]


def test_untracked_copy_fit_uses_net_bytes():
    cfg = config([tv("A")], {"A": 0.1})
    nas = {"tv": tree(unit("Big", {"e": 800, "f": 10}))}
    disk = {"tv": tree(unit("Big", {"e": 800}))}
    p = plan(cfg, reg_with("A"), "A", nas, disk, free=150)
    op = ops(p)["tv/Big"]
    assert op.kind == UNTRACKED and op.group == G_NEW
    assert op.new == ["Big/f"]


def test_cleanup_waits_for_a_confirmed_copy_on_the_new_home():
    cfg = config([tv("A", "B")], {"A": 0.1, "B": 0.1})
    u = Unit("B", date(2026, 1, 1), {"A": Copy(T0, OK, 100)})
    nas = {"tv": tree(unit("X", {"e": 100}))}
    disk = {"tv": tree(unit("X", {"e": 100}))}
    p = plan(cfg, reg_with("A", "B", **{"tv/X": u}), "A", nas, disk)
    assert not p.ops

    u.copies["B"] = Copy(T0, OK, 100)
    p = plan(cfg, reg_with("A", "B", **{"tv/X": u}), "A", nas, disk)
    assert ops(p)["tv/X"].kind == CLEANUP
    assert "moved to B" in ops(p)["tv/X"].note
    assert not p.warnings


def test_copy_with_errors_is_not_confirmed():
    cfg = config([tv("A", "B")], {"A": 0.1, "B": 0.1})
    u = Unit(
        "B", date(2026, 1, 1), {"A": Copy(T0, OK, 100), "B": Copy(T0, "errors", 100)}
    )
    nas = {"tv": tree(unit("X", {"e": 100}))}
    disk = {"tv": tree(unit("X", {"e": 100}))}
    p = plan(cfg, reg_with("A", "B", **{"tv/X": u}), "A", nas, disk)
    assert not p.ops


def test_gone_unit_is_deleted_and_warned_about():
    cfg = config([tv("A")], {"A": 0.1})
    reg = reg_with(
        "A", **{"tv/X": Unit("A", date(2026, 1, 1), {"A": Copy(T0, OK, 100)})}
    )
    disk = {"tv": tree(unit("X", {"e": 100}))}
    p = plan(cfg, reg, "A", {"tv": tree()}, disk)
    assert ops(p)["tv/X"].kind == GONE
    assert any("tv/X" in w for w in p.warnings)


def test_scan_error_blocks_deletions_under_it():
    cfg = config([tv("A")], {"A": 0.1})
    reg = reg_with(
        "A", **{"tv/X": Unit("A", date(2026, 1, 1), {"A": Copy(T0, OK, 100)})}
    )
    nas_x = unit("X", {"e": 100})
    nas_x.errors.append(("X/Extras", "Permission denied"))
    disk = {"tv": tree(unit("X", {"e": 100, "Extras/m": 50}))}
    p = plan(cfg, reg, "A", {"tv": tree(nas_x)}, disk)
    assert "tv/X" not in ops(p)
    assert ("tv/X/Extras", "Permission denied") in p.scan_errors


def test_thresholds_are_shares_of_the_drive_before_the_run():
    cfg = config([tv("A")], {"A": 0.0})
    files = {f"e{i}": 10 for i in range(10)}
    reg = reg_with(
        "A", **{"tv/X": Unit("A", date(2026, 1, 1), {"A": Copy(T0, OK, 100)})}
    )
    disk = {"tv": tree(unit("X", files))}

    one_gone = dict(files)
    del one_gone["e0"]
    p = plan(cfg, reg, "A", {"tv": tree(unit("X", one_gone))}, disk)
    assert not p.warnings  # exactly 10%: not more than

    two_gone = dict(one_gone)
    del two_gone["e1"]
    p = plan(cfg, reg, "A", {"tv": tree(unit("X", two_gone))}, disk)
    assert any("files" in w for w in p.warnings)
    assert any("bytes" not in w and "of" in w for w in p.warnings)


def test_skip_leaves_a_unit_out():
    cfg = config([tv("A")], {"A": 0.0})
    reg = reg_with(
        "A", **{"tv/X": Unit("A", date(2026, 1, 1), {"A": Copy(T0, OK, 100)})}
    )
    nas = {"tv": tree(unit("X", {"e": 200}))}
    disk = {"tv": tree(unit("X", {"e": 100}))}
    p = plan(cfg, reg, "A", nas, disk, skip={"tv/X"})
    assert not p.ops
    assert ("tv/X", "skipped on request") in p.skipped
    assert p.record_skipped == ["tv/X"]


def test_missing_marker_skips_the_rule_entirely():
    cfg = config([tv("A")], {"A": 0.0})
    reg = reg_with(
        "A", **{"tv/X": Unit("A", date(2026, 1, 1), {"A": Copy(T0, OK, 100)})}
    )
    disk = {"tv": tree(unit("X", {"e": 100}))}
    p = plan(cfg, reg, "A", {"tv": None}, disk)
    assert not p.ops and not p.orphans and not p.gone
    assert [r.name for r in p.markers_missing] == ["tv"]


def test_folders_of_unknown_rules_are_orphaned():
    cfg = config([tv("A")], {"A": 0.0})
    disk = {"tv": tree(), "old": tree(unit("Y", {"e": 5}))}
    p = plan(cfg, reg_with("A"), "A", {"tv": tree()}, disk)
    assert [o.path for o in p.orphans] == ["old"]


def test_home_outside_the_spread_list_is_placed_again():
    cfg = config([tv("B")], {"A": 0.1, "B": 0.1})
    u = Unit("A", date(2026, 1, 1), {"A": Copy(T0, OK, 100)})
    p = plan(
        cfg, reg_with("A", "B", **{"tv/X": u}), "B", {"tv": tree(unit("X", {"e": 100}))}
    )
    assert p.home_changes["tv/X"] == "B"
    assert ops(p)["tv/X"].kind == FIRST
