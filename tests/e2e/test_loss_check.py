"""The lost-data check that every test's Baruch commands go through.

These make sure it catches what it should: a test that loses data silently
must fail, not pass.
"""

import shutil

import pytest
from support import SilentLoss, spread

TV = "archive/tv"
DRIVES = {"BK-6A": "10%", "BK-6B": "10%"}


def setup(env):
    env.nas.add_show(TV, "Lost", episodes=2, size=25_000)
    env.nas.add_show(TV, "Wire", episodes=2, size=10_000)
    env.nas.mark(TV)
    env.config(rules=[spread("tv", TV, list(DRIVES))], drives=DRIVES)
    env.ok("init")
    return env.register("BK-6A"), env.register("BK-6B")


def test_a_deletion_nobody_was_told_about_is_caught(env, monkeypatch):
    a, b = setup(env)
    env.backup(a)
    env.nas.delete(f"{TV}/Wire")
    import baruch.commands as commands

    real = commands.end_writes

    def sneaky(ctx, out, config, config_text, reg, store, state, nas, now):
        shutil.rmtree(state.docked.path / "tv" / "Wire")  # never in the plan
        return real(ctx, out, config, config_text, reg, store, state, nas, now)

    monkeypatch.setattr(commands, "end_writes", sneaky)
    with pytest.raises(SilentLoss, match="tv/Wire"):
        env.backup(a)


def test_a_change_on_the_nas_is_caught(env, monkeypatch):
    a, _ = setup(env)
    import baruch.commands as commands

    real = commands.scan_nas

    def touching(config, out):
        env.nas.delete(f"{TV}/Wire/S01E01.mkv")
        return real(config, out)

    monkeypatch.setattr(commands, "scan_nas", touching)
    with pytest.raises(SilentLoss, match="changed the NAS"):
        env.backup(a)


def test_the_accepted_risk_of_an_older_state_is_real(env):
    """Documented, not prevented (spec: "Losing the NAS state"): restoring an
    older copy of Baruch's state and pruning before every drive has been
    docked again can delete the last copy. prune's plan names the drive it
    trusts ("also on BK-6B, last seen …") so the user can notice."""
    a, b = setup(env)
    env.backup(a)
    env.ok("move", "set", "tv/Lost", "--to", "BK-6B")
    env.backup(b)
    backup = env.tmp / "state-backup"
    shutil.copytree(env.state_dir, backup)  # what the home backup holds
    env.host.advance(days=20)
    env.ok("drive", "forget", "BK-6B", answers="y")  # BK-6B broke
    shutil.rmtree(b.root)
    shutil.rmtree(env.state_dir)
    shutil.copytree(backup, env.state_dir)  # the older state comes back
    env.nas.delete(f"{TV}/Lost")  # and the NAS lost the show

    env.drives.dock(a)
    with pytest.raises(SilentLoss, match="tv/Lost") as lost:
        env.run("prune", "BK-6A", answers="y")
    assert "also on BK-6B, last seen" in str(lost.value)  # what the user sees
