"""Data already on a drive without records: the switch-over from an old backup."""

import shutil

from support import pin, spread

TV = "archive/tv"
MOVIES = "archive/movies"


def setup(env):
    env.nas.add_show(MOVIES, "Alien", episodes=2, size=2000)
    env.nas.add_show(MOVIES, "Heat", episodes=2, size=2000)
    env.nas.mark(MOVIES)
    env.nas.add_show(TV, "Lost", episodes=2, size=2000)
    env.nas.add_show(TV, "Wire", episodes=2, size=2000)
    env.nas.mark(TV)
    env.config(
        rules=[
            pin("movies", MOVIES, "BK-20A"),
            spread("tv", TV, ["BK-6A", "BK-20A"]),
        ],
        drives={"BK-20A": "5%", "BK-6A": "10%"},
    )
    env.ok("init")


def mirror(env, disk):
    """An old plain mirror, already moved into the /<rule>/ layout."""
    shutil.copytree(env.nas.path(MOVIES), disk.path("movies"), symlinks=True)
    shutil.copytree(env.nas.path(TV), disk.path("tv"), symlinks=True)


def test_untracked_copy_on_its_home_drive_is_listed_file_by_file(env):
    setup(env)
    old = env.drives.new("old-mirror", capacity=200_000)
    mirror(env, old)
    old.path("movies/Alien/stale.mkv").write_bytes(b"x" * 10)
    env.nas.add(f"{MOVIES}/Alien/new.mkv", size=100)
    env.drives.dock(old)
    env.ok("drive", "register", "BK-20A")

    r = env.run("run", answers="y")

    assert r.code == 0, r.out
    assert "existing folder, no record yet" in r.out
    assert "- stale.mkv" in r.out
    assert "+ new.mkv" in r.out
    assert "first sync" not in r.out.split("movies/Heat")[1].split("\n")[0]
    assert not old.exists("movies/Alien/stale.mkv")
    assert old.exists("movies/Alien/new.mkv")
    assert env.record(old, "movies/Alien").result == "ok"


def test_untracked_copy_counts_towards_the_warnings(env):
    setup(env)
    old = env.drives.new("old-mirror", capacity=200_000)
    mirror(env, old)
    for i in range(20):
        old.path(f"movies/Alien/junk{i}.bin").write_bytes(b"x" * 100)
    env.drives.dock(old)
    env.ok("drive", "register", "BK-20A")

    r = env.run("run", answers="n")

    assert any(line.startswith("WARNING") for line in r.lines)


def test_data_of_units_homed_elsewhere_is_stale_until_prune(env):
    setup(env)
    a = env.register("BK-6A", capacity=100_000)
    env.backup(a)  # tv shows now live on BK-6A
    old = env.drives.new("old-mirror", capacity=200_000)
    mirror(env, old)
    env.drives.dock(old)
    env.ok("drive", "register", "BK-20A")

    r = env.run("run", answers="y")

    assert "Stale data on BK-20A" in r.out and "can be pruned" in r.out
    assert old.exists("tv/Lost/S01E01.mkv")

    r = env.run("prune", "BK-20A", answers="y")
    assert r.code == 0, r.out
    assert "home BK-6A has a newer complete copy" in r.out
    assert not old.exists("tv/Lost")
    assert old.exists("movies/Alien/S01E01.mkv")


def test_units_whose_data_is_already_there_get_that_drive_as_home(env):
    setup(env)
    env.register("BK-6A", capacity=100_000)
    old = env.drives.new("old-mirror", capacity=200_000)
    mirror(env, old)
    env.drives.dock(old)
    env.ok("drive", "register", "BK-20A")
    env.ok("run", answers="y")

    # BK-6A comes first in tv's list, but BK-20A already holds the shows.
    assert env.home("tv/Lost") == "BK-20A"
    r = env.run("prune", "BK-20A", answers="y")
    assert r.code == 0, r.out
    assert "Nothing to prune" in r.out
    assert old.exists("tv/Lost/S01E01.mkv")


def test_prune_asks_and_n_changes_nothing(env):
    setup(env)
    a = env.register("BK-6A", capacity=100_000)
    env.backup(a)
    old = env.drives.new("old-mirror", capacity=200_000)
    mirror(env, old)
    old.path("stray-folder").mkdir()
    old.path("stray-folder/notes.txt").write_text("left over")
    env.drives.dock(old)
    env.ok("drive", "register", "BK-20A")

    r = env.run("prune", "BK-20A", answers="n")

    assert "stray-folder" in r.out
    assert old.exists("stray-folder")
    assert old.exists("tv/Lost")
