"""Colors and symbols: only on a terminal, and never more than decoration."""

import io

from support import Terminal

from baruch import style
from baruch.style import Line, Style
from baruch.timefmt import duration


def test_styling_only_on_a_terminal_without_no_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert style.for_stream(Terminal()).on
    assert not style.for_stream(io.StringIO()).on
    monkeypatch.setenv("NO_COLOR", "1")
    assert not style.for_stream(Terminal()).on
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    assert not style.for_stream(Terminal()).on


def test_ascii_symbols_when_the_terminal_isnt_utf8():
    st = Style(True, utf8=False)
    assert style.strip(st.paint(Line("Read BK-6A", style.OK))) == "Read BK-6A"
    assert "ok" in st.paint(Line("Read BK-6A", style.OK))
    assert "✔" not in st.paint(Line("Read BK-6A", style.OK))


def test_painting_and_stripping_gives_back_the_plain_text():
    st = Style(True)
    lines = [
        Line("Plan for BK-6A (usbshare1)", style.HEAD),
        style.unit("home/Photos", ": first sync: 3 files"),
        Line("  - a/b.jpg", style.DELETE),
        Line("Deletions on BK-6A (1 entry, 1 unit):", style.DANGER),
        "WARNING: 1 of 3 files would be deleted",
        Line("run `baruch prune BK-6A`", style.NOTE),
        Line("see `baruch prune BK-6A`", style.ERROR),
        Line("  last complete run never, last verify never", style.INFO),
        Line("Safe to eject in DSM.", style.FINAL),
    ]
    for line in lines:
        painted = st.paint(line)
        assert style.strip(painted) == line
        assert "\033[" in painted, line
    for line in ["x plain line", ""]:  # untagged: as it is
        assert st.paint(line) == line


def test_plain_style_changes_nothing():
    line = Line("  - a/b.jpg", style.DELETE)
    assert style.PLAIN.paint(line) == "  - a/b.jpg"


def test_durations():
    from datetime import timedelta

    assert duration(timedelta(seconds=4)) == "4s"
    assert duration(timedelta(minutes=2, seconds=51)) == "2m 51s"
    assert duration(timedelta(hours=3, minutes=5)) == "3h 05m"
