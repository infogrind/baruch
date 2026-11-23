"""Interrupted runs, a full disk, records on the drive and in the cache."""

import contextlib
import json

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


def cached(env, drive_name):
    doc = json.loads((env.state_dir / "cache" / f"{drive_name}.json").read_text())
    return {unit: rec[2] for unit, rec in doc["records"].items()}


def test_ctrl_c_records_finished_units_and_the_next_run_continues(env, monkeypatch):
    setup(env)
    a = env.register("BK-6A")
    fail_on_call(monkeypatch, 2, exc=KeyboardInterrupt())

    r = env.backup(a)

    assert r.code == 130
    assert "Interrupted" in r.out
    assert r.lines[-1] == "Safe to eject in DSM."  # end-of-run writes done
    assert env.record(a, "tv/A").result == "ok"
    # Recorded as interrupted on the drive itself, and in the cache.
    assert env.record(a, "tv/B").result == "in-progress"
    assert cached(env, "BK-6A")["tv/B"] == "in-progress"
    assert env.record(a, "tv/C") is None
    assert "BK-6A" not in str(env.registry()["drives"]["BK-6A"]["last_complete_run"])

    monkeypatch.undo()
    r = env.backup(a)
    assert r.code == 0, r.out
    assert env.record(a, "tv/B").result == "ok"
    assert a.read("tv/B/S01E01.mkv") == env.nas.read(f"{TV}/B/S01E01.mkv")
    first_unit = next(line for line in r.lines if line.startswith("tv/"))
    assert first_unit.startswith("tv/B")  # incomplete units go first


def test_a_crash_leaves_the_unit_in_progress_on_the_drive_and_in_the_cache(
    env, monkeypatch
):
    setup(env)
    a = env.register("BK-6A")

    class Crash(BaseException):  # nothing after it runs, like kill -9
        pass

    fail_on_call(monkeypatch, 2, exc=Crash())
    with contextlib.suppress(Crash):
        env.backup(a)

    assert env.record(a, "tv/A").result == "ok"
    assert env.record(a, "tv/B").result == "in-progress"
    assert cached(env, "BK-6A")["tv/B"] == "in-progress"


def test_full_disk_mid_unit_marks_it_incomplete_and_stops(env, monkeypatch):
    setup(env)
    a = env.register("BK-6A")
    fail_on_call(monkeypatch, 2, result=(11, "write failed: No space left on device"))

    r = env.backup(a)

    assert r.code != 0
    assert "No space left" in r.out
    assert env.record(a, "tv/B").result == "in-progress"
    assert env.record(a, "tv/C") is None
    left_out = env.registry()["drives"]["BK-6A"]["left_out"]
    assert left_out["tv/B"] == "interrupted"
    assert left_out["tv/C"].startswith("not reached")


def test_gone_unit_stays_until_prune_removes_its_data(env):
    setup(env, {"A": 30_000})
    a = env.register("BK-6A")
    env.backup(a)
    env.nas.delete(f"{TV}/A")

    r = env.backup(a)

    assert a.exists("tv/A/S01E01.mkv")  # a run never deletes a whole unit
    assert "1 unit of rule tv gone on the NAS" in r.out
    assert "tv/A" in env.registry()["units"]


def test_a_run_with_nothing_to_do_still_records_the_drive(env):
    setup(env, {"A": 30_000})
    a = env.register("BK-6A")
    env.backup(a)
    before = env.registry()["drives"]["BK-6A"]["last_seen"]

    r = env.backup(a)

    assert r.code == 0
    assert "[y/n]" not in r.out
    assert env.registry()["drives"]["BK-6A"]["last_seen"] != before
    assert r.lines[-1] == "Safe to eject in DSM."


def test_only_one_command_at_a_time(env):
    setup(env, {"A": 30_000})
    a = env.register("BK-6A")
    from baruch.registry import Store

    holder = Store(env.state_dir, env.host.now)
    assert holder.lock()
    try:
        r = env.backup(a)
        assert r.code != 0
        assert "Another Baruch command" in r.out
    finally:
        holder.unlock()
