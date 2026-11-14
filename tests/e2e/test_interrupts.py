"""Interrupted runs, a full disk, unverified copies, bookkeeping."""

from support import spread

import baruch.execute as execute

TV = "archive/tv"


def setup(env, shows=None):
    for name, size in (shows or {"A": 30_000, "B": 20_000, "C": 10_000}).items():
        env.nas.add_show(TV, name, episodes=2, size=size)
    env.nas.mark(TV)
    env.config(
        rules=[spread("tv", TV, ["BK-6A", "BK-6B"])],
        drives={"BK-6A": "10%", "BK-6B": "10%"},
    )
    env.ok("init")


def fail_on_call(monkeypatch, n, exc=None, result=None):
    """Make the n-th rsync call raise `exc` or return `result`."""
    real = execute.run_rsync
    calls = {"n": 0}

    def fake(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == n:
            if exc:
                raise exc
            return result
        return real(*args, **kwargs)

    monkeypatch.setattr(execute, "run_rsync", fake)


def test_ctrl_c_records_finished_units_and_the_next_run_continues(env, monkeypatch):
    setup(env)
    a = env.register("BK-6A")
    fail_on_call(monkeypatch, 2, exc=KeyboardInterrupt())

    r = env.backup(a)

    assert r.code == 130
    assert "Interrupted" in r.out
    assert r.lines[-1] == "safe to eject in DSM"  # snapshot and contents written
    assert env.unit("tv/A")["copies"]["BK-6A"]["result"] == "ok"
    assert env.unit("tv/B")["copies"]["BK-6A"]["result"] == "incomplete"
    assert "BK-6A" not in env.unit("tv/C")["copies"]
    assert a.exists(".baruch/registry.yaml")

    monkeypatch.undo()
    r = env.backup(a)
    assert r.code == 0, r.out
    assert env.unit("tv/B")["copies"]["BK-6A"]["result"] == "ok"
    assert a.read("tv/B/S01E01.mkv") == env.nas.read(f"{TV}/B/S01E01.mkv")
    first_unit = next(line for line in r.lines if line.startswith("tv/"))
    assert first_unit.startswith("tv/B")  # incomplete units go first


def test_full_disk_mid_unit_marks_it_incomplete_and_stops(env, monkeypatch):
    setup(env)
    a = env.register("BK-6A")
    fail_on_call(monkeypatch, 2, result=(11, "write failed: No space left on device"))

    r = env.backup(a)

    assert r.code != 0
    assert "No space left" in r.out
    assert env.unit("tv/B")["copies"]["BK-6A"]["result"] == "incomplete"
    assert "BK-6A" not in env.unit("tv/C")["copies"]


def test_unverified_copies_are_confirmed_when_their_drive_is_docked(env):
    setup(env, {"A": 30_000, "Big": 80_000})
    a = env.register("BK-6A")
    b = env.register("BK-6B")
    env.backup(a)
    env.backup(b)
    (env.state_dir / "registry.yaml").unlink()
    env.drives.dock(b)
    env.ok("registry", "restore", answers="y")
    env.drives.undock(b)
    # Largest first: Big went to BK-6A, A to BK-6B (whose snapshot this is).
    assert env.unit("tv/Big")["copies"]["BK-6A"]["unverified"] is True

    env.backup(a)

    assert "unverified" not in env.unit("tv/Big")["copies"]["BK-6A"]


def test_nothing_unverified_is_trusted_for_a_cleanup(env):
    setup(env, {"A": 30_000})
    a = env.register("BK-6A")
    b = env.register("BK-6B")
    env.backup(a)
    env.ok("move", "set", "tv/A", "--to", "BK-6B")
    env.backup(b)
    (env.state_dir / "registry.yaml").unlink()
    env.drives.dock(a)
    env.ok("registry", "restore", answers="y")  # BK-6B's copy is now unverified
    env.drives.undock(a)

    r = env.backup(a)

    assert a.exists("tv/A/S01E01.mkv")
    assert "copy confirmed" not in r.out


def test_gone_unit_stays_registered_until_every_copy_is_deleted(env):
    setup(env, {"A": 30_000})
    a = env.register("BK-6A")
    b = env.register("BK-6B")
    env.backup(a)
    env.ok("move", "set", "tv/A", "--to", "BK-6B")
    env.backup(b)
    env.nas.delete(f"{TV}/A")

    env.backup(b)
    assert "tv/A" in env.registry()["units"]

    env.backup(a)
    assert "tv/A" not in env.registry()["units"]
    assert not a.exists("tv/A")


def test_a_run_with_nothing_to_do_still_records_the_drive(env):
    setup(env, {"A": 30_000})
    a = env.register("BK-6A")
    env.backup(a)
    before = env.registry()["drives"]["BK-6A"]["last_seen"]

    r = env.backup(a)

    assert r.code == 0
    assert "[y/n]" not in r.out
    assert env.registry()["drives"]["BK-6A"]["last_seen"] != before
    assert r.lines[-1] == "safe to eject in DSM"


def test_only_one_command_at_a_time(env):
    setup(env, {"A": 30_000})
    a = env.register("BK-6A")
    from baruch.registry import Store

    holder = Store(env.state_dir, env.host.now)
    assert holder.lock()
    try:
        r = env.backup(a)
        assert r.code != 0
        assert "Another Baruch command is running" in r.out
    finally:
        holder.unlock()
