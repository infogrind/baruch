"""On a terminal: colors, symbols and status lines, but the same information.

The same story runs in two identical sandboxes, once into a pipe and once on a
(fake) terminal. Without escape codes, symbols and status lines, the terminal
shows exactly what the pipe gets, except that each synced unit is one line
instead of two."""

import re

from support import Env, pin, spread

from baruch import style

STATUS = re.compile(r"\r\033\[K[^\r\n]*(?=\r)")


def story(env: Env, tty: bool) -> list[str]:
    env.nas.add_show("archive/tv", "Lost", episodes=4, size=20_000)
    env.nas.add_show("archive/tv", "Wire", episodes=2, size=10_000)
    env.nas.add("homes/alex/notes.txt", size=500)
    env.nas.add_show("homes/alex", "Photos", episodes=5, size=5_000)
    env.nas.mark("archive/tv")
    env.nas.mark("homes/alex")
    env.config(
        rules=[
            spread("tv", "archive/tv", ["BK-6A"]),
            pin("home", "homes/alex", "BK-6A"),
        ],
        drives={"BK-6A": "10%"},
    )
    env.ok("init")
    drive = env.register("BK-6A", capacity=200_000)
    outs = [env.run("status", tty=tty).out, env.run("run", tty=tty).out]
    env.drives.dock(drive)
    outs.append(env.run("run", "--dry-run", tty=tty).out)
    outs.append(env.run("run", answers="y\n", tty=tty).out)
    env.nas.delete("homes/alex/Photos/S01E01.mkv")
    env.nas.replace("archive/tv/Lost/S01E02.mkv")
    outs.append(env.run("run", answers="y\n", tty=tty).out)
    env.drives.undock(drive)
    outs.append(env.run("status", tty=tty).out)
    return [o.replace(str(env.tmp), "TMP") for o in outs]


def plain_of(terminal: str) -> str:
    return style.strip(STATUS.sub("", terminal).replace("\r\033[K", ""))


def test_a_terminal_shows_the_same_as_a_pipe(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    # Equal-length paths: they're in files on the drive, and so in free space.
    piped = story(Env(tmp_path / "pipe"), tty=False)
    shown = story(Env(tmp_path / "term"), tty=True)

    assert "\033[" not in "".join(piped) and "\r" not in "".join(piped)
    assert "\033[31m" in shown[4]  # the deletion in red
    for pipe, term in zip(piped, shown, strict=True):
        term = plain_of(term)
        # A synced unit: `[1/3] … copied so far)` + `tv/Lost: done (1m 00s)`
        # into a pipe; `[1/3] 10:12 tv/Lost: copying …: done (1m 00s)` on a
        # terminal.
        pipe_lines = [
            line
            for line in pipe.splitlines()
            if not line.startswith("[") and not re.search(r": done \(", line)
        ]
        term_lines = [line for line in term.splitlines() if not line.startswith("[")]
        assert term_lines == pipe_lines
        units = [line for line in pipe.splitlines() if re.search(r": done \(", line)]
        synced = [line for line in term.splitlines() if line.startswith("[")]
        assert len(units) == len(synced)
        for unit, line in zip(units, synced, strict=True):
            name, _, rest = unit.partition(": done")
            assert f" {name}: " in line and line.endswith(f": done{rest}")


def test_the_plan_file_is_plain_on_a_terminal_too(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    env = Env(tmp_path)
    story(env, tty=True)
    logs = list((env.state_dir / "logs").iterdir())
    assert logs
    for log in logs:
        assert "\033" not in log.read_text()
