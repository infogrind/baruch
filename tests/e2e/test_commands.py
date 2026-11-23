"""status, find, config check, init, verify, registry restore."""

import os

from support import not_root, pin, spread

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


# init and the registry


def test_commands_refuse_without_a_registry_and_name_init(env):
    setup(env)
    (env.state_dir / "registry.yaml").unlink()

    r = env.run("status")

    assert r.code != 0
    assert "baruch init" in r.out and "saved version" in r.out


def test_init_refuses_to_overwrite_an_existing_registry(env):
    setup(env)

    r = env.run("init")

    assert r.code != 0
    assert "already" in r.out


def test_a_version_is_kept_before_each_change(env):
    setup(env)
    a = env.register("BK-6A")
    env.backup(a)

    versions = list((env.state_dir / "versions").iterdir())
    assert len(versions) >= 2


# status


def test_status_lists_drives_and_units_that_need_attention(env):
    setup(env)
    a = env.register("BK-6A")
    env.register("BK-6B")
    env.backup(a)
    env.ok("move", "set", "tv/Lost", "--to", "BK-6B")

    r = env.ok("status")

    assert "BK-6A" in r.out and "BK-6B" in r.out
    assert "tv/Lost" in r.out and "copy pending" in r.out
    assert "Last scan" in r.out


def test_status_flags_drives_not_docked_for_too_long(env):
    setup(env)
    a = env.register("BK-6A")
    env.backup(a)
    env.host.advance(days=40)

    r = env.ok("status")

    assert "not docked for" in r.out


def test_status_lists_units_with_no_backup(env):
    setup(env)
    a = env.register("BK-6A")
    env.backup(a)
    env.ok("drive", "forget", "BK-6A", answers="y")

    r = env.ok("status")

    assert "no backup" in r.out and "tv/Lost" in r.out


# find


def test_find_shows_which_drive_holds_matching_files(env):
    setup(env)
    a = env.register("BK-6A")
    env.backup(a)

    r = env.ok("find", "S01E02")

    assert "tv/Lost/S01E02.mkv" in r.out and "BK-6A" in r.out


def test_find_ignores_forgotten_drives(env):
    setup(env)
    a = env.register("BK-6A")
    env.backup(a)
    env.ok("drive", "forget", "BK-6A", answers="y")

    r = env.ok("find", "S01E02")

    assert "on no drive" in r.out


# config check


def test_config_check_accepts_a_good_configuration(env):
    setup(env)

    r = env.run("config", "check")

    assert r.code == 0, r.out


def test_config_check_reports_a_missing_marker(env):
    setup(env)
    env.nas.remove_marker(TV)

    r = env.run("config", "check")

    assert r.code != 0
    assert ".backup-source" in r.out


def test_config_check_refuses_overlapping_sources(env):
    env.nas.mark("archive")
    env.nas.mark(TV)
    env.config(
        rules=[pin("archive", "archive", "BK-20A"), spread("tv", TV, ["BK-20A"])],
        drives={"BK-20A": "5%"},
    )

    r = env.run("config", "check")

    assert r.code != 0
    assert "inside" in r.out


def test_config_check_reports_drives_missing_from_drives(env):
    env.nas.mark(TV)
    env.config(rules=[spread("tv", TV, ["BK-6A", "BK-X"])], drives={"BK-6A": "10%"})

    r = env.run("config", "check")

    assert r.code != 0
    assert "BK-X" in r.out


def test_config_check_refuses_bad_rule_names(env):
    env.nas.mark(TV)
    env.config(rules=[spread("t v", TV, ["BK-6A"])], drives={"BK-6A": "10%"})

    r = env.run("config", "check")

    assert r.code != 0
    assert "t v" in r.out


# verify


def test_verify_reports_content_that_differs_with_the_same_size_and_time(env):
    setup(env)
    a = env.register("BK-6A")
    env.backup(a)
    env.nas.replace(f"{TV}/Lost/S01E01.mkv", keep_mtime=True)
    env.drives.dock(a)

    r = env.run("verify", answers="")

    assert r.code == 0, r.out
    assert "Lost/S01E01.mkv" in r.out and "unknown which side is right" in r.out
    assert "rsync" in r.out  # commands for both directions
    assert env.registry()["drives"]["BK-6A"]["last_verify"]


def test_verify_says_how_much_it_compared(env, monkeypatch):
    from baruch.commands import Out

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(Out, "STATUS_EVERY", 0)  # every update
    setup(env)
    a = env.register("BK-6A")
    env.backup(a)
    env.drives.dock(a)

    r = env.run("verify", answers="")
    shown = env.run("verify", answers="", tty=True)

    assert "Verified 3 units, 5 files, 29.3 KiB" in r.lines  # with the marker
    assert r.out.index("Scanned tv") < r.out.index("Verified")
    assert "Verifying [2/3] tv/Lost" in shown.out  # the status line
    assert "29.3 KiB of 29.3 KiB" in shown.out


def test_verify_reports_files_changed_on_the_nas(env):
    setup(env)
    a = env.register("BK-6A")
    env.backup(a)
    env.nas.replace(f"{TV}/Wire/S01E01.mkv")
    env.drives.dock(a)

    r = env.run("verify", answers="")

    assert "changed on the NAS" in r.out and "Wire/S01E01.mkv" in r.out


@not_root
def test_verify_offers_to_copy_unreadable_files_again(env):
    setup(env)
    a = env.register("BK-6A")
    env.backup(a)
    before = env.record(a, "tv/Wire")
    os.chmod(a.path("tv/Wire/S01E01.mkv"), 0)
    env.drives.dock(a)
    env.host.advance(days=1)

    r = env.run("verify", answers="y")

    assert r.code == 0, r.out
    assert "copy these 1 unreadable files again from the NAS" in r.out
    assert a.read("tv/Wire/S01E01.mkv") == env.nas.read(f"{TV}/Wire/S01E01.mkv")
    # A re-copy isn't a sync: `synced` and the result stay as they were.
    after = env.record(a, "tv/Wire")
    assert (after.synced, after.result) == (before.synced, before.result)


def test_init_writes_a_commented_starter_config(env):
    r = env.run("init")

    assert r.code == 0, r.out
    text = (env.config_dir / "config.yaml").read_text()
    assert "# " in text and "drives: [BK-20A]" in text and "pin" not in text
    assert "config.yaml" in r.out
    r = env.run("config", "check")
    assert r.code == 0, r.out
    assert "0 rules" in r.out


def test_init_keeps_an_existing_config(env):
    env.config(rules=[], drives={"BK-6A": "10%"})
    before = (env.config_dir / "config.yaml").read_text()

    r = env.run("init")

    assert r.code == 0, r.out
    assert (env.config_dir / "config.yaml").read_text() == before
    assert "kept" in r.out.lower()
