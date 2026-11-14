"""Placement of spread units: preference order, closed drives, waiting."""

from support import spread

TV = "archive/tv"


def setup_tv(env, drives, shows, reserve="10%"):
    for name, size in shows.items():
        env.nas.add_show(TV, name, episodes=2, size=size)
    env.nas.mark(TV)
    env.config(rules=[spread("tv", TV, drives)], drives=dict.fromkeys(drives, reserve))
    env.ok("init")


def test_units_go_to_the_first_drive_in_the_list_largest_first(env):
    # BK-6A: 100_000 capacity, 10_000 reserve -> room for ~90_000.
    setup_tv(env, ["BK-6A", "BK-6B"], {"Big": 60_000, "Mid": 40_000, "Small": 20_000})
    a = env.register("BK-6A", capacity=100_000)
    env.register("BK-6B", capacity=100_000)

    r = env.backup(a)

    assert r.code == 0, r.out
    # Largest first: Big fits, Mid doesn't (60_000 + 40_000 > 90_000), Small fits.
    assert env.unit("tv/Big")["home"] == "BK-6A"
    assert env.unit("tv/Small")["home"] == "BK-6A"
    assert env.unit("tv/Mid")["home"] is None
    assert a.exists("tv/Big/S01E01.mkv") and not a.exists("tv/Mid")
    assert "tv/Mid" in r.out and "waiting for BK-6B" in r.out


def test_waiting_unit_goes_to_the_next_drive_when_it_is_docked(env):
    setup_tv(env, ["BK-6A", "BK-6B"], {"Big": 60_000, "Mid": 40_000, "Small": 20_000})
    a = env.register("BK-6A", capacity=100_000)
    b = env.register("BK-6B", capacity=100_000)
    env.backup(a)

    r = env.backup(b)

    assert r.code == 0, r.out
    assert env.unit("tv/Mid")["home"] == "BK-6B"
    assert b.exists("tv/Mid/S01E01.mkv")
    assert not b.exists("tv/Big")


def test_new_unit_waits_for_a_preferred_drive_that_is_not_docked(env):
    setup_tv(env, ["BK-6A", "BK-6B"], {"Show": 20_000})
    env.register("BK-6A", capacity=100_000)
    b = env.register("BK-6B", capacity=100_000)

    r = env.backup(b)

    assert env.unit("tv/Show")["home"] is None
    assert not b.exists("tv/Show")
    assert "waiting for BK-6A" in r.out
    assert 'baruch move set "tv/Show" --to BK-6B' in r.out


def test_drives_in_the_list_that_are_not_registered_are_skipped(env):
    setup_tv(env, ["BK-6A", "BK-6B"], {"Show": 20_000})
    b = env.register("BK-6B", capacity=100_000)

    env.backup(b)

    assert env.unit("tv/Show")["home"] == "BK-6B"
    assert b.exists("tv/Show/S01E01.mkv")


def test_a_unit_that_fits_nowhere_is_reported_and_never_split(env):
    setup_tv(env, ["BK-6A"], {"Huge": 200_000})
    a = env.register("BK-6A", capacity=100_000)

    r = env.backup(a)

    assert "tv/Huge" in r.out and "fits on no drive" in r.out
    assert not a.exists("tv/Huge")
    assert env.unit("tv/Huge")["home"] is None


def test_assignments_are_permanent_and_growth_lands_on_the_same_drive(env):
    setup_tv(env, ["BK-6A", "BK-6B"], {"Show": 20_000})
    a = env.register("BK-6A", capacity=100_000)
    env.register("BK-6B", capacity=100_000)
    env.backup(a)
    env.nas.add(f"{TV}/Show/S02E01.mkv", size=10_000)

    env.backup(a)

    assert env.unit("tv/Show")["home"] == "BK-6A"
    assert a.exists("tv/Show/S02E01.mkv")


def test_registered_drive_without_config_entry_takes_no_units(env):
    setup_tv(env, ["BK-6A"], {"Show": 20_000})
    other = env.register("BK-X", capacity=100_000)

    r = env.backup(other)

    assert r.code == 0, r.out
    assert other.files() == set()


def test_growth_into_the_reserve_is_reported_with_a_move_suggestion(env):
    setup_tv(env, ["BK-6A", "BK-6B"], {"Old": 40_000, "New": 40_000})
    a = env.register("BK-6A", capacity=100_000)
    env.register("BK-6B", capacity=100_000)
    env.backup(a)
    # ~83_000 used, reserve 10_000: growing by 15_000 eats into the reserve.
    env.nas.add(f"{TV}/New/S02E01.mkv", size=15_000, version=5)

    r = env.backup(a)

    assert a.exists("tv/New/S02E01.mkv")
    assert "of the reserve used" in r.out
    assert 'baruch move set "tv/Old" --to BK-6B' in r.out
