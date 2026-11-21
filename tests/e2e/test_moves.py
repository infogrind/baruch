"""Moves and prune: copy first, then delete; nothing unexplained is deleted."""

import unicodedata

import pytest
from support import pin, spread

TV = "archive/tv"


DRIVES = {"BK-6A": "10%", "BK-6B": "10%"}


def setup(env, shows=None, capacity=100_000):
    for name, size in (shows or {"Lost": 20_000, "Wire": 10_000}).items():
        env.nas.add_show(TV, name, episodes=2, size=size)
    env.nas.mark(TV)
    env.config(rules=[spread("tv", TV, list(DRIVES))], drives=DRIVES)
    env.ok("init")
    a = env.register("BK-6A", capacity=100_000)
    return a, env.register("BK-6B", capacity=capacity)


def prune(env, drive, answers="y"):
    env.drives.dock(drive)
    try:
        return env.run("prune", drive.root.name, answers=answers)
    finally:
        env.drives.undock(drive)


# Copy first, then delete


def test_move_copies_first_and_only_prune_deletes_the_old_copy(env):
    a, b = setup(env)
    env.backup(a)
    env.ok("move", "set", "tv/Lost", "--to", "BK-6B")

    r = env.backup(a)  # before BK-6B has it: nothing is deleted
    assert a.exists("tv/Lost/S01E01.mkv")
    assert "held: tv/Lost: home BK-6B has no copy yet" in r.out
    r = prune(env, a)
    assert "kept: tv/Lost: home BK-6B has no copy yet" in r.out
    assert a.exists("tv/Lost/S01E01.mkv")

    r = env.backup(b)
    assert "tv/Lost: first sync" in r.out

    r = prune(env, a)
    assert "home BK-6B has a newer complete copy" in r.out
    assert "0 files only here" in r.out
    assert not a.exists("tv/Lost")
    assert a.exists("tv/Wire/S01E01.mkv")
    assert env.record(a, "tv/Lost") is None


def test_moving_back_never_trusts_an_older_copy(env):
    a, b = setup(env)
    env.backup(a)
    env.ok("move", "set", "tv/Lost", "--to", "BK-6B")
    env.backup(b)
    env.host.advance(days=1)
    env.nas.replace(f"{TV}/Lost/S01E01.mkv")
    env.ok("move", "set", "tv/Lost", "--to", "BK-6A")  # back, before BK-6A's run

    r = prune(env, b)

    assert "kept: tv/Lost: home BK-6A has an older copy" in r.out
    assert b.exists("tv/Lost/S01E01.mkv")


def test_prune_n_changes_nothing(env):
    a, b = setup(env)
    env.backup(a)
    env.ok("move", "set", "tv/Lost", "--to", "BK-6B")
    env.backup(b)

    r = prune(env, a, answers="n")

    assert "Nothing changed" in r.out
    assert a.exists("tv/Lost/S01E01.mkv")


def test_prune_rewrites_the_restore_script(env):
    a, b = setup(env)
    env.backup(a)
    env.ok("move", "set", "tv/Lost", "--to", "BK-6B")
    env.backup(b)
    before = a.path(".baruch/restore.sh").read_text()

    prune(env, a)

    after = a.path(".baruch/restore.sh").read_text()
    assert "tv/Lost" in before and "'tv/Lost'" not in after


def test_an_interrupted_prune_never_leaves_a_record_over_deleted_data(env, monkeypatch):
    a, b = setup(env)
    env.backup(a)
    env.ok("move", "set", "tv/Lost", "--to", "BK-6B")
    env.backup(b)
    import baruch.commands as commands

    def crash(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(commands, "remove_unit", crash)
    with pytest.raises(KeyboardInterrupt):
        prune(env, a)

    assert env.record(a, "tv/Lost") is None  # removed before the data


# move set


def test_move_set_refuses_drives_outside_the_list_and_unregistered_ones(env):
    a, _ = setup(env)
    env.backup(a)
    env.config(
        rules=[spread("tv", TV, ["BK-6A", "BK-6B", "BK-6C"])],
        drives={"BK-6A": "10%", "BK-6B": "10%", "BK-6C": "10%", "BK-Z": "10%"},
    )
    assert "isn't registered" in env.run("move", "set", "tv/Lost", "--to", "BK-6C").out
    env.register("BK-Z")
    assert "isn't in rule tv" in env.run("move", "set", "tv/Lost", "--to", "BK-Z").out


def test_move_set_refuses_the_current_home_and_gone_units(env):
    a, _ = setup(env)
    env.backup(a)
    assert "already BK-6A" in env.run("move", "set", "tv/Lost", "--to", "BK-6A").out
    env.nas.delete(f"{TV}/Wire")
    env.backup(a)
    assert "gone on the NAS" in env.run("move", "set", "tv/Wire", "--to", "BK-6B").out


def test_move_set_warns_if_the_unit_does_not_fit(env):
    a, _ = setup(env, {"Lost": 60_000, "Wire": 1000}, capacity=50_000)
    env.backup(a)

    r = env.ok("move", "set", "tv/Lost", "--to", "BK-6B")

    assert "may not fit on BK-6B" in r.out
    assert env.home("tv/Lost") == "BK-6B"  # a warning, not a refusal


def test_move_set_gives_an_unplaced_unit_a_home(env):
    a, b = setup(env, {"Huge": 95_000})
    env.backup(a)
    assert env.home("tv/Huge") is None

    env.ok("move", "set", "tv/Huge", "--to", "BK-6B")

    assert env.home("tv/Huge") == "BK-6B"


def test_unknown_unit_name_suggests_similar_ones(env):
    nfd = unicodedata.normalize("NFD", "Café")
    a, _ = setup(env, {nfd: 1000})
    env.backup(a)
    nfc = unicodedata.normalize("NFC", "tv/Café")
    if nfc in env.registry()["units"]:
        pytest.skip("this file system normalises names")

    r = env.run("move", "set", nfc, "--to", "BK-6B", answers="\n")

    assert "Similar" in r.out


# Config changes


def test_drive_removed_from_the_list_places_its_units_again(env):
    a, b = setup(env)
    env.backup(a)
    env.config(rules=[spread("tv", TV, ["BK-6B"])], drives=DRIVES)

    r = env.backup(b)

    assert "home BK-6A → BK-6B" in r.out
    assert b.exists("tv/Lost/S01E01.mkv")
    assert a.exists("tv/Lost/S01E01.mkv")  # stale until pruned


def test_removed_rule_becomes_unexplained_data_never_deleted(env):
    a, _ = setup(env)
    env.backup(a)
    env.nas.add_show("archive/movies", "Alien", episodes=1, size=100)
    env.nas.mark("archive/movies")
    env.config(
        rules=[pin("movies", "archive/movies", "BK-6A")],
        drives={"BK-6A": "10%", "BK-6B": "10%"},
    )

    r = env.backup(a)
    assert "rule tv removed: 3 units dropped" in r.out

    r = prune(env, a)
    assert "rule tv isn't in the config" in r.out
    assert "rm -rf --" in r.out and "/tv" in r.out
    assert a.exists("tv/Lost/S01E01.mkv")


def test_a_rule_added_back_finds_its_data_again(env):
    a, _ = setup(env)
    env.backup(a)
    env.config(rules=[], drives=DRIVES)
    env.backup(a)
    env.config(rules=[spread("tv", TV, ["BK-6B", "BK-6A"])], drives=DRIVES)

    r = env.backup(a)

    # BK-6B comes first now, but BK-6A already holds the data.
    assert env.home("tv/Lost") == "BK-6A"
    assert "first sync" not in r.out


# Gone units


def test_gone_unit_is_pruned_and_its_entry_removed(env):
    a, _ = setup(env)
    env.backup(a)
    env.nas.delete(f"{TV}/Wire")
    env.backup(a)

    r = prune(env, a)

    assert "gone on the NAS" in r.out and "2 files only here" in r.out
    assert "WARNING" in r.out and "exist on no other drive" in r.out
    assert not a.exists("tv/Wire")
    assert "tv/Wire" not in env.registry()["units"]


def test_prune_keeps_a_gone_unit_that_came_back(env, monkeypatch):
    a, _ = setup(env)
    env.backup(a)
    env.nas.delete(f"{TV}/Wire")
    import baruch.commands as commands

    real = commands.scan_nas

    def scan_then_restore(config):
        out = real(config)
        env.nas.add_show(TV, "Wire", episodes=1, size=10)  # back after the plan
        return out

    monkeypatch.setattr(commands, "scan_nas", scan_then_restore)
    r = prune(env, a)

    assert "back on the NAS, kept" in r.out
    assert a.exists("tv/Wire/S01E01.mkv")


def test_rename_shows_zero_files_only_here_once_the_new_folder_is_backed_up(env):
    a, _ = setup(env)
    env.backup(a)
    env.nas.rename(f"{TV}/Wire", f"{TV}/The Wire")
    env.backup(a)

    r = prune(env, a)

    assert "tv/Wire: gone on the NAS" in r.out and "0 files only here" in r.out


# Found with the sandbox


def test_a_newly_excluded_unit_names_the_drive_holding_its_data(env):
    a, _ = setup(env)
    env.backup(a)
    env.config(rules=[spread("tv", TV, list(DRIVES))], drives=DRIVES, exclude=["Lost"])

    r = env.backup(a)

    assert "tv/Lost is now excluded" in r.out
    assert "stays on BK-6A" in r.out


def test_prune_says_incomplete_when_the_homes_sync_didnt_finish(env, monkeypatch):
    a, b = setup(env)
    env.backup(a)
    env.ok("move", "set", "tv/Lost", "--to", "BK-6B")
    import baruch.execute as execute

    class Crash(BaseException):
        pass

    real = execute.run_rsync

    def copy_then_crash(*args, **kwargs):
        real(*args, **kwargs)
        raise Crash

    monkeypatch.setattr(execute, "run_rsync", copy_then_crash)
    with pytest.raises(Crash):
        env.backup(b)
    monkeypatch.undo()
    env.drives.undock_all()

    r = prune(env, a)

    assert "kept: tv/Lost: home BK-6B is incomplete" in r.out
    assert a.exists("tv/Lost/S01E01.mkv")


def test_prune_shows_where_else_the_files_are_and_when_that_drive_was_seen(env):
    a, b = setup(env)
    env.backup(a)
    env.ok("move", "set", "tv/Lost", "--to", "BK-6B")
    env.backup(b)
    env.host.advance(days=20)

    r = prune(env, a, answers="n")

    assert "tv/Lost: home BK-6B has a newer complete copy" in r.out
    assert "(also on the NAS; BK-6B, last seen 2026-11-14)" in r.out


def test_prune_says_when_a_unit_is_on_no_other_drive(env):
    a, _ = setup(env)
    env.backup(a)
    env.nas.delete(f"{TV}/Wire")
    env.backup(a)

    r = prune(env, a, answers="n")

    assert "(on no other drive)" in r.out
