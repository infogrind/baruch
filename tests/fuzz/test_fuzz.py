"""Random sequences of what a user (and bad luck) can do, with the lost-data
check after every Baruch command (tests/support.py, Env.run).

Reproducible: by default the sequences are derandomized (the same on every
run and in CI). To explore further:

    BARUCH_FUZZ=explore uv run pytest tests/fuzz -q           # more, random
    uv run pytest tests/fuzz -q --hypothesis-seed=12345        # a given seed

A failure prints the shortest sequence of steps that still fails, as code,
and a `@reproduce_failure(...)` line: paste it above `TestBaruch` to replay
exactly that case while fixing it.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, precondition, rule
from support import Env
from support import rule as rule_conf

import baruch.execute as execute

TV = "archive/tv"
MOVIES = "archive/movies"
SHOWS = ["Lost", "Wire", "Fargo"]
DRIVES = {"BK-6A": 60_000, "BK-6B": 60_000, "BK-20A": 150_000}
TV_LISTS = [
    ["BK-6A", "BK-6B", "BK-20A"],
    ["BK-6B", "BK-6A", "BK-20A"],
    ["BK-6B", "BK-20A"],
    ["BK-6A", "BK-6B"],
]
SIZES = [3_000, 7_000, 12_000, 20_000]

settings.register_profile(
    "ci",
    derandomize=True,
    print_blob=True,
    max_examples=15,
    stateful_step_count=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
settings.register_profile(
    "explore",
    print_blob=True,
    max_examples=200,
    stateful_step_count=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
settings.load_profile(os.environ.get("BARUCH_FUZZ", "ci"))


class Crash(BaseException):
    """A simulated kill -9."""


class Baruch(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.tmp = Path(tempfile.mkdtemp(prefix="baruch-fuzz-"))
        self.env = Env(self.tmp)
        self.disks = {}
        self.tv_list = TV_LISTS[0]
        self.fault: tuple[str, int] | None = None
        self.trace: list[str] = []  # BARUCH_FUZZ_TRACE=1 prints it
        self.state_lost = False  # at most once per sequence
        self.env.nas.mark(TV)
        self.env.nas.add_show(MOVIES, "Alien", episodes=2, size=4_000)
        self.env.nas.mark(MOVIES)
        self._config()
        self.env.ok("init")
        for name, capacity in DRIVES.items():
            self.disks[name] = self.env.register(name, capacity=capacity)

    def teardown(self):
        if os.environ.get("BARUCH_FUZZ_TRACE"):
            print("\n--- sequence:\n  " + "\n  ".join(self.trace))
        self.env.drives.undock_all()
        for p in self.tmp.rglob("*"):
            with contextlib.suppress(OSError):
                if p.is_dir() and not p.is_symlink():
                    p.chmod(0o755)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _config(self):
        self.env.config(
            rules=[
                rule_conf("tv", TV, *self.tv_list),
                rule_conf("movies", MOVIES, "BK-20A"),
            ],
            drives={name: "10%" for name in DRIVES},
        )

    def _show(self, name: str) -> Path:
        return self.env.nas.path(f"{TV}/{name}")

    def _episodes(self, name: str) -> list[str]:
        folder = self._show(name)
        if not folder.is_dir():
            return []
        return sorted(f"{TV}/{name}/{p.name}" for p in folder.iterdir() if p.is_file())

    def _docked(self, drive: str, *argv: str, answers: str = "y"):
        """Dock one drive, run a Baruch command, undock. A pending fault
        applies to this command's rsync calls."""
        disk = self.disks[drive]
        self.env.drives.dock(disk)
        real = execute.run_rsync
        if self.fault:
            kind, n = self.fault
            self.fault = None
            calls = {"n": 0}

            def faulty(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] != n:
                    return real(*args, **kwargs)
                if kind == "crash":
                    raise Crash
                if kind == "interrupt":
                    raise KeyboardInterrupt
                if kind == "full":
                    return 11, "rsync: write failed: No space left on device (28)"
                real(*args, **kwargs)  # "after-copy": the copy, then a crash
                raise Crash

            execute.run_rsync = faulty
        result = "crash"
        try:
            r = self.env.run(*argv, answers=answers)
            result = f"exit {r.code}"
            if "Carry out this plan?" in r.out or "Delete them?" in r.out:
                result += ", asked"
        except Crash:
            pass
        finally:
            execute.run_rsync = real
            self.env.drives.undock(disk)
            self.trace.append(f"{' '.join(argv)} on {drive} [{answers}]: {result}")

    # The NAS

    @rule(
        name=st.sampled_from(SHOWS),
        episodes=st.integers(1, 3),
        size=st.sampled_from(SIZES),
    )
    def add_show(self, name, episodes, size):
        self.trace.append(
            "add_show "
            + repr({k: v for k, v in locals().items() if k not in ("self", "data")})
        )
        if not self._show(name).exists():
            self.env.nas.add_show(TV, name, episodes=episodes, size=size)

    @rule(name=st.sampled_from(SHOWS), size=st.sampled_from(SIZES))
    def add_episode(self, name, size):
        self.trace.append(
            "add_episode "
            + repr({k: v for k, v in locals().items() if k not in ("self", "data")})
        )
        if self._show(name).is_dir():
            n = len(self._episodes(name)) + 1
            self.env.nas.add(f"{TV}/{name}/S02E{n:02d}.mkv", size=size // 3)

    @rule(name=st.sampled_from(SHOWS), data=st.data())
    def replace_episode(self, name, data):
        self.trace.append(
            "replace_episode "
            + repr({k: v for k, v in locals().items() if k not in ("self", "data")})
        )
        eps = self._episodes(name)
        if eps:
            self.env.nas.replace(data.draw(st.sampled_from(eps)))

    @rule(name=st.sampled_from(SHOWS), data=st.data())
    def delete_episode(self, name, data):
        self.trace.append(
            "delete_episode "
            + repr({k: v for k, v in locals().items() if k not in ("self", "data")})
        )
        eps = self._episodes(name)
        if eps:
            self.env.nas.delete(data.draw(st.sampled_from(eps)))

    @rule(name=st.sampled_from(SHOWS))
    def delete_show(self, name):
        self.trace.append(
            "delete_show "
            + repr({k: v for k, v in locals().items() if k not in ("self", "data")})
        )
        if self._show(name).exists():
            self.env.nas.delete(f"{TV}/{name}")

    @rule(old=st.sampled_from(SHOWS), new=st.sampled_from(SHOWS))
    def rename_show(self, old, new):
        self.trace.append(
            "rename_show "
            + repr({k: v for k, v in locals().items() if k not in ("self", "data")})
        )
        if self._show(old).exists() and not self._show(new).exists():
            self.env.nas.rename(f"{TV}/{old}", f"{TV}/{new}")

    @rule(present=st.booleans())
    def set_marker(self, present):
        """The marker missing is how an unmounted volume looks."""
        self.trace.append(f"set_marker {present}")
        marker = self.env.nas.path(f"{TV}/.backup-source")
        if present and not marker.exists():
            self.env.nas.mark(TV)
        elif not present and marker.exists():
            marker.unlink()

    # Baruch

    @rule(drive=st.sampled_from(list(DRIVES)), answer=st.sampled_from(["y", "n"]))
    def run(self, drive, answer):
        self.trace.append(
            "run "
            + repr({k: v for k, v in locals().items() if k not in ("self", "data")})
        )
        self._docked(drive, "run", answers=answer)

    @rule(drive=st.sampled_from(list(DRIVES)), answer=st.sampled_from(["y", "n"]))
    def prune(self, drive, answer):
        self.trace.append(
            "prune "
            + repr({k: v for k, v in locals().items() if k not in ("self", "data")})
        )
        self._docked(drive, "prune", drive, answers=answer)

    @rule(order=st.permutations(list(DRIVES)), answer=st.sampled_from(["y", "n"]))
    def back_up_every_drive(self, order, answer):
        """The usual routine: dock each drive in turn and run."""
        self.trace.append(f"back_up_every_drive {order} {answer}")
        for drive in order:
            self._docked(drive, "run", answers=answer)

    @rule(order=st.permutations(list(DRIVES)))
    def prune_every_drive(self, order):
        self.trace.append(f"prune_every_drive {order}")
        for drive in order:
            self._docked(drive, "prune", drive, answers="y")

    @rule(name=st.sampled_from(SHOWS), drive=st.sampled_from(list(DRIVES)))
    def move_set(self, name, drive):
        self.trace.append(
            "move_set "
            + repr({k: v for k, v in locals().items() if k not in ("self", "data")})
        )
        self.env.run("move", "set", f"tv/{name}", "--to", drive, answers="\n")

    @rule(drive=st.sampled_from(list(DRIVES)))
    def verify(self, drive):
        self.trace.append(
            "verify "
            + repr({k: v for k, v in locals().items() if k not in ("self", "data")})
        )
        self._docked(drive, "verify", answers="y")

    @rule(drive=st.sampled_from(list(DRIVES)))
    def forget_and_reuse(self, drive):
        """The drive is forgotten (as if broken), then the same disk comes
        back and is registered again with --force."""
        self.trace.append(
            "forget_and_reuse "
            + repr({k: v for k, v in locals().items() if k not in ("self", "data")})
        )
        self.env.run("drive", "forget", drive, answers="y")
        self._docked(drive, "drive", "register", drive, "--force", answers="y")

    @rule(which=st.integers(0, len(TV_LISTS) - 1))
    def change_tv_list(self, which):
        self.trace.append(
            "change_tv_list "
            + repr({k: v for k, v in locals().items() if k not in ("self", "data")})
        )
        self.tv_list = TV_LISTS[which]
        self._config()

    @rule(
        kind=st.sampled_from(["crash", "interrupt", "full", "after-copy"]),
        n=st.integers(1, 3),
    )
    def fault_next(self, kind, n):
        self.trace.append(
            "fault_next "
            + repr({k: v for k, v in locals().items() if k not in ("self", "data")})
        )
        self.fault = (kind, n)

    @rule(days=st.sampled_from([1, 40]))
    def time_passes(self, days):
        self.trace.append(
            "time_passes "
            + repr({k: v for k, v in locals().items() if k not in ("self", "data")})
        )
        self.env.host.advance(days=days)

    @precondition(lambda self: (self.env.state_dir / "versions").is_dir())
    @rule(data=st.data())
    def roll_back_to_a_saved_version(self, data):
        """A saved version copied back by hand: an older state, but without
        the cached contents, so nothing unverified is trusted."""
        self.trace.append(
            "roll_back_to_a_saved_version "
            + repr({k: v for k, v in locals().items() if k not in ("self", "data")})
        )
        saved = sorted((self.env.state_dir / "versions").iterdir())
        pick = data.draw(st.sampled_from(saved))
        shutil.copy2(pick / "registry.yaml", self.env.state_dir / "registry.yaml")
        cache = self.env.state_dir / "cache"
        for f in cache.glob("*.json"):
            f.unlink()
        for f in (pick / "cache").glob("*.json"):
            shutil.copy2(f, cache / f.name)

    @precondition(lambda self: not self.state_lost)
    @rule()
    def lose_the_state_and_take_over_every_drive(self):
        """Baruch's state is gone: init, then take every drive over before
        the first run, as documented."""
        self.trace.append(
            "lose_the_state_and_take_over_every_drive "
            + repr({k: v for k, v in locals().items() if k not in ("self", "data")})
        )
        self.state_lost = True
        shutil.rmtree(self.env.state_dir)
        self.env.ok("init")
        for drive in DRIVES:
            self._docked(drive, "drive", "register", drive, answers="y")


TestBaruch = Baruch.TestCase
