"""Safety: markers, scan errors, gone units, the mount check."""

import os

from support import not_root, pin, spread

TV = "archive/tv"


def setup(env):
    env.nas.add_show(TV, "Lost", episodes=4, size=20_000)
    env.nas.add_show(TV, "Wire", episodes=2, size=10_000)
    env.nas.mark(TV)
    env.config(rules=[spread("tv", TV, ["BK-6A"])], drives={"BK-6A": "10%"})
    env.ok("init")
    a = env.register("BK-6A", capacity=100_000)
    env.backup(a)
    return a


def test_missing_marker_skips_the_rule_and_touches_nothing(env):
    a = setup(env)
    before = a.files()
    env.nas.delete(TV)  # e.g. the volume isn't mounted
    env.nas.mkdir(TV)

    r = env.backup(a)

    assert "marker" in r.out and "tv" in r.out
    assert a.files() == before
    assert "gone" not in r.out.lower()


def test_gone_unit_is_kept_by_runs_and_reported(env):
    a = setup(env)
    env.nas.delete(f"{TV}/Wire")

    r = env.backup(a)

    assert "1 units of rule tv gone on the NAS" in r.out
    assert "can be pruned" in r.out
    assert a.exists("tv/Wire/S01E01.mkv")  # only prune removes it
    assert "tv/Wire" in env.registry()["units"]


def test_emptied_folder_whose_marker_survives_triggers_the_unit_loss_warning(env):
    a = setup(env)
    for i in (1, 2, 3):
        env.nas.delete(f"{TV}/Lost/S01E{i:02d}.mkv")

    r = env.backup(a, answers="n")

    assert any(line.startswith("WARNING") and "tv/Lost" in line for line in r.lines)


def test_new_files_give_no_warning(env):
    a = setup(env)
    env.nas.add(f"{TV}/Wire/S02E01.mkv", size=30_000)

    r = env.backup(a, answers="n")

    assert "WARNING" not in r.out


@not_root
def test_unreadable_folder_on_the_nas_plans_no_deletions_under_it(env):
    a = setup(env)
    env.nas.add(f"{TV}/Lost/Extras/making-of.mkv", size=1000)
    env.backup(a)
    os.chmod(env.nas.path(f"{TV}/Lost/Extras"), 0)

    r = env.backup(a)

    assert "Lost/Extras" in r.out and "can't be read" in r.out
    assert a.exists("tv/Lost/Extras/making-of.mkv")


@not_root
def test_unreadable_unit_folder_is_unreadable_not_gone(env):
    a = setup(env)
    os.chmod(env.nas.path(f"{TV}/Wire"), 0)

    r = env.backup(a)

    assert "tv/Wire" in r.out and "unreadable" in r.out
    assert a.exists("tv/Wire/S01E01.mkv")
    assert "tv/Wire" in env.registry()["units"]


@not_root
def test_unreadable_file_marks_the_unit_synced_with_errors(env):
    env.nas.add_show("archive/movies", "Alien", episodes=2, size=2000)
    env.nas.mark("archive/movies")
    env.config(
        rules=[pin("movies", "archive/movies", "BK-20A")], drives={"BK-20A": "5%"}
    )
    env.ok("init")
    drive = env.register("BK-20A")
    os.chmod(env.nas.path("archive/movies/Alien/S01E02.mkv"), 0)

    r = env.backup(drive)

    assert r.code == 0, r.out
    assert "S01E02.mkv" in r.out
    assert env.record(drive, "movies/Alien").result == "errors"
    assert drive.exists("movies/Alien/S01E01.mkv")


def test_drive_disappearing_mid_run_stops_and_records_nothing_for_that_unit(env):
    env.nas.add_show(TV, "A", episodes=1, size=1000)
    env.nas.add_show(TV, "B", episodes=1, size=2000)
    env.nas.mark(TV)
    env.config(rules=[spread("tv", TV, ["BK-6A"])], drives={"BK-6A": "10%"})
    env.ok("init")
    a = env.register("BK-6A")
    env.drives.dock(a)

    real_is_mount = env.host.is_mount
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        # The first checks pass (discovery, first unit); then the drive is gone.
        return real_is_mount(path) and calls["n"] <= 2

    env.host.is_mount = flaky
    r = env.run("run", answers="y")

    assert r.code != 0
    assert "no longer mounted" in r.out
    recs = env.records(a)
    assert [u for u, r in recs.items() if r.result == "ok" and u != "tv/(files)"] in (
        [],
        ["tv/B"],
    )


def test_renamed_show_is_a_gone_unit_plus_a_new_one(env):
    a = setup(env)
    env.nas.rename(f"{TV}/Wire", f"{TV}/The Wire")

    r = env.backup(a)

    assert "tv/The Wire" in r.out and "gone on the NAS" in r.out
    assert a.exists("tv/The Wire/S01E01.mkv")
    # The old folder stays until prune, so there's no moment without a copy.
    assert a.exists("tv/Wire/S01E01.mkv")
