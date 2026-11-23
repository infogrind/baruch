"""The status line during scans, and what the scanner reports to it."""

import io
from pathlib import Path

from support import Terminal

from baruch.commands import Out
from baruch.context import Context
from baruch.scan import scan_drive, scan_tree


def out_on(stdout: io.StringIO) -> Out:
    ctx = Context(Path("/c"), Path("/s"), None, io.StringIO(), stdout, True)
    return Out(ctx)


def test_the_status_line_is_overwritten_and_cleared_before_other_output(
    monkeypatch,
):
    monkeypatch.setenv("NO_COLOR", "1")
    term = Terminal()
    out = out_on(term)

    out.status("Scanning tv · Lost", "3 files")
    out.status("Scanning tv · The Wire")  # too soon: skipped
    out("Scanned tv: 2 units")

    assert term.getvalue() == (
        "\r\033[K⠋ Scanning tv · Lost  3 files" + "\r\033[K" + "Scanned tv: 2 units\n"
    )


def test_no_status_line_outside_a_terminal():
    plain = io.StringIO()
    out = out_on(plain)

    out.status("Scanning tv: Lost")
    out("done")

    assert plain.getvalue() == "done\n"


def make(root: Path, rel: str, size: int):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(range(256)) * (size // 256) + b"x" * (size % 256))


def test_the_scanner_reports_each_unit_and_the_running_totals(tmp_path):
    make(tmp_path, "Lost/S01/E01.mkv", 100)
    make(tmp_path, "Lost/S01/E02.mkv", 200)
    make(tmp_path, "Wire/E01.mkv", 50)
    make(tmp_path, "notes.txt", 7)
    seen = []

    scan_tree(tmp_path, [], progress=lambda *a: seen.append(a))

    assert seen[0] == ("Lost", 0, 0)
    assert ("Lost", 2, 300) in seen
    assert ("Wire", 2, 300) in seen  # starting the next unit
    assert seen[-1] == ("(files)", 4, 357)  # notes.txt, after the folders


def test_files_before_the_first_folder_count_as_the_files_unit(tmp_path):
    make(tmp_path, ".backup-source", 0)
    make(tmp_path, "Lost/E01.mkv", 10)
    seen = []

    scan_tree(tmp_path, [], progress=lambda *a: seen.append(a))

    assert seen[0] == ("(files)", 1, 0)


def test_a_drive_scan_counts_across_its_folders(tmp_path):
    make(tmp_path, "tv/Lost/E01.mkv", 100)
    make(tmp_path, "movies/Heat/Heat.mkv", 30)
    seen = []

    scan_drive(tmp_path, [], progress=lambda *a: seen.append(a))

    assert ("movies/Heat", 1, 30) in seen
    assert seen[-1] == ("tv/Lost", 2, 130)
