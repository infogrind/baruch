"""Moves and config changes: copy first, then delete."""

from support import pin, spread

TV = "archive/tv"


def setup(env):
    env.nas.add_show(TV, "Lost", episodes=2, size=20_000)
    env.nas.add_show(TV, "Wire", episodes=2, size=10_000)
    env.nas.mark(TV)
    env.config(
        rules=[spread("tv", TV, ["BK-6A", "BK-6B"])],
        drives={"BK-6A": "10%", "BK-6B": "10%"},
    )
    env.ok("init")
    a = env.register("BK-6A", capacity=100_000)
    b = env.register("BK-6B", capacity=100_000)
    env.backup(a)
    return a, b


def test_move_copies_first_and_deletes_the_old_copy_later(env):
    a, b = setup(env)

    env.ok("move", "set", "tv/Lost", "--to", "BK-6B")
    assert env.unit("tv/Lost")["home"] == "BK-6B"

    # Old drive docked before the new one has a copy: nothing is deleted.
    r = env.backup(a)
    assert a.exists("tv/Lost/S01E01.mkv")
    assert "moved to" not in r.out

    r = env.backup(b)
    assert "tv/Lost" in r.out and "first copy" in r.out
    assert b.exists("tv/Lost/S01E01.mkv")

    r = env.backup(a)
    assert "moved to BK-6B, copy confirmed there on" in r.out
    assert "WARNING" not in r.out
    assert not a.exists("tv/Lost")
    assert "BK-6A" not in env.unit("tv/Lost")["copies"]


def test_move_set_refuses_pinned_units(env):
    env.nas.add_show("archive/movies", "Alien", episodes=1, size=1000)
    env.nas.mark("archive/movies")
    env.config(
        rules=[pin("movies", "archive/movies", "BK-20A")],
        drives={"BK-20A": "5%", "BK-6A": "10%"},
    )
    env.ok("init")
    drive = env.register("BK-20A")
    env.register("BK-6A")
    env.backup(drive)

    r = env.run("move", "set", "movies/Alien", "--to", "BK-6A")

    assert r.code != 0
    assert "pin" in r.out
    assert env.unit("movies/Alien")["home"] == "BK-20A"


def test_move_set_refuses_drives_outside_the_spread_list(env):
    setup(env)
    env.register("BK-X")

    r = env.run("move", "set", "tv/Lost", "--to", "BK-X")

    assert r.code != 0
    assert "spread list" in r.out


def test_move_set_refuses_unregistered_drives(env):
    env.nas.add_show(TV, "Lost", episodes=1, size=1000)
    env.nas.mark(TV)
    env.config(rules=[spread("tv", TV, ["BK-6A", "BK-6B"])], drives={"BK-6A": "10%"})
    env.ok("init")
    a = env.register("BK-6A")
    env.backup(a)

    r = env.run("move", "set", "tv/Lost", "--to", "BK-6B")

    assert r.code != 0
    assert "not registered" in r.out


def test_move_set_warns_if_the_unit_does_not_fit(env):
    env.nas.add_show(TV, "Lost", episodes=2, size=20_000)
    env.nas.mark(TV)
    env.config(
        rules=[spread("tv", TV, ["BK-6A", "BK-6B"])],
        drives={"BK-6A": "10%", "BK-6B": "10%"},
    )
    env.ok("init")
    a = env.register("BK-6A", capacity=100_000)
    env.register("BK-6B", capacity=15_000)
    env.backup(a)

    r = env.run("move", "set", "tv/Lost", "--to", "BK-6B")

    assert r.code == 0, r.out
    assert "doesn't fit" in r.out
    assert env.unit("tv/Lost")["home"] == "BK-6B"


def test_unknown_unit_name_suggests_similar_ones(env):
    setup(env)

    r = env.run("move", "set", "tv/lost", "--to", "BK-6B")

    assert r.code != 0
    assert "tv/Lost" in r.out


def test_pin_moved_to_another_drive_moves_the_units(env):
    env.nas.add_show("archive/movies", "Alien", episodes=2, size=2000)
    env.nas.mark("archive/movies")
    env.config(
        rules=[pin("movies", "archive/movies", "BK-20A")],
        drives={"BK-20A": "5%", "BK-12A": "5%"},
    )
    env.ok("init")
    old = env.register("BK-20A")
    new = env.register("BK-12A")
    env.backup(old)

    env.config(
        rules=[pin("movies", "archive/movies", "BK-12A")],
        drives={"BK-20A": "5%", "BK-12A": "5%"},
    )
    r = env.backup(old)
    assert old.exists("movies/Alien/S01E01.mkv")  # copy first

    env.backup(new)
    assert env.unit("movies/Alien")["home"] == "BK-12A"
    assert new.exists("movies/Alien/S01E01.mkv")

    r = env.backup(old)
    assert "copy confirmed there on" in r.out
    assert not old.exists("movies/Alien")


def test_drive_removed_from_a_spread_list_places_its_units_again(env):
    a, b = setup(env)
    env.config(
        rules=[spread("tv", TV, ["BK-6B"])],
        drives={"BK-6A": "10%", "BK-6B": "10%"},
    )

    env.backup(b)

    assert env.unit("tv/Lost")["home"] == "BK-6B"
    assert b.exists("tv/Lost/S01E01.mkv")
    assert a.exists("tv/Lost/S01E01.mkv")  # until it's docked again

    r = env.backup(a)
    assert not a.exists("tv/Lost")
    assert "copy confirmed there on" in r.out


def test_removed_rule_leaves_orphaned_data_that_only_prune_deletes(env):
    a, _ = setup(env)
    env.config(rules=[], drives={"BK-6A": "10%", "BK-6B": "10%"})

    r = env.backup(a)
    assert "orphaned" in r.out
    assert a.exists("tv/Lost/S01E01.mkv")

    env.drives.dock(a)
    r = env.run("prune", "BK-6A", answers="y")
    assert r.code == 0, r.out
    assert not a.exists("tv")
