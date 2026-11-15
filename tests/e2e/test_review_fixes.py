"""Regression tests for the findings of the first code review."""

import os
import shutil
import subprocess

import pytest
from support import not_root, pin, spread

import baruch.execute as execute
import baruch.scan as scan

TV = "archive/tv"


def setup(env, shows=None, drives=("BK-6A",)):
    for name, size in (shows or {"Lost": 20_000, "Wire": 10_000}).items():
        env.nas.add_show(TV, name, episodes=2, size=size)
    env.nas.mark(TV)
    env.config(
        rules=[spread("tv", TV, list(drives))], drives=dict.fromkeys(drives, "10%")
    )
    env.ok("init")
    return [env.register(d) for d in drives]


def failing_stat(monkeypatch, *bad_names):
    """lstat of these names fails with an I/O error during the NAS scan."""
    real = scan._lstat

    def fake(item):
        if item.name in bad_names:
            raise OSError(5, "Input/output error")
        return real(item)

    monkeypatch.setattr(scan, "_lstat", fake)


# H1: scan errors at the top of a source never become deletions


def test_stat_error_on_a_unit_folder_is_unreadable_not_gone(env, monkeypatch):
    (a,) = setup(env)
    env.backup(a)
    failing_stat(monkeypatch, "Wire")

    r = env.backup(a)

    assert "gone" not in r.out.lower()
    assert "tv/Wire" in r.out and "unreadable" in r.out
    assert a.exists("tv/Wire/S01E01.mkv")
    assert "tv/Wire" in env.registry()["units"]


def test_stat_error_on_a_loose_file_keeps_it_on_the_drive(env, monkeypatch):
    env.nas.add(f"{TV}/notes.txt", size=500)
    (a,) = setup(env)
    env.backup(a)
    failing_stat(monkeypatch, "notes.txt")

    env.backup(a)

    assert a.exists("tv/notes.txt")


def test_stat_error_on_an_unknown_folder_is_not_pruned(env, monkeypatch):
    (a,) = setup(env)
    env.backup(a)
    a.path("tv/New").mkdir()
    a.path("tv/New/S01E01.mkv").write_bytes(b"x" * 10)
    env.nas.add_show(TV, "New", episodes=1, size=10)
    failing_stat(monkeypatch, "New")
    env.drives.dock(a)

    env.run("prune", "BK-6A", answers="y")

    assert a.exists("tv/New/S01E01.mkv")


def test_a_real_folder_named_files_is_reported_not_silently_dropped(env):
    env.nas.add_show(TV, "(files)", episodes=1, size=100)
    (a,) = setup(env)

    r = env.backup(a)

    assert "(files)" in r.out and "rename" in r.out


# H2, M2: --skip


def test_skip_protects_a_gone_unit(env):
    (a,) = setup(env)
    env.backup(a)
    env.nas.delete(f"{TV}/Wire")

    r = env.backup(a, "y", "--skip", "tv/Wire")

    assert "skipped on request" in r.out
    assert a.exists("tv/Wire/S01E01.mkv")


def test_unknown_skip_names_are_refused_with_similar_ones(env):
    (a,) = setup(env)
    env.backup(a)

    r = env.backup(a, "y", "--skip", "tv/wire")

    assert r.code != 0
    assert "tv/Wire" in r.out


def test_skipping_an_unchanged_unit_does_not_make_it_outdated(env):
    (a,) = setup(env)
    env.backup(a)

    env.backup(a, "y", "--skip", "tv/Wire")

    assert env.unit("tv/Wire")["copies"]["BK-6A"]["result"] == "ok"


def test_a_skipped_copy_is_ok_again_after_a_run_without_changes(env):
    (a,) = setup(env)
    env.backup(a)
    env.nas.replace(f"{TV}/Wire/S01E01.mkv")
    env.backup(a, "y", "--skip", "tv/Wire")
    assert env.unit("tv/Wire")["copies"]["BK-6A"]["result"] == "skipped"

    env.backup(a)

    assert env.unit("tv/Wire")["copies"]["BK-6A"]["result"] == "ok"
    assert env.registry()["drives"]["BK-6A"]["last_complete_run"]


# H3, M9: the end of a run never writes to a drive that's gone


def test_drive_gone_during_rsync_leaves_the_mount_point_untouched(env, monkeypatch):
    (a,) = setup(env)
    env.backup(a)
    env.nas.add(f"{TV}/Wire/S02E01.mkv", size=100)
    contents = (env.state_dir / "contents" / "BK-6A.txt").read_text()
    env.drives.dock(a)
    slot = env.host.usb / "usbshare1"

    def unplug(*args, **kwargs):
        slot.unlink()
        slot.mkdir()  # the empty mount point that's left behind
        return 12, "rsync error: error in rsync protocol data stream (code 12)"

    monkeypatch.setattr(execute, "run_rsync", unplug)
    r = env.run("run", answers="y")

    assert r.code != 0
    assert list(slot.iterdir()) == []
    assert (env.state_dir / "contents" / "BK-6A.txt").read_text() == contents
    assert "nothing was written" in r.out
    slot.rmdir()
    env.drives.slots.clear()


def test_a_full_drive_still_saves_the_registry_and_nas_side_files(env, monkeypatch):
    (a,) = setup(env)
    env.nas.add(f"{TV}/Wire/S02E01.mkv", size=100)
    real_write = execute.write_to_drive

    def full(path, text):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(execute, "write_to_drive", full)
    r = env.backup(a)
    monkeypatch.setattr(execute, "write_to_drive", real_write)

    assert r.code == 0, r.out
    assert "couldn't write" in r.out
    assert (env.state_dir / "contents" / "BK-6A.txt").exists()
    assert env.unit("tv/Wire")["copies"]["BK-6A"]["result"] == "ok"


# H4, M1: rsync errors


RECEIVER_ERROR = (
    'rsync: [receiver] mkstemp "/x/tv/Wire/.S01E01.mkv.Ab12" failed: '
    "Permission denied (13)\n"
    "rsync error: some files/attrs were not transferred (see previous errors) "
    "(code 23) at main.c(1338) [sender=3.4.1]\n"
)


def test_write_errors_on_the_drive_stop_the_run(env, monkeypatch):
    (a,) = setup(env)
    env.backup(a)
    env.nas.replace(f"{TV}/Wire/S01E01.mkv")
    env.nas.replace(f"{TV}/Lost/S01E01.mkv")
    monkeypatch.setattr(execute, "run_rsync", lambda *x, **k: (23, RECEIVER_ERROR))

    r = env.backup(a)

    assert r.code != 0
    assert "stopped" in r.out and "writing to the drive" in r.out
    results = {
        name: env.unit(name)["copies"]["BK-6A"]["result"]
        for name in ("tv/Lost", "tv/Wire")
    }
    assert "incomplete" in results.values()
    assert "errors" not in results.values()


def test_a_file_deleted_between_plan_and_copy_counts_as_vanished(env, monkeypatch):
    (a,) = setup(env)
    env.backup(a)
    env.nas.add(f"{TV}/Wire/S02E01.mkv", size=100)
    env.nas.add(f"{TV}/Wire/S02E02.mkv", size=100)
    real = execute.run_rsync

    def delete_first(*args, **kwargs):
        env.nas.delete(f"{TV}/Wire/S02E01.mkv")
        return real(*args, **kwargs)

    monkeypatch.setattr(execute, "run_rsync", delete_first)
    r = env.backup(a)

    assert r.code == 0, r.out
    assert "vanished" in r.out
    assert env.unit("tv/Wire")["copies"]["BK-6A"]["result"] == "ok"


def test_rsync_stopped_by_ctrl_c_counts_as_interrupted(env, monkeypatch):
    (a,) = setup(env)
    monkeypatch.setattr(
        execute, "run_rsync", lambda *a, **k: (20, "rsync error: received SIGINT")
    )

    r = env.backup(a)

    assert r.code == 130
    assert "Interrupted" in r.out


# M3: moving back never deletes the newest copy


def test_moving_back_does_not_trust_an_older_copy(env):
    a, b = setup(env, {"Wire": 10_000}, drives=("BK-6A", "BK-6B"))
    env.backup(a)
    env.ok("move", "set", "tv/Wire", "--to", "BK-6B")
    env.backup(b)
    env.nas.add(f"{TV}/Wire/S01E03.mkv", size=100)
    env.backup(b)
    env.ok("move", "set", "tv/Wire", "--to", "BK-6A")

    r = env.backup(b)

    assert "copy confirmed" not in r.out
    assert b.exists("tv/Wire/S01E03.mkv")


# M4: registry-only changes are shown and need a y


def test_registry_only_changes_are_shown_and_asked_for(env):
    env.nas.add_show("archive/movies", "Alien", episodes=1, size=100)
    env.nas.mark("archive/movies")
    env.config(
        rules=[pin("movies", "archive/movies", "BK-20A")],
        drives={"BK-20A": "5%", "BK-X": "5%"},
    )
    env.ok("init")
    drive = env.register("BK-20A")
    env.register("BK-X")
    env.backup(drive)
    env.config(
        rules=[pin("movies", "archive/movies", "BK-X")],
        drives={"BK-20A": "5%", "BK-X": "5%"},
    )

    r = env.backup(drive, answers="n")

    assert "movies/Alien" in r.out and "home BK-20A → BK-X" in r.out
    assert "[y/n]" in r.out
    assert env.unit("movies/Alien")["home"] == "BK-20A"


# M5: verify on odd drive states


def test_verify_survives_a_file_where_the_nas_has_a_folder(env):
    (a,) = setup(env)
    env.backup(a)
    env.nas.delete(f"{TV}/Wire/S01E01.mkv")
    env.nas.add(f"{TV}/Wire/S01E01.mkv/inner.mkv", size=10)
    env.drives.dock(a)

    r = env.run("verify")

    assert r.code == 0, r.out
    assert "changed on the NAS" in r.out


@not_root
def test_verify_counts_an_unreadable_drive_folder_as_read_errors(env):
    (a,) = setup(env)
    env.backup(a)
    os.chmod(a.path("tv/Wire"), 0)
    env.drives.dock(a)

    r = env.run("verify", answers="n")

    assert r.code == 0, r.out
    assert "can't be read on BK-6A" in r.out
    assert env.registry()["drives"]["BK-6A"]["last_verify"]
    assert env.registry()["drives"]["BK-6A"]["read_errors"] == 2
    os.chmod(a.path("tv/Wire"), 0o755)
    assert "consider replacing" in env.ok("status").out


def test_verify_commands_are_shell_quoted(env):
    (a,) = setup(env, {"It's $(x)": 1000})
    env.backup(a)
    env.nas.replace(f"{TV}/It's $(x)/S01E01.mkv", keep_mtime=True)
    env.drives.dock(a)

    r = env.run("verify")

    assert "rsync -a '" in r.out and "'\"'\"'" in r.out


# M6: restore.sh keeps newer NAS files and lists files in the dry run


def run_script(env, script, *args):
    path = os.path.dirname(env.host.rsync()) + os.pathsep + os.environ["PATH"]
    return subprocess.run(
        ["sh", str(script), *args],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": path},
        check=False,
    )


def test_restore_script_keeps_newer_files_on_the_nas(env):
    (a,) = setup(env)
    env.backup(a)
    env.nas.replace(f"{TV}/Wire/S01E01.mkv")  # newer than the backup
    newer = env.nas.read(f"{TV}/Wire/S01E01.mkv")

    run_script(env, a.path(".baruch/restore.sh"), "--run")

    assert env.nas.read(f"{TV}/Wire/S01E01.mkv") == newer


def test_restore_script_dry_run_lists_the_files(env):
    (a,) = setup(env)
    env.backup(a)
    env.nas.delete(f"{TV}/Wire")

    r = run_script(env, a.path(".baruch/restore.sh"))

    assert "Wire/S01E01.mkv" in r.stdout


# M7: drive-side read errors


@not_root
def test_unreadable_rule_folder_on_the_drive_is_reported_and_skipped(env):
    (a,) = setup(env)
    env.backup(a)
    os.chmod(a.path("tv"), 0)

    r = env.backup(a)

    os.chmod(a.path("tv"), 0o755)
    assert r.code != 0
    assert "can't be read on the drive" in r.out
    assert "up to date" not in r.out
    assert "first copy" not in r.out
    assert a.exists("tv/Wire/S01E01.mkv")


# M8: the registry is read after the lock is taken


def test_commands_load_the_registry_only_after_taking_the_lock(env, monkeypatch):
    (a,) = setup(env)
    env.backup(a)
    import baruch.commands as commands

    order = []
    real_lock, real_get = commands.lock, commands.get_registry
    monkeypatch.setattr(
        commands, "lock", lambda *x: (order.append("lock"), real_lock(*x))[1]
    )
    monkeypatch.setattr(
        commands, "get_registry", lambda *x: (order.append("load"), real_get(*x))[1]
    )

    env.ok("drive", "forget", "BK-6A", answers="y")

    assert order[:2] == ["lock", "load"]


# L2: unverified home copies are confirmed by their sync, not by existing


def test_unverified_home_copy_stays_unverified_if_the_run_stops_first(env, monkeypatch):
    a, b = setup(env, {"A": 30_000, "Big": 80_000}, drives=("BK-6A", "BK-6B"))
    env.backup(a)
    env.backup(b)
    (env.state_dir / "registry.yaml").unlink()
    env.drives.dock(b)
    env.ok("registry", "restore", answers="y")
    env.drives.undock(b)
    env.nas.replace(f"{TV}/Big/S01E01.mkv")
    monkeypatch.setattr(
        execute, "run_rsync", lambda *x, **k: (_ for _ in ()).throw(KeyboardInterrupt())
    )

    env.backup(a)

    assert env.unit("tv/Big")["copies"]["BK-6A"]["result"] == "incomplete"


# L3: excluded names on the drive are never orphaned


def test_excluded_names_inside_rule_folders_on_the_drive_are_not_orphaned(env):
    (a,) = setup(env)
    env.backup(a)
    a.path("tv/@eaDir").mkdir()
    a.path("tv/@eaDir/thumb").write_bytes(b"x")
    env.drives.dock(a)

    env.run("prune", "BK-6A", answers="y")

    assert a.exists("tv/@eaDir/thumb")


# L5: tabs and newlines in names


@pytest.mark.skipif(os.name != "posix", reason="POSIX names")
def test_tabs_and_newlines_in_names_survive_the_contents_list(env):
    (a,) = setup(env, {"Tab\there": 100, "New\nline": 100})
    env.backup(a)

    assert (
        "Tab\there/S01E01.mkv" in env.ok("find", "S01E01").out.replace("\\x09", "\t")
        or "Tab\\x09here" in env.ok("find", "S01E01").out
    )
    contents = (env.state_dir / "contents" / "BK-6A.txt").read_text()
    assert len(contents.splitlines()) == len(a.files())


# L7: move suggestions prefer the fewest units


def test_move_suggestion_prefers_one_large_unit_over_many_small_ones(env):
    shows = {f"Old{i}": 4_000 for i in range(5)}
    shows["Big"] = 50_000
    a, b = setup(env, shows, drives=("BK-6A", "BK-6B"))
    for i in range(5):
        os.utime(env.nas.path(f"{TV}/Old{i}/S01E01.mkv"), (1, 1))
    env.backup(a)
    env.nas.add(f"{TV}/Big/S02E01.mkv", size=25_000)

    r = env.backup(a)

    suggestions = {line for line in r.lines if "baruch move set" in line}
    assert len(suggestions) == 1
    assert '"tv/Big"' in suggestions.pop()


# L8: partial files are cleaned up


def test_partial_files_are_cleaned_up_after_a_run(env):
    (a,) = setup(env)
    a.path(".baruch/partial").mkdir(parents=True)
    a.path(".baruch/partial/S01E01.mkv").write_bytes(b"leftover")

    env.backup(a)

    assert not a.exists(".baruch/partial/S01E01.mkv")


# L9: config entries of the wrong shape


@pytest.mark.parametrize(
    "doc, problem",
    [
        ("drives:\n  BK-6A: 10%\n", "BK-6A"),
        ("rules:\n  - just a string\n", "rule"),
        ("rules:\n  - { name: a, source: relative/path, pin: X }\n", "absolute"),
    ],
)
def test_badly_shaped_config_entries_are_reported(env, doc, problem):
    env.config_dir.mkdir(parents=True, exist_ok=True)
    (env.config_dir / "config.yaml").write_text(doc)

    r = env.run("config", "check")

    assert r.code != 0
    assert problem in r.out
    assert "Traceback" not in r.out


def test_overlap_through_a_symlink_is_detected(env):
    env.nas.mark(TV)
    os.symlink(env.nas.path("archive"), env.tmp / "alias")
    env.config(rules=[], drives={})
    (env.config_dir / "config.yaml").write_text(
        "rules:\n"
        f"  - {{ name: a, source: {env.nas.path('archive')}, pin: X }}\n"
        f"  - {{ name: b, source: {env.tmp / 'alias' / 'tv'}, pin: X }}\n"
    )

    r = env.run("config", "check")

    assert r.code != 0 and "inside" in r.out


def _unused():  # keep imports used in every platform
    return shutil
