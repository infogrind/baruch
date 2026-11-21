"""The restore script on each drive: /.baruch/restore.sh.

It copies the drive's data back to the NAS without Baruch, unit by unit, to
the rule sources as configured when it was written:

- by default the units whose home is this drive, if the drive's records say
  their sync finished;
- with `--stale`, in a second pass once every drive's home units are back,
  the stale units `prune` would hold (their data may be the newest or the
  only copy).

Everything else is listed with the command to restore it by hand. It never
deletes on the NAS, keeps newer files there (`rsync --update`) but lists
every one it skips, and never restores the `.backup-source` markers.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from baruch import records, stale
from baruch.config import Config, Rule
from baruch.scan import FILES_UNIT
from baruch.timefmt import show

SAFE = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 ._-/()+,=@")


def fmt(text: str) -> str:
    """A name as a `printf %b` argument in single quotes: every byte outside a
    plain set as an octal escape, so plain `sh` gets the exact bytes back."""
    raw = os.fsencode(text)
    out = "".join(chr(b) if b in SAFE else f"\\0{b:03o}" for b in raw)
    return f"'{out}'"


HEADER = r"""#!/bin/sh
# Restore this drive (__DRIVE__) to the NAS, without Baruch.
#
# Written by Baruch on __WHEN__ UTC. The targets are the rule sources as they
# were configured then: edit them below if the NAS has different paths now.
#
#   sh restore.sh                  shows what would be copied, copies nothing
#   sh restore.sh --run            copies this drive's home units
#   sh restore.sh --run --stale    second pass, once every drive's home units
#                                  are back: the other units listed below
#   --overwrite                    also copy files where the NAS has a newer one
#
# Nothing on the NAS is ever deleted. Files there that are newer than the
# drive's are kept (rsync --update) and listed. The .backup-source markers are
# never restored: re-create them once every drive is restored.

set -u
DRIVE=$(cd "$(dirname "$0")/.." && pwd)
RECORDS="$DRIVE/.baruch/units.yaml"
DRY=yes
STALE=
UPDATE=--update
for arg in "$@"; do
    case "$arg" in
        --run) DRY= ;;
        --stale) STALE=yes ;;
        --overwrite) UPDATE= ;;
        *) echo "unknown option: $arg"; exit 2 ;;
    esac
done

RSYNC=
for p in /home/linuxbrew/.linuxbrew/opt/rsync/bin/rsync \
         /opt/homebrew/opt/rsync/bin/rsync /usr/local/opt/rsync/bin/rsync; do
    if [ -x "$p" ]; then RSYNC=$p; break; fi
done
[ -n "$RSYNC" ] || RSYNC=$(command -v rsync || true)
if [ -z "$RSYNC" ]; then echo "rsync not found: install it (brew install rsync)"; exit 1; fi
V=$("$RSYNC" --version 2>/dev/null | head -n 1)
case "$V" in
    *openrsync*) echo "$RSYNC is openrsync: install rsync (brew install rsync)"; exit 1 ;;
esac
MAJOR=$(echo "$V" | sed -n 's/.*version \([0-9]*\)\.\([0-9]*\).*/\1/p')
MINOR=$(echo "$V" | sed -n 's/.*version \([0-9]*\)\.\([0-9]*\).*/\2/p')
if [ -z "$MAJOR" ] || [ "$MAJOR" -lt 3 ] || { [ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 2 ]; }; then
    echo "$RSYNC is older than rsync 3.2: install it (brew install rsync)"; exit 1
fi

RECORDS_OK=yes
if [ ! -r "$RECORDS" ]; then
    RECORDS_OK=
    echo "!! $RECORDS can't be read: nothing is restored by itself; everything"
    echo "!! is listed below with the command to restore it by hand."
fi
SKIPPED=0
LISTED=0

complete() {  # the record says the unit's sync finished
    [ -n "$RECORDS_OK" ] || return 1
    K=$1 awk 'index($0, ENVIRON["K"] ": {") == 1 && /result: (ok|errors)\}$/ {f=1} END {exit !f}' "$RECORDS"
}
recorded() {
    [ -n "$RECORDS_OK" ] || return 1
    K=$1 awk 'index($0, ENVIRON["K"] ": {") == 1 {f=1} END {exit !f}' "$RECORDS"
}
synced() {
    K=$1 awk 'index($0, ENVIRON["K"] ": {") == 1 {sub(/.*synced: /, ""); sub(/,.*/, ""); print}' "$RECORDS" 2>/dev/null
}
target_ok() {  # the rule's source folder exists, or can be made one level deep
    if [ -d "$1" ]; then return 0; fi
    parent=$(dirname "$1")
    if [ ! -d "$parent" ]; then
        echo "!! $1: its parent $parent doesn't exist (share not set up or volume"
        echo "!! not mounted): skipped"
        return 1
    fi
    if [ -n "$DRY" ]; then return 0; fi
    mkdir "$1"
}
copy() {  # source on the drive, target on the NAS, [files-unit]
    if [ -n "${3:-}" ]; then
        set -- "$1" "$2" --exclude=/*/ --exclude=/.backup-source
    else
        set -- "$1" "$2"
    fi
    src=$1; dst=$2; shift 2
    out=$(mktemp)
    if [ -n "$DRY" ]; then
        "$RSYNC" -aH $UPDATE -n --itemize-changes --info=skip1 "$@" "$src" "$dst" >"$out" 2>&1
    else
        "$RSYNC" -aH $UPDATE --info=skip1 "$@" "$src" "$dst" >"$out" 2>&1
    fi
    grep -v ' is newer$' "$out" || true
    n=$(grep -c ' is newer$' "$out" || true)
    if [ "$n" -gt 0 ]; then
        echo "   kept $n newer files on the NAS (copy them too with --overwrite):"
        grep ' is newer$' "$out" | sed 's/^/     /'
        SKIPPED=$((SKIPPED + n))
    fi
    rm -f "$out"
}
by_hand() {  # source, target, files-unit
    if [ -n "${3:-}" ]; then
        echo "     by hand: $RSYNC -aH --exclude='/*/' --exclude=/.backup-source '$1' '$2'"
    else
        echo "     by hand: $RSYNC -aH '$1' '$2'"
    fi
}
unit() {  # kind key name rule folder source [reason]
    kind=$1; key=$2; name=$(printf '%b' "$3"); rule=$(printf '%b' "$4")
    folder=$(printf '%b' "$5"); source=$(printf '%b' "$6"); reason=${7:-}
    if [ "$folder" = "(files)" ]; then
        src="$DRIVE/$rule/"; dst="$source/"; files=yes
    else
        src="$DRIVE/$rule/$folder/"; dst="$source/$folder/"; files=
    fi
    if [ ! -e "$DRIVE/$rule" ]; then return 0; fi
    if [ "$kind" = stale ] && [ -z "$STALE" ]; then
        echo "-- $name: second pass (--stale): $reason"
        LISTED=$((LISTED + 1)); return 0
    fi
    if [ "$kind" = home ] && [ -n "$STALE" ]; then return 0; fi
    if ! complete "$key"; then
        if recorded "$key"; then
            echo "-- $name: not restored: its last sync didn't finish; an older"
            echo "   complete copy may be on another drive"
        else
            echo "-- $name: not restored: existing folder, no record yet (may be the"
            echo "   only copy)"
        fi
        by_hand "$src" "$dst" "$files"
        LISTED=$((LISTED + 1)); return 0
    fi
    target_ok "$source" || return 0
    echo "== $name (synced $(synced "$key")) -> $dst ${reason:+($reason)}"
    copy "$src" "$dst" "$files"
}
note() {  # path reason
    echo "-- $(printf '%b' "$1"): not restored: $2"
    LISTED=$((LISTED + 1))
}

if [ -n "$DRY" ]; then echo "Dry run: nothing is copied. Add --run to copy."; fi

"""

FOOTER = r"""
NEWER=$(find "$DRIVE" -mindepth 1 -maxdepth 2 -newer "$0" ! -path "$DRIVE/.baruch*" \
    ! -name lost+found 2>/dev/null)
if [ -n "$NEWER" ]; then
    echo "-- added after this script was written, not restored:"
    echo "$NEWER" | sed 's/^/     /'
fi
echo
[ "$SKIPPED" -eq 0 ] || echo "$SKIPPED files kept on the NAS because they're newer (listed above)."
[ "$LISTED" -eq 0 ] || echo "$LISTED items listed, not restored (see above)."
if [ -n "$DRY" ]; then echo "Dry run finished: nothing was copied."; fi
echo "Once every drive is restored, re-create the markers:"
"""


def _holds(rule: Rule, path: Path) -> bool:
    try:
        return Path(os.path.realpath(path)).is_relative_to(
            os.path.realpath(rule.source)
        )
    except OSError:
        return False


def order(config: Config, own_files: list[Path]) -> list[Rule]:
    """Config order, but the rule holding Baruch's own files first."""
    first = [r for r in config.rules if any(_holds(r, p) for p in own_files)]
    return first + [r for r in config.rules if r not in first]


def script(
    *,
    config: Config,
    drive: str,
    home_units: list[str],
    items: list[stale.Item],
    own_files: list[Path],
    now,
) -> str:
    """`home_units`: units whose home is this drive; `items`: its stale and
    unexplained data, as `stale.classify` sees it now."""
    rules = {r.name: r for r in config.rules}
    second = {i.unit: i for i in stale.for_restore(items)}
    lines = [HEADER.replace("__DRIVE__", drive).replace("__WHEN__", show(now))]
    for rule in order(config, own_files):
        for u in sorted(home_units):
            if u.split("/", 1)[0] == rule.name:
                lines.append(_unit_line("home", u, rule, ""))
    for rule in order(config, own_files):
        for u, item in sorted(second.items()):
            if u.split("/", 1)[0] == rule.name:
                why = f"{item.reason}; its home is {item.home or 'none'}"
                lines.append(_unit_line("stale", u, rule, why))
    for item in items:
        if item.unit in second:
            continue
        if item.unit and item.unit.split("/", 1)[0] not in rules:
            continue
        lines.append(f"note {fmt(item.path)} {shlex.quote(item.reason)}\n")
    lines.append(FOOTER)
    for rule in config.rules:
        lines.append(
            f"echo {shlex.quote('  touch ' + shlex.quote(str(rule.source / '.backup-source')))}\n"
        )
    return "".join(lines)


def _unit_line(kind: str, unit: str, rule: Rule, reason: str) -> str:
    folder = unit.split("/", 1)[1]
    return (
        f"unit {kind} {shlex.quote(records.key(unit))} {fmt(unit)} "
        f"{fmt(rule.name)} {fmt(folder)} {fmt(str(rule.source))} "
        f"{shlex.quote(reason)}\n"
    )


__all__ = ["FILES_UNIT", "script", "order", "fmt"]
