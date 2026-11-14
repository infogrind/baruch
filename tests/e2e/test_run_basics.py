"""A backup run: plan first, y/n, exactly the plan."""

import os
from pathlib import Path

from support import pin


def setup_movies(env, capacity=100_000):
    env.nas.add_show("archive/movies", "Heat (1995)", episodes=3, size=3000)
    env.nas.add_show("archive/movies", "Alien", episodes=2, size=2000)
    env.nas.add("archive/movies/readme.txt", size=100)
    env.nas.mark("archive/movies")
    env.config(
        rules=[pin("movies", "archive/movies", "BK-20A")], drives={"BK-20A": "5%"}
    )
    env.ok("init")
    return env.register("BK-20A", capacity=capacity)


def test_first_run_copies_every_unit_as_first_copy(env):
    drive = setup_movies(env)

    r = env.backup(drive)

    assert r.code == 0, r.out
    assert "movies/Heat (1995)" in r.out
    assert "first copy: 3 files" in r.out
    assert "movies/(files)" in r.out
    assert r.lines[-1] == "safe to eject in DSM"
    for rel in ("Heat (1995)/S01E01.mkv", "Alien/S01E02.mkv", "readme.txt"):
        assert drive.read(f"movies/{rel}") == env.nas.read(f"archive/movies/{rel}")
    src = env.nas.path("archive/movies/Alien/S01E01.mkv").stat()
    assert drive.path("movies/Alien/S01E01.mkv").stat().st_mtime == src.st_mtime


def test_first_run_records_units_and_copies(env):
    drive = setup_movies(env)
    env.backup(drive)

    unit = env.unit("movies/Heat (1995)")
    assert unit["home"] == "BK-20A"
    copy = unit["copies"]["BK-20A"]
    assert copy["result"] == "ok"
    assert copy["size"] == 3000
    assert copy["synced"]
    assert "movies/(files)" in env.registry()["units"]
    assert env.registry()["drives"]["BK-20A"]["last_complete_run"]


def test_run_writes_snapshot_config_and_contents_to_the_drive(env):
    drive = setup_movies(env)
    env.backup(drive)

    assert drive.exists(".baruch/label.yaml")
    assert drive.exists(".baruch/registry.yaml")
    assert drive.exists(".baruch/config.yaml")
    contents = drive.path(".baruch/contents.txt").read_text()
    assert "movies/Heat (1995)/S01E01.mkv" in contents
    assert (env.state_dir / "contents" / "BK-20A.txt").exists()
    assert list((env.state_dir / "logs").iterdir())


def test_answer_n_changes_nothing(env):
    drive = setup_movies(env)

    r = env.backup(drive, answers="n")

    assert r.code == 0
    assert "nothing changed" in r.out.lower()
    assert drive.files() == set()
    assert env.registry()["units"] == {}


def test_dry_run_shows_the_plan_and_writes_nothing(env):
    drive = setup_movies(env)
    before = env.registry()

    r = env.backup(drive, "", "--dry-run")

    assert r.code == 0
    assert "first copy: 3 files" in r.out
    assert "[y/n]" not in r.out
    assert drive.files() == set()
    assert env.registry() == before


def test_run_refuses_without_a_terminal(env):
    drive = setup_movies(env)
    env.drives.dock(drive)

    r = env.run("run", answers="y", interactive=False)

    assert r.code != 0
    assert "terminal" in r.out
    assert drive.files() == set()


def test_second_run_lists_each_changed_file(env):
    drive = setup_movies(env)
    env.backup(drive)
    env.nas.replace("archive/movies/Heat (1995)/S01E01.mkv")
    env.nas.delete("archive/movies/Heat (1995)/S01E02.mkv")
    env.nas.add("archive/movies/Heat (1995)/extras.mkv", size=500)

    r = env.backup(drive)

    assert "~ Heat (1995)/S01E01.mkv" in r.out
    assert "- Heat (1995)/S01E02.mkv" in r.out
    assert "+ Heat (1995)/extras.mkv" in r.out
    assert "first copy" not in r.out
    assert "Alien" not in r.out  # unchanged units aren't listed file by file
    assert drive.read("movies/Heat (1995)/S01E01.mkv") == env.nas.read(
        "archive/movies/Heat (1995)/S01E01.mkv"
    )
    assert not drive.exists("movies/Heat (1995)/S01E02.mkv")
    assert drive.exists("movies/Heat (1995)/extras.mkv")


def test_unchanged_nas_gives_an_empty_plan(env):
    drive = setup_movies(env)
    env.backup(drive)

    r = env.backup(drive)

    assert r.code == 0
    assert "nothing to do" in r.out.lower()


def test_warnings_come_at_the_end_right_above_the_question(env):
    drive = setup_movies(env)
    env.backup(drive)
    for i in (1, 2, 3):
        env.nas.delete(f"archive/movies/Heat (1995)/S01E{i:02d}.mkv")

    r = env.backup(drive, answers="n")

    warning = next(i for i, line in enumerate(r.lines) if line.startswith("WARNING"))
    question = next(i for i, line in enumerate(r.lines) if "[y/n]" in line)
    last_file = max(
        i for i, line in enumerate(r.lines) if line.strip().startswith("- ")
    )
    assert last_file < warning < question
    assert "movies/Heat (1995)" in "\n".join(r.lines[warning:question])
    assert drive.exists("movies/Heat (1995)/S01E01.mkv")


def test_summary_names_the_saved_plan_file(env):
    drive = setup_movies(env)

    r = env.backup(drive, answers="n")

    plan_line = next(line for line in r.lines if line.startswith("Plan saved to "))
    path = plan_line.removeprefix("Plan saved to ").strip()
    assert os.path.exists(path)
    assert "first copy: 3 files" in Path(path).read_text()


def test_skip_leaves_a_unit_out_and_it_stays_outdated(env):
    drive = setup_movies(env)
    env.backup(drive)
    synced = env.unit("movies/Alien")["copies"]["BK-20A"]["synced"]
    env.nas.replace("archive/movies/Alien/S01E01.mkv")
    env.nas.replace("archive/movies/Heat (1995)/S01E01.mkv")

    r = env.backup(drive, "y", "--skip", "movies/Alien")

    assert "movies/Alien" in r.out and "skipped on request" in r.out
    assert drive.read("movies/Alien/S01E01.mkv") != env.nas.read(
        "archive/movies/Alien/S01E01.mkv"
    )
    assert drive.read("movies/Heat (1995)/S01E01.mkv") == env.nas.read(
        "archive/movies/Heat (1995)/S01E01.mkv"
    )
    copy = env.unit("movies/Alien")["copies"]["BK-20A"]
    assert copy["result"] == "skipped"
    assert copy["synced"] == synced


def test_excluded_names_are_not_copied_and_are_not_units(env):
    env.nas.add_show("archive/movies", "Alien", episodes=2, size=2000)
    env.nas.add("archive/movies/Alien/@eaDir/thumb.jpg", size=50)
    env.nas.add("archive/movies/Alien/.DS_Store", size=10)
    env.nas.add("archive/movies/@eaDir/x.db", size=10)
    env.nas.mark("archive/movies")
    env.config(
        rules=[pin("movies", "archive/movies", "BK-20A")], drives={"BK-20A": "5%"}
    )
    env.ok("init")
    drive = env.register("BK-20A")

    env.backup(drive)

    assert drive.exists("movies/Alien/S01E01.mkv")
    assert not drive.exists("movies/Alien/@eaDir")
    assert not drive.exists("movies/Alien/.DS_Store")
    assert "movies/@eaDir" not in env.registry()["units"]


def test_symlinks_hard_links_and_empty_folders(env):
    env.nas.add_show("archive/movies", "Alien", episodes=2, size=2000)
    os.symlink("S01E01.mkv", env.nas.path("archive/movies/Alien/latest"))
    os.link(
        env.nas.path("archive/movies/Alien/S01E01.mkv"),
        env.nas.path("archive/movies/Alien/hardlink.mkv"),
    )
    env.nas.mkdir("archive/movies/Empty")
    env.nas.mark("archive/movies")
    env.config(
        rules=[pin("movies", "archive/movies", "BK-20A")], drives={"BK-20A": "5%"}
    )
    env.ok("init")
    drive = env.register("BK-20A")

    env.backup(drive)

    assert os.readlink(drive.path("movies/Alien/latest")) == "S01E01.mkv"
    a = drive.path("movies/Alien/S01E01.mkv").stat()
    b = drive.path("movies/Alien/hardlink.mkv").stat()
    assert a.st_ino == b.st_ino
    assert drive.path("movies/Empty").is_dir()
    assert env.unit("movies/Empty")["copies"]["BK-20A"]["size"] == 0
    assert env.unit("movies/Alien")["copies"]["BK-20A"]["size"] == 2000


def test_several_docked_drives_need_a_choice(env):
    drive = setup_movies(env)
    other = env.register("BK-6A", capacity=50_000)
    env.drives.dock(drive)
    env.drives.dock(other)

    r = env.run("run", "--drive", "BK-20A", answers="y")

    assert r.code == 0, r.out
    assert drive.exists("movies/Alien/S01E01.mkv")
    assert other.files() == set()


def test_several_docked_drives_ask_which_one(env):
    drive = setup_movies(env)
    other = env.register("BK-6A", capacity=50_000)
    env.drives.dock(drive)
    env.drives.dock(other)

    r = env.run("run", answers="BK-20A\ny\n")

    assert r.code == 0, r.out
    assert "Which drive" in r.out
    assert drive.exists("movies/Alien/S01E01.mkv")


def test_no_usable_drive_lists_the_mounted_volumes(env):
    setup_movies(env)
    stranger = env.drives.new("stranger")
    env.drives.dock(stranger)

    r = env.run("run", answers="y")

    assert r.code != 0
    assert "No usable drive" in r.out
    assert "usbshare1" in r.out and "no label" in r.out


def test_each_real_run_writes_a_log_with_the_plan(env):
    drive = setup_movies(env)
    env.backup(drive)

    logs = list((env.state_dir / "logs").iterdir())
    assert logs
    assert any("first copy: 3 files" in log.read_text() for log in logs)
