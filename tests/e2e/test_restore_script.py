"""The restore script each run writes to the drive: /.baruch/restore.sh.

These run the real script with plain `sh` against the fake NAS."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from support import needs_linux, not_root, pin, spread
from support import run_restore_script as run_script

TV = "archive/tv"


def shellcheck() -> str:
    """The binary from the shellcheck-py dev dependency, in this venv."""
    return str(Path(sys.executable).parent / "shellcheck")


MOVIES = "archive/movies"


def setup(env):
    env.nas.add_show(MOVIES, "Alien", episodes=2, size=2000)
    env.nas.mark(MOVIES)
    env.nas.add_show(TV, "It's Always Sunny [HD]", episodes=2, size=2000)
    env.nas.mark(TV)
    env.nas.add("homes/alex/notes.txt", size=100)
    env.nas.mark("homes/alex")
    env.config(
        rules=[
            pin("movies", MOVIES, "BK-20A"),
            spread("tv", TV, ["BK-6A", "BK-6B"]),
            pin("home", "homes/alex", "BK-20A"),
        ],
        drives={"BK-20A": "5%", "BK-6A": "10%", "BK-6B": "10%"},
    )
    env.ok("init")
    drive = env.register("BK-20A", capacity=200_000)
    env.backup(drive)
    return drive


def test_each_run_writes_an_executable_restore_script(env):
    drive = setup(env)

    script = drive.path(".baruch/restore.sh")

    assert os.access(script, os.X_OK)
    text = script.read_text()
    assert "--run" in text and "--stale" in text and "--overwrite" in text
    assert "'movies/Alien'" in text and "'home/notes.txt'" not in text
    assert "It's Always Sunny" not in text  # no tv data on this drive


def test_the_rule_holding_baruchs_own_files_comes_first(env):
    drive = setup(env)
    env.config_dir = env.nas.path("homes/alex/.config/baruch")
    shutil.copytree(env.tmp / "config", env.config_dir)

    env.backup(drive)

    units = [
        line
        for line in drive.path(".baruch/restore.sh").read_text().splitlines()
        if line.startswith("unit home")
    ]
    assert " 'home' " in units[0]  # the rule field of the first unit


def test_without_run_the_script_copies_nothing(env):
    drive = setup(env)
    env.nas.delete(f"{MOVIES}/Alien")

    result = run_script(env, drive.path(".baruch/restore.sh"))

    assert result.returncode == 0, result.stderr
    assert "Dry run" in result.stdout
    assert not env.nas.path(f"{MOVIES}/Alien").exists()


def test_with_run_it_restores_the_home_units_but_never_the_markers(env):
    drive = setup(env)
    shutil.rmtree(env.nas.path(MOVIES))  # the share /archive is still there
    shutil.rmtree(env.nas.path("homes/alex"))

    result = run_script(env, drive.path(".baruch/restore.sh"), "--run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert env.nas.read(f"{MOVIES}/Alien/S01E01.mkv") == drive.read(
        "movies/Alien/S01E01.mkv"
    )
    assert env.nas.read("homes/alex/notes.txt") == drive.read("home/notes.txt")
    assert not env.nas.path(f"{MOVIES}/.backup-source").exists()
    assert "re-create the markers" in result.stdout
    assert f"touch {env.src(MOVIES)}/.backup-source" in result.stdout


def test_a_rule_folder_whose_share_is_missing_is_skipped(env):
    drive = setup(env)
    shutil.rmtree(env.nas.path("archive"))  # e.g. the volume isn't mounted

    result = run_script(env, drive.path(".baruch/restore.sh"), "--run")

    assert "doesn't exist" in result.stdout and "skipped" in result.stdout
    assert not env.nas.path("archive").exists()
    assert result.returncode == 1  # the restore isn't complete
    assert "2 units not restored because of an error" in result.stdout  # movies
    assert env.nas.path("homes/alex/notes.txt").exists()  # the others go on


@not_root
def test_a_failed_copy_is_counted_and_the_script_fails(env):
    drive = setup(env)
    shutil.rmtree(env.nas.path(MOVIES))
    shutil.rmtree(env.nas.path("homes/alex"))
    drive.path("movies/Alien/S01E01.mkv").chmod(0)  # rsync can't read it
    try:
        result = run_script(env, drive.path(".baruch/restore.sh"), "--run")
    finally:
        drive.path("movies/Alien/S01E01.mkv").chmod(0o644)

    out = result.stdout
    assert result.returncode == 1, out
    assert "!! movies/Alien: rsync failed (exit 23)" in out
    assert "1 unit not restored because of an error" in out
    assert env.nas.read("homes/alex/notes.txt") == drive.read("home/notes.txt")


def test_the_script_passes_shellcheck(env):
    drive = setup(env)
    env.nas.add_show(TV, "Odd $(name) `x` 'q'", episodes=1, size=100)
    tv_drive = env.register("BK-6A")
    env.backup(tv_drive)
    (drive.root / "stray.txt").write_text("x")  # unexplained data: a `note` line
    env.backup(drive)

    for script in (
        drive.path(".baruch/restore.sh"),
        tv_drive.path(".baruch/restore.sh"),
    ):
        result = subprocess.run(
            [shellcheck(), "--shell=sh", "--severity=style", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout


def test_the_script_finds_the_drive_wherever_it_is_mounted(env):
    drive = setup(env)
    moved = env.tmp / "elsewhere" / "usbshare2"
    shutil.copytree(drive.root, moved, symlinks=True)
    shutil.rmtree(env.nas.path(MOVIES))

    result = run_script(env, moved / ".baruch" / "restore.sh", "--run")

    assert result.returncode == 0, result.stderr
    assert env.nas.path(f"{MOVIES}/Alien/S01E01.mkv").exists()


def test_newer_files_on_the_nas_are_kept_and_listed_until_overwrite(env):
    drive = setup(env)
    env.nas.add(f"{MOVIES}/Alien/extra.mkv", size=10)  # never deletes
    env.host.advance(days=1)
    env.nas.replace(f"{MOVIES}/Alien/S01E01.mkv")  # newer than the drive's
    newer = env.nas.read(f"{MOVIES}/Alien/S01E01.mkv")
    script = drive.path(".baruch/restore.sh")

    result = run_script(env, script, "--run")

    assert env.nas.path(f"{MOVIES}/Alien/extra.mkv").exists()
    assert env.nas.read(f"{MOVIES}/Alien/S01E01.mkv") == newer
    assert "kept 1 newer files on the NAS" in result.stdout
    assert "S01E01.mkv" in result.stdout

    run_script(env, script, "--run", "--overwrite")
    assert env.nas.read(f"{MOVIES}/Alien/S01E01.mkv") == drive.read(
        "movies/Alien/S01E01.mkv"
    )


def test_an_incomplete_unit_is_listed_not_restored(env):
    drive = setup(env)
    from baruch import records
    from baruch.records import IN_PROGRESS, Record

    recs = records.read(drive.root)
    old = recs["movies/Alien"]
    recs["movies/Alien"] = Record(old.started, old.synced, IN_PROGRESS)
    records.write(drive.root, recs)
    shutil.rmtree(env.nas.path(f"{MOVIES}/Alien"))

    result = run_script(env, drive.path(".baruch/restore.sh"), "--run")

    assert "movies/Alien: not restored: its last sync didn't finish" in result.stdout
    assert "by hand:" in result.stdout
    assert not env.nas.path(f"{MOVIES}/Alien").exists()


def test_held_stale_units_come_back_in_the_second_pass(env):
    setup(env)
    a = env.register("BK-6A")
    env.register("BK-6B")
    env.backup(a)
    env.ok("move", "set", "tv/It's Always Sunny [HD]", "--to", "BK-6B")
    env.backup(a)  # BK-6B has no copy yet: held
    shutil.rmtree(env.nas.path(TV))
    env.nas.mkdir(TV)
    script = a.path(".baruch/restore.sh")

    first = run_script(env, script, "--run")
    assert "second pass (--stale)" in first.stdout
    assert not env.nas.path(f"{TV}/It's Always Sunny [HD]").exists()

    second = run_script(env, script, "--run", "--stale")
    assert second.returncode == 0, second.stderr
    assert env.nas.path(f"{TV}/It's Always Sunny [HD]/S01E01.mkv").exists()


def test_prune_rewrites_the_script(env):
    drive = setup(env)
    env.config(rules=[pin("movies", MOVIES, "BK-20A")], drives={"BK-20A": "5%"})
    env.drives.dock(drive)
    env.ok("prune", "BK-20A", answers="y")

    text = drive.path(".baruch/restore.sh").read_text()

    assert "'movies/Alien'" in text
    units = [line for line in text.splitlines() if line.startswith("unit ")]
    assert not any(" 'home' " in line for line in units)
    assert "rule home isn" in text  # listed as unexplained, not restored


def test_unreadable_records_restore_nothing_by_themselves(env):
    drive = setup(env)
    drive.path(".baruch/units.yaml").unlink()
    shutil.rmtree(env.nas.path(MOVIES))
    env.nas.mkdir(MOVIES)

    result = run_script(env, drive.path(".baruch/restore.sh"), "--run")

    assert "can't be read: nothing is restored by itself" in result.stdout
    assert not env.nas.path(f"{MOVIES}/Alien").exists()


def test_odd_names_are_quoted_safely(env):
    setup(env)
    a = env.register("BK-6A")
    env.nas.add_show(TV, "back\\slash $HOME `x`", episodes=1, size=10)
    env.backup(a)
    shutil.rmtree(env.nas.path(TV))
    env.nas.mkdir(TV)

    result = run_script(env, a.path(".baruch/restore.sh"), "--run")

    assert result.returncode == 0, result.stderr
    assert env.nas.path(f"{TV}/It's Always Sunny [HD]/S01E01.mkv").exists()
    assert env.nas.path(f"{TV}/back\\slash $HOME `x`/S01E01.mkv").exists()


@needs_linux
def test_names_that_arent_valid_utf8_are_restored_byte_exact(env):
    setup(env)
    a = env.register("BK-6A")
    name = os.fsdecode(b"Caf\xe9")
    env.nas.add_show(TV, name, episodes=1, size=10)
    env.backup(a)
    shutil.rmtree(env.nas.path(TV))
    env.nas.mkdir(TV)

    result = run_script(env, a.path(".baruch/restore.sh"), "--run")

    assert result.returncode == 0, result.stderr
    assert os.path.exists(os.fsencode(env.nas.path(TV)) + b"/Caf\xe9/S01E01.mkv")
