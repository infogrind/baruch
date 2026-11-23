"""A persistent demo sandbox: the test suite's fake NAS and drives in a folder,
with the real CLI run against them (real rsync, real terminal).

    uv run demo/sandbox.py setup DIR       # NAS data, config, three drives
    uv run demo/sandbox.py dock DIR BK-6A  # dock a drive (undocks the others)
    uv run demo/sandbox.py undock DIR
    uv run demo/sandbox.py change DIR      # new, changed and deleted files
    uv run demo/sandbox.py baruch DIR ARGS # run baruch in the sandbox
    uv run demo/sandbox.py story DIR       # setup + a fixed sequence, printed

`demo/baruch` runs the `baruch` step with `$BARUCH_DEMO` as DIR. Sizes are
small (MiB, not TiB) but real: every file is written and copied.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from baruch.cli import main  # noqa: E402
from baruch.context import Context  # noqa: E402
from tests.support import Env, pin, spread  # noqa: E402

MiB = 1024**2
DRIVES = {"BK-6A": 200 * MiB, "BK-6B": 200 * MiB, "BK-20A": 900 * MiB}
STATE = "sandbox.json"

SHOWS = {  # name: (episodes, MiB)
    "The Wire": (60, 120),
    "Lost": (120, 110),
    "Breaking Bad": (62, 95),
    "Deadwood": (36, 60),
    "Twin Peaks": (48, 70),
    "The Expanse": (62, 80),
    "Fargo": (51, 55),
    "Columbo": (69, 40),
}
MOVIES = ["Heat (1995)", "Alien (1979)", "Arrival (2016)", "Ran (1985)", "Ronin (1998)"]


def load(root: Path) -> Env:
    env = Env(root)
    state = json.loads((root / STATE).read_text())
    for name in DRIVES:
        env.drives.new(name, DRIVES[name])
    env.host.clock = datetime.fromisoformat(state["clock"])
    env.nas.versions = state["versions"]
    env.check_loss = False
    for slot in sorted(env.host.usb.iterdir()):
        env.drives.slots[str(slot.resolve())] = slot
    return env


def save(env: Env) -> None:
    state = {"clock": env.host.clock.isoformat(), "versions": env.nas.versions}
    (env.tmp / STATE).write_text(json.dumps(state, indent=1))


def setup(root: Path) -> Env:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    env = Env(root)
    env.check_loss = False
    tv, movies = "archive/tv", "archive/movies"
    for name, (episodes, mib) in SHOWS.items():
        env.nas.add_show(tv, name, episodes=episodes, size=mib * MiB)
    for i, name in enumerate(MOVIES):
        env.nas.add(f"{movies}/{name}/{name}.mkv", size=(25 + 7 * i) * MiB)
        env.nas.add(f"{movies}/{name}/{name}.srt", size=40_000)
    home = "homes/alex"
    for year in range(2015, 2027):
        for n in range(1, 151):
            env.nas.add(f"{home}/Photos/{year}/IMG_{n:04d}.jpg", size=60_000)
    for n in range(1, 401):
        env.nas.add(f"{home}/Documents/letter-{n:03d}.txt", size=3_000)
    env.nas.add(f"{home}/todo.txt", size=500)
    for p in ("website", "thesis"):
        for n in range(1, 301):
            env.nas.add(f"projects/{p}/src/file{n:03d}.txt", size=8_000)
    for source in (tv, movies, home, "projects"):
        env.nas.mark(source)
    env.config(
        rules=[
            pin("home", home, "BK-20A"),
            pin("projects", "projects", "BK-20A"),
            pin("movies", movies, "BK-20A"),
            spread("tv", tv, ["BK-6A", "BK-6B", "BK-20A"]),
        ],
        drives={name: "5%" for name in DRIVES},
    )
    env.ok("init")
    for name, capacity in DRIVES.items():
        env.register(name, capacity=capacity)
    save(env)
    return env


def dock(env: Env, name: str) -> None:
    env.drives.undock_all()
    env.drives.dock(env.drives.new(name, DRIVES[name]))


def change(env: Env) -> None:
    env.host.advance(days=3)
    env.nas.add_show("archive/tv", "Severance", episodes=19, size=45 * MiB)
    for n in range(1, 41):
        env.nas.add(f"homes/alex/Photos/2026/IMG_{150 + n:04d}.jpg", size=60_000)
    for n in range(1, 6):
        env.nas.replace(f"homes/alex/Documents/letter-{n:03d}.txt", size=3_500)
    env.nas.delete("projects/thesis/src/file300.txt")


def run(env: Env, argv: list[str], answers: str | None = None) -> int:
    ctx = Context(
        config_dir=env.config_dir,
        state_dir=env.state_dir,
        host=env.host,
        stdin=sys.stdin if answers is None else io.StringIO(answers),
        stdout=sys.stdout,
        interactive=True,
    )
    try:
        return main(argv, ctx)
    finally:
        sys.stdout.flush()


def story(root: Path) -> None:
    def say(text: str) -> None:
        print(f"\n### {text}", flush=True)

    def baruch(*argv: str, answers: str | None = None) -> None:
        print(f"$ baruch {' '.join(argv)}", flush=True)
        code = run(env, list(argv), answers or "")
        if code:
            print(f"(exit code {code})")

    env = setup(root)
    say("No drive docked")
    baruch("status")
    say("First run on BK-20A (the big drive)")
    dock(env, "BK-20A")
    baruch("run", "--dry-run")
    baruch("run", answers="y\n")
    say("First run on BK-6A")
    dock(env, "BK-6A")
    baruch("run", answers="y\n")
    env.drives.undock_all()
    say("Three days later, files changed on the NAS; BK-20A docked again")
    change(env)
    dock(env, "BK-20A")
    baruch("run", answers="y\n")
    say("Status afterwards")
    baruch("status")
    save(env)


def cli(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="sandbox", description=__doc__.split("\n")[0])
    steps = ["setup", "dock", "undock", "change", "baruch", "story"]
    p.add_argument("step", choices=steps)
    p.add_argument("dir", type=Path)
    p.add_argument("args", nargs=argparse.REMAINDER)
    a = p.parse_args(argv)
    root = a.dir.resolve()
    if a.step == "setup":
        setup(root)
        print(f"Sandbox ready in {root}. Try: BARUCH_DEMO={root} demo/baruch status")
        return 0
    if a.step == "story":
        story(root)
        return 0
    env = load(root)
    code = 0
    if a.step == "dock":
        dock(env, a.args[0])
    elif a.step == "undock":
        env.drives.undock_all()
    elif a.step == "change":
        change(env)
    else:
        code = run(env, a.args)
    save(env)
    return code


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
