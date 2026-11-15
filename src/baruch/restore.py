"""The restore script on each drive: /.baruch/restore.sh.

It copies everything on the drive back to the NAS without Baruch, using the
rule sources from the configuration at the time of the run. Without `--run`
it only shows what it would copy, and it never deletes anything on the NAS.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from baruch.config import Config, Rule
from baruch.timefmt import show

HEADER = """\
#!/bin/sh
# Restore everything on this drive ({drive}) to the NAS, without Baruch.
#
# Written by Baruch on {when} UTC, from the configuration at that time.
# The targets below are the rule sources then: edit them if the NAS has
# different paths now.
#
#   sh restore.sh          shows what would be copied, copies nothing
#   sh restore.sh --run    copies
#
# Nothing on the NAS is ever deleted, and files there that are newer than the
# drive's version are kept (rsync --update). The dry run lists every file it
# would copy. Uses the rsync on the PATH (any rsync works for this; set
# RSYNC=/path/to/rsync to choose one).

set -e
DRIVE=$(cd "$(dirname "$0")/.." && pwd)
RSYNC=${{RSYNC:-rsync}}
DRY=yes
if [ "$1" = "--run" ]; then DRY=; fi

restore() {{  # rule folder on the drive, target folder on the NAS
    if [ ! -d "$DRIVE/$1" ]; then
        echo "== $1: not on this drive, skipped"
        return 0
    fi
    echo "== $DRIVE/$1/ -> $2/"
    if [ -n "$DRY" ]; then
        "$RSYNC" -a -H --update -n --itemize-changes "$DRIVE/$1/" "$2/"
    else
        mkdir -p "$2"
        "$RSYNC" -a -H --update --stats "$DRIVE/$1/" "$2/"
    fi
}}

if [ -n "$DRY" ]; then
    echo "Dry run: nothing is copied. Start with --run to copy."
fi

"""


def _holds(rule: Rule, path: Path) -> bool:
    try:
        return Path(os.path.realpath(path)).is_relative_to(
            os.path.realpath(rule.source)
        )
    except OSError:
        return False


def order(config: Config, own_files: list[Path]) -> list[Rule]:
    """Config order, but the rule holding Baruch's own files first: restoring
    it also brings back the configuration and the registry."""
    first = [r for r in config.rules if any(_holds(r, p) for p in own_files)]
    return first + [r for r in config.rules if r not in first]


def script(config: Config, drive: str, root: Path, own_files: list[Path], now) -> str:
    lines = [HEADER.format(drive=drive, when=show(now))]
    for rule in order(config, own_files):
        folder = root / rule.name
        try:
            has_data = folder.is_dir() and any(folder.iterdir())
        except OSError:
            has_data = True  # can't tell; the script skips it if it's not there
        if has_data:
            lines.append(f'restore "{rule.name}" {shlex.quote(str(rule.source))}\n')
    lines.append(
        '\nif [ -n "$DRY" ]; then echo "Dry run finished: nothing was copied."; fi\n'
    )
    return "".join(lines)
