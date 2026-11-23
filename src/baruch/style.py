"""Colors and a few symbols on a terminal, on top of the plain text.

Output is built as plain lines; a line can carry what it means (`Line`, a str
with a `kind`). `Style.paint` turns that into color and symbols when stdout is
a terminal. It only wraps the text and puts a symbol in front: taking away the
escape codes and the symbols (`strip`) gives back exactly the plain text. Pipes,
files and the plan file get the plain text.
"""

from __future__ import annotations

import os
import re

RESET = "\033[0m"
BOLD, DIM = "1", "2"
RED, GREEN, YELLOW, BLUE, CYAN = "31", "32", "33", "34", "36"

# Kinds of line.
HEAD = "head"  # a section: ==> Plan for BK-6A
DANGER = "danger"  # the deletions section
ATTENTION = "attention"  # a section that needs action: Not complete
ADD, CHANGE, DELETE = "add", "change", "delete"  # + / ~ / - lines
ERROR = "error"  # an error inside a unit: `  ! …`
WARN = "warn"  # WARNING: …
FAIL = "fail"  # the command or the run stopped
OK = "ok"  # a step done
FINAL = "final"  # Safe to eject
NOTE = "note"  # secondary
UNIT = "unit"  # a line starting with a unit's name
TOTAL = "total"
QUESTION = "question"
INFO = "info"  # a drive's facts in `status`

# Symbols and their ASCII forms, for terminals that aren't UTF-8.
SYMBOLS = {"head": ("==>", "==>"), "ok": ("✔", "ok"), "fail": ("✘", "x")}
SYMBOLS["warn"] = ("⚠", "!")
SPINNER = ("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", "|/-\\")

_ANSI = re.compile(r"\033\[[0-9;]*m")
_SYMBOL = re.compile(
    r"^\033\[[0-9;]*m(?:"
    + "|".join(re.escape(s) for pair in SYMBOLS.values() for s in pair)
    + r")\033\[0m ",
    re.MULTILINE,
)
_COMMAND = re.compile(r"`[^`\n]+`")
_NEVER = re.compile(r"(run|sync|verify) never")


class Line(str):
    """A line of plain text and what it means; `bold` is the length of a
    leading name to show in bold."""

    kind: str
    bold: int

    def __new__(cls, text: str, kind: str = "", bold: int = 0) -> Line:
        line = super().__new__(cls, text)
        line.kind, line.bold = kind, bold
        return line


def unit(name: str, rest: str, kind: str = UNIT) -> Line:
    """`name` + `rest`, the name in bold."""
    return Line(name + rest, kind, len(name))


class Style:
    def __init__(self, on: bool, utf8: bool = True):
        self.on = on
        self.utf8 = utf8
        self._frame = 0

    def code(self, codes: str, text: str) -> str:
        if not self.on or not text:
            return text
        start = f"\033[{codes}m"
        # Keep the outer style after an inner reset.
        return start + text.replace(RESET, RESET + start) + RESET

    def symbol(self, name: str, codes: str) -> str:
        return self.code(codes, SYMBOLS[name][0 if self.utf8 else 1]) + " "

    def spinner(self) -> str:
        frames = SPINNER[0 if self.utf8 else 1]
        frame = frames[self._frame % len(frames)]
        self._frame += 1
        return frame

    def paint(self, line: str) -> str:
        if not self.on:
            return str(line)
        kind = getattr(line, "kind", "")
        bold = getattr(line, "bold", 0)
        text = str(line)
        if not kind and (text.startswith("WARNING:") or text.startswith("Warning:")):
            kind = WARN
        if bold:
            text = self.code(BOLD, text[:bold]) + text[bold:]
        if kind != NOTE:
            text = _COMMAND.sub(lambda m: self.code(CYAN, m.group()), text)
        if kind == INFO:
            text = _NEVER.sub(self._never, text)
        match kind:
            case "head":
                return self.symbol("head", f"{BOLD};{BLUE}") + self.code(BOLD, text)
            case "danger":
                return self.symbol("head", f"{BOLD};{RED}") + self.code(
                    f"{BOLD};{RED}", text
                )
            case "attention":
                return self.symbol("head", f"{BOLD};{YELLOW}") + self.code(BOLD, text)
            case "add":
                return self.code(GREEN, text)
            case "change":
                return self.code(YELLOW, text)
            case "delete" | "error":
                return self.code(RED, text)
            case "warn":
                return self.symbol("warn", f"{BOLD};{YELLOW}") + self.code(BOLD, text)
            case "fail":
                return self.symbol("fail", f"{BOLD};{RED}") + self.code(
                    f"{BOLD};{RED}", text
                )
            case "ok":
                return self.symbol("ok", GREEN) + text
            case "final":
                return self.symbol("ok", f"{BOLD};{GREEN}") + self.code(
                    f"{BOLD};{GREEN}", text
                )
            case "note":
                return self.code(DIM, text)
            case "total" | "question":
                return self.code(BOLD, text)
        return text

    def _never(self, m: re.Match) -> str:
        what = m.group(1)
        return f"{what} " + self.code(DIM if what == "verify" else YELLOW, "never")


PLAIN = Style(False)


def _utf8() -> bool:
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = os.environ.get(var)
        if value:
            return "utf-8" in value.lower() or "utf8" in value.lower()
    return True  # unset: macOS and most terminals are UTF-8


def for_stream(stream) -> Style:
    """Styled on a terminal, unless NO_COLOR is set or the terminal is dumb."""
    try:
        tty = stream.isatty()
    except (AttributeError, ValueError):
        tty = False
    on = tty and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
    encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
    utf8 = encoding == "utf8" if encoding else _utf8()
    return Style(on, utf8)


def strip(text: str) -> str:
    """The plain text of styled output."""
    return _ANSI.sub("", _SYMBOL.sub("", text))
