"""Placement: which drive a unit belongs on, and growth into the reserve."""

from support import pin, spread

TV = "archive/tv"


def setup(env, shows, drives=("BK-6A", "BK-6B"), reserve="10%"):
    for name, size in shows.items():
        env.nas.add_show(TV, name, episodes=1, size=size)
    env.nas.mark(TV)
    env.config(
        rules=[spread("tv", TV, list(drives))], drives={d: reserve for d in drives}
    )
    env.ok("init")


def test_units_go_to_the_first_drive_with_room_largest_first(env):
    setup(env, {"Big": 60_000, "Mid": 30_000, "Small": 5_000})
    a = env.register("BK-6A", capacity=100_000)
    env.register("BK-6B", capacity=100_000)

    r = env.backup(a)

    assert r.code == 0, r.out
    # Big fits on BK-6A; Mid doesn't any more (reserve), so BK-6B; Small fits.
    assert env.home("tv/Big") == "BK-6A"
    assert env.home("tv/Mid") == "BK-6B"
    assert env.home("tv/Small") == "BK-6A"
    assert "tv/Mid: new unit, home BK-6B" in r.out
    assert not a.exists("tv/Mid")


def test_a_unit_homed_on_another_drive_is_copied_when_that_drive_is_docked(env):
    setup(env, {"Big": 60_000, "Mid": 30_000})
    a = env.register("BK-6A", capacity=100_000)
    b = env.register("BK-6B", capacity=100_000)
    env.backup(a)

    r = env.backup(b)

    assert "tv/Mid: first sync" in r.out
    assert b.exists("tv/Mid/S01E01.mkv")


def test_drives_in_the_list_that_are_not_registered_are_skipped(env):
    setup(env, {"A": 10_000})
    b = env.register("BK-6B")

    env.backup(b)

    assert env.home("tv/A") == "BK-6B"


def test_a_unit_that_fits_nowhere_is_unplaced_and_never_split(env):
    setup(env, {"Huge": 200_000})
    a = env.register("BK-6A", capacity=100_000)
    env.register("BK-6B", capacity=100_000)

    r = env.backup(a)

    assert "Unplaced" in r.out and "tv/Huge" in r.out
    assert env.home("tv/Huge") is None
    assert not a.exists("tv/Huge")
    assert "tv/Huge: unplaced" in env.ok("status").out


def test_homes_are_permanent_and_growth_lands_on_the_same_drive(env):
    setup(env, {"A": 20_000})
    a = env.register("BK-6A", capacity=100_000)
    env.register("BK-6B", capacity=100_000)
    env.backup(a)
    env.nas.add(f"{TV}/A/S02E01.mkv", size=30_000)

    env.backup(a)

    assert env.home("tv/A") == "BK-6A"
    assert a.exists("tv/A/S02E01.mkv")


def test_growth_into_the_reserve_shows_the_largest_units_and_the_command(env):
    setup(env, {"Lost": 40_000, "Wire": 40_000})
    a = env.register("BK-6A", capacity=100_000)
    env.register("BK-6B", capacity=100_000)
    env.backup(a)
    env.nas.add(f"{TV}/Lost/S02E01.mkv", size=6_000)  # fits, but into the reserve

    r = env.backup(a)

    assert "grown into the reserve" in r.out
    assert "Largest units on BK-6A" in r.out and "tv/Lost" in r.out
    assert "Room above reserve in rule tv: BK-6B" in r.out
    assert "baruch move set UNIT --to DRIVE" in r.out


def test_registered_drive_without_config_entry_takes_no_units(env):
    env.nas.add_show("archive/movies", "Alien", episodes=1, size=1000)
    env.nas.mark("archive/movies")
    env.config(
        rules=[pin("movies", "archive/movies", "BK-20A")], drives={"BK-20A": "5%"}
    )
    env.ok("init")
    other = env.register("BK-X")

    r = env.backup(other)

    assert r.code == 0, r.out
    assert not other.exists("movies")
    assert env.home("movies/Alien") is None


def test_units_copy_pending_on_a_drive_not_docked_for_long_are_shown(env):
    setup(env, {"Big": 60_000, "Mid": 30_000})
    a = env.register("BK-6A", capacity=100_000)
    env.register("BK-6B", capacity=100_000)
    env.backup(a)
    env.host.advance(days=40)
    env.nas.add(f"{TV}/Big/S02E01.mkv", size=100)

    r = env.backup(a)

    assert "1 unit copy pending on BK-6B" in r.out
    assert "baruch move set" in r.out
