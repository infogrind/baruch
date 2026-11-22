"""Two rules with the same source: two independent copies on different drives."""

import shutil

from support import pin, spread
from support import run_restore_script as run_script

HOME = "homes/alex"


def setup(env):
    env.nas.add(f"{HOME}/notes.txt", size=100)
    env.nas.add(f"{HOME}/docs/a.txt", size=200)
    env.nas.mark(HOME)
    env.config(
        rules=[pin("home", HOME, "BK-20A"), pin("home-1t", HOME, "BK-1T")],
        drives={"BK-20A": "5%", "BK-1T": "5%"},
    )
    env.ok("init")
    return env.register("BK-20A"), env.register("BK-1T")


def test_config_check_accepts_the_same_source_on_other_drives(env):
    setup(env)

    r = env.run("config", "check")

    assert r.code == 0, r.out


def test_config_check_refuses_the_same_source_with_a_drive_in_common(env):
    env.nas.mark(HOME)
    env.config(
        rules=[
            pin("home", HOME, "BK-20A"),
            spread("home-2", HOME, ["BK-1T", "BK-20A"]),
        ],
        drives={"BK-20A": "5%", "BK-1T": "5%"},
    )

    r = env.run("config", "check")

    assert r.code != 0
    assert "same source and drive BK-20A in common" in r.out


def test_each_rule_backs_up_the_source_to_its_own_drive(env):
    big, small = setup(env)

    env.backup(big)
    env.backup(small)

    assert env.home("home/docs") == "BK-20A"
    assert env.home("home-1t/docs") == "BK-1T"
    assert big.read("home/docs/a.txt") == env.nas.read(f"{HOME}/docs/a.txt")
    assert small.read("home-1t/docs/a.txt") == env.nas.read(f"{HOME}/docs/a.txt")
    assert not big.path("home-1t").exists()
    assert not small.path("home").exists()


def test_baruchs_own_state_is_summarised_in_every_rule_that_holds_it(env):
    env.state_dir = env.nas.path(f"{HOME}/.local/state/baruch")
    big, small = setup(env)
    env.backup(big)
    env.backup(small)
    env.backup(big)  # the state changes on every run

    r = env.backup(small)

    assert r.code == 0, r.out
    assert "Baruch's own state" in r.out
    assert "registry.yaml" not in r.out.split("Baruch's own state")[0]
    assert "Warning" not in r.out


def test_restoring_both_copies_brings_the_source_back_once(env):
    big, small = setup(env)
    env.backup(big)
    env.backup(small)
    shutil.rmtree(env.nas.path(HOME))

    for drive in (big, small):
        env.drives.dock(drive)
        result = run_script(env, drive.path(".baruch/restore.sh"), "--run")
        env.drives.undock(drive)
        assert result.returncode == 0, result.stdout + result.stderr

    assert env.nas.read(f"{HOME}/docs/a.txt") == big.read("home/docs/a.txt")
    assert env.nas.read(f"{HOME}/notes.txt") == small.read("home-1t/notes.txt")
    marker = f"touch {env.src(HOME)}/.backup-source"
    assert result.stdout.count(marker) == 1
