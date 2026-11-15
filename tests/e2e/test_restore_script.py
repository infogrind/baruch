"""The restore script each run writes to the drive: /.baruch/restore.sh."""

import os
import shutil
import subprocess

from support import pin, spread

TV = "archive/tv"
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
            spread("tv", TV, ["BK-6A"]),
            pin("home", "homes/alex", "BK-20A"),
        ],
        drives={"BK-20A": "5%", "BK-6A": "10%"},
    )
    env.ok("init")
    drive = env.register("BK-20A", capacity=200_000)
    env.backup(drive)
    return drive


def run_script(env, script, *args):
    rsync_dir = os.path.dirname(env.host.rsync())
    path = rsync_dir + os.pathsep + os.environ.get("PATH", "")
    return subprocess.run(
        ["sh", str(script), *args],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": path},
        check=False,
    )


def test_each_run_writes_an_executable_restore_script(env):
    drive = setup(env)

    script = drive.path(".baruch/restore.sh")

    assert script.exists()
    assert os.access(script, os.X_OK)
    text = script.read_text()
    assert "--run" in text
    assert env.src("archive/movies") in text
    assert env.src("homes/alex") in text
    assert env.src(TV) not in text  # no tv data on this drive


def test_the_rule_holding_baruchs_own_files_comes_first(env):
    drive = setup(env)
    env.config_dir = env.nas.path("homes/alex/.config/baruch")
    shutil.copytree(env.tmp / "config", env.config_dir)

    env.backup(drive)

    lines = [
        line
        for line in drive.path(".baruch/restore.sh").read_text().splitlines()
        if line.startswith('restore "')
    ]
    assert lines[0].startswith('restore "home"')


def test_without_run_the_script_copies_nothing(env):
    drive = setup(env)
    env.nas.delete(f"{MOVIES}/Alien")

    result = run_script(env, drive.path(".baruch/restore.sh"))

    assert result.returncode == 0, result.stderr
    assert "Dry run" in result.stdout
    assert not env.nas.path(f"{MOVIES}/Alien").exists()


def test_with_run_it_restores_everything_on_the_drive(env):
    drive = setup(env)
    shutil.rmtree(env.nas.path("archive"))
    shutil.rmtree(env.nas.path("homes"))

    result = run_script(env, drive.path(".baruch/restore.sh"), "--run")

    assert result.returncode == 0, result.stderr
    assert env.nas.read(f"{MOVIES}/Alien/S01E01.mkv") == drive.read(
        "movies/Alien/S01E01.mkv"
    )
    assert env.nas.path(f"{MOVIES}/.backup-source").exists()
    assert env.nas.read("homes/alex/notes.txt") == drive.read("home/notes.txt")


def test_the_script_finds_the_drive_wherever_it_is_mounted(env):
    drive = setup(env)
    moved = env.tmp / "elsewhere" / "usbshare2"
    shutil.copytree(drive.root, moved, symlinks=True)
    shutil.rmtree(env.nas.path("archive"))

    result = run_script(env, moved / ".baruch" / "restore.sh", "--run")

    assert result.returncode == 0, result.stderr
    assert env.nas.path(f"{MOVIES}/Alien/S01E01.mkv").exists()


def test_it_never_deletes_on_the_nas(env):
    drive = setup(env)
    env.nas.add(f"{MOVIES}/Alien/newer.mkv", size=10)

    run_script(env, drive.path(".baruch/restore.sh"), "--run")

    assert env.nas.path(f"{MOVIES}/Alien/newer.mkv").exists()


def test_prune_rewrites_the_script(env):
    drive = setup(env)
    env.config(
        rules=[pin("movies", MOVIES, "BK-20A")],
        drives={"BK-20A": "5%"},
    )
    env.drives.dock(drive)
    env.ok("prune", "BK-20A", answers="y")

    text = drive.path(".baruch/restore.sh").read_text()

    assert 'restore "movies"' in text
    assert 'restore "home"' not in text


def test_odd_names_are_quoted_safely(env):
    setup(env)
    a = env.register("BK-6A")
    env.backup(a)
    shutil.rmtree(env.nas.path(TV))

    result = run_script(env, a.path(".baruch/restore.sh"), "--run")

    assert result.returncode == 0, result.stderr
    assert env.nas.path(f"{TV}/It's Always Sunny [HD]/S01E01.mkv").exists()
