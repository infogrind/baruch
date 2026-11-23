"""The plan as text. The summary, the deletions and, last, the warnings come
right above the question, where the terminal shows them first.

Lines are `style.Line`s: plain text that knows what it means, for colors on a
terminal (`Out.lines`). Joined with `text`, they're the plan file."""

from __future__ import annotations

import posixpath
from datetime import UTC, datetime

from baruch import names, stale
from baruch.planner import FIRST, Plan, UnitOp
from baruch.scan import FILES_UNIT
from baruch.sizes import count, human
from baruch.style import (
    ADD,
    ATTENTION,
    CHANGE,
    DANGER,
    DELETE,
    ERROR,
    HEAD,
    NOTE,
    TOTAL,
    Line,
    unit,
)
from baruch.timefmt import show_date

MAX_DELETION_LINES = 50

MARKS = {"+": ADD, "~": CHANGE, "-": DELETE, "!": ERROR}


def text(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def item(indent: str, mark: str, rest: str) -> Line:
    """A `  + path` line under a unit."""
    return Line(f"{indent}{mark} {rest}", MARKS[mark])


def q(unit: str) -> str:
    return '"' + names.display(unit).replace('"', '\\"') + '"'


def _inner(op: UnitOp, rel: str) -> str:
    """A path inside its unit, for listing under the unit's name."""
    folder = op.unit.split("/", 1)[1]
    if folder != FILES_UNIT and rel.startswith(folder + "/"):
        rel = rel[len(folder) + 1 :]
    return names.display(rel)


def unit_lines(op: UnitOp) -> list[str]:
    name = names.display(op.unit)
    if op.kind == FIRST:
        return [
            unit(name, f": first sync: {count(op.files, 'file')}, {human(op.size)}")
        ]
    rest = ""
    if op.unrecorded:
        rest += ": existing folder, no record yet"
    if not op.has_changes:
        lines = [
            unit(name, rest + (", no changes" if op.unrecorded else ": no changes"))
        ]
        return lines + [item("  ", "!", names.display(e)) for e in op.errors]
    lines: list[str] = [unit(name, rest)]
    own, excl = set(op.own), set(op.excluded)
    changed, deleted, new = set(op.changed), set(op.delete), set(op.new)
    for rel in op.transfer:
        if rel in own:
            continue
        mark = "~" if rel in changed or rel in deleted else "+"
        lines.append(item("  ", mark, _inner(op, rel)))
    for rel in op.delete:
        if rel in new or rel in own or rel in excl:
            continue
        lines.append(item("  ", "-", _inner(op, rel)))
    if excl:
        lines.append(
            item("  ", "-", f"{len(excl)} excluded names (now matched by an exclude)")
        )
    if own:
        lines.append(item("  ", "~", f"Baruch's own state: {count(len(own), 'file')}"))
    for e in op.errors:
        lines.append(item("  ", "!", names.display(e)))
    return lines


def render_plan(plan: Plan, where: str) -> list[str]:
    out: list[str] = [Line(f"Plan for {plan.drive} ({where})", HEAD), ""]
    for op in plan.ops:
        if op.has_changes or op.kind == FIRST or op.unrecorded or op.errors:
            out.extend(unit_lines(op))
    unchanged = sum(
        1
        for op in plan.ops
        if not (op.has_changes or op.kind == FIRST or op.unrecorded or op.errors)
    )
    problems = (
        plan.skipped
        or plan.drive_errors
        or plan.scan_errors
        or plan.unreadable
        or plan.markers_missing
        or plan.unplaced
    )
    if not plan.needs_answer and not problems:
        out.append("Nothing to do: everything on this drive is up to date.")
    elif not plan.needs_answer:
        out.append("No files change on this drive.")
    elif unchanged:
        out.append(Line(f"{count(unchanged, 'unit')} unchanged.", NOTE))
    changes = plan.registry_changes()
    if changes:
        out.extend(["", Line("Registry changes:", HEAD)])
        out.extend(f"  {names.display(c)}" for c in changes)
    if plan.skipped:
        out.extend(["", Line("Skipped:", ATTENTION)])
        out.extend(f"  {names.display(u)}: {why}" for u, why in plan.skipped)
    if plan.unplaced:
        out.extend(["", Line("Unplaced (fit on no drive of their rule):", ATTENTION)])
        out.extend(f"  {names.display(u)}: {human(s)}" for u, s in plan.unplaced)
    for rule in plan.markers_missing:
        out.append(
            Line(f"rule {rule} skipped: marker missing (.backup-source)", CHANGE)
        )
    for path, msg in plan.scan_errors:
        out.append(
            Line(
                f"{names.display(path)} can't be read on the NAS: {msg} (nothing "
                "under it is deleted)",
                ERROR,
            )
        )
    for u, msg in plan.unreadable:
        out.append(
            Line(
                f"{names.display(u)}: unreadable on the NAS ({msg}); left as it is",
                ERROR,
            )
        )
    for path, msg in plan.drive_errors:
        out.append(
            Line(f"{names.display(path)} can't be read on the drive: {msg}", ERROR)
        )
    for drive, n, seen in plan.pending_elsewhere:
        out.append(
            f"{count(n, 'unit')} copy pending on {drive}, last docked "
            f"{show_date(seen)}: "
            "`baruch move set UNIT --to DRIVE` gives them a drive that is docked"
        )
    if not plan.needs_answer:
        out.extend(_gone_lines(plan))
    out.extend(stale_lines(plan.stale, plan.drive))
    return out


def _gone_lines(plan: Plan) -> list[str]:
    return [
        f"{count(len(units), 'unit')} of rule {rule} gone on the NAS: nothing is "
        "deleted, "
        "see the plan"
        for rule, units in sorted(plan.gone.items())
    ]


def stale_lines(items: list[stale.Item], drive: str) -> list[str]:
    """Stale and unexplained data on a drive, as the end of a run reports it."""
    prune = [i for i in items if i.action == stale.PRUNE]
    hold = [i for i in items if i.action == stale.HOLD]
    unexplained = [i for i in items if i.action == stale.UNEXPLAINED]
    out = []
    if prune or hold:
        size = sum(i.size for i in prune)
        out.append(
            f"Stale data on {drive}: {count(len(prune), 'unit')}, {human(size)} can be "
            "pruned "
            f"(`baruch prune {drive}`); {len(hold)} held"
        )
        for i in hold:
            out.append(f"  held: {names.display(i.path)}: {i.reason}")
    if unexplained:
        out.append(
            f"Unexplained data on {drive} (never deleted by Baruch; `baruch prune "
            f"{drive}` shows the commands to delete it by hand):"
        )
        for i in unexplained:
            out.append(f"  {names.display(i.path)}: {i.reason}, {human(i.size)}")
    return out


def _replaced(op: UnitOp, rel: str) -> bool:
    """A deleted file whose folder gets a new file in the same plan."""
    folder = posixpath.dirname(rel)
    return any(posixpath.dirname(n) == folder for n in op.new if n != rel)


def deletion_lines(plan: Plan) -> list[str]:
    units = []
    total = 0
    for op in plan.ops:
        own, excl, new = set(op.own), set(op.excluded), set(op.new)
        plain = [
            r for r in op.delete if r not in own and r not in excl and r not in new
        ]
        if not (plain or excl or own & set(op.delete)):
            continue
        lines: list[str] = [unit(f"  {names.display(op.unit)}", "")]
        for rel in plain:
            note = "    replaced in the same folder" if _replaced(op, rel) else ""
            lines.append(item("    ", "-", _inner(op, rel) + note))
        if excl:
            lines.append(f"    {len(excl)} excluded names, listed in the plan file")
        own_deleted = own & set(op.delete)
        if own_deleted:
            lines.append(f"    Baruch's own state: {len(own_deleted)} deleted")
        total += len(plain) + len(excl) + len(own_deleted)
        units.append(lines)
    if not units:
        return []
    out: list[str] = [
        "",
        Line(
            f"Deletions on {plan.drive} ({count(total, 'entry', 'entries')}, "
            f"{count(len(units), 'unit')}):",
            DANGER,
        ),
    ]
    shown = 0
    for lines in units:
        for line in lines:
            if shown >= MAX_DELETION_LINES:
                rest = sum(len(u) for u in units) - shown
                out.append(Line(f"  … {rest} more lines in the plan file", TOTAL))
                return out
            out.append(line)
            shown += 1
    return out


def _amounts(copy, write, overwrite, over_bytes, delete, del_bytes, full=False):
    parts = [
        f"copy {copy} ({human(write)})" if copy or full else "",
        f"overwrite {overwrite} ({human(over_bytes)})" if overwrite or full else "",
        f"delete {delete} ({human(del_bytes)})" if delete or full else "",
    ]
    return ", ".join(p for p in parts if p) or f"copy 0 ({human(0)})"


def render_summary(plan: Plan, log) -> list[str]:
    out: list[str] = ["", Line("Summary:", HEAD)]
    busy = [op for op in plan.ops if op.has_changes]
    for op in busy:
        amounts = _amounts(
            op.copy_files,
            op.write_bytes,
            len(op.changed),
            op.overwrite_bytes,
            op.delete_files,
            op.delete_bytes,
        )
        out.append(unit(f"  {names.display(op.unit)}", f": {amounts}"))
    if busy:
        total = _amounts(
            sum(op.copy_files for op in busy),
            sum(op.write_bytes for op in busy),
            sum(len(op.changed) for op in busy),
            sum(op.overwrite_bytes for op in busy),
            sum(op.delete_files for op in busy),
            sum(op.delete_bytes for op in busy),
            full=True,
        )
        out.append(Line(f"  Total: {total}", TOTAL))
    if log is not None:
        out.append(Line(f"Plan file: {log}", NOTE))
    out.extend(_gone_lines(plan))
    out.extend(deletion_lines(plan))
    if plan.warnings:
        out.append("")
        out.extend(plan.warnings)
    return out


def reserve_lines(plan: Plan) -> list[str]:
    if not plan.reserve_used:
        return []
    out = [
        f"{plan.drive}: existing units have grown into the reserve: "
        f"{human(plan.reserve_used)} of {human(plan.reserve)} used."
    ]
    if plan.largest:
        out.append(f"  Largest units on {plan.drive}:")
        for unit, size, newest in plan.largest:
            out.append(f"    {names.display(unit)}  {human(size)}  {_age(newest)}")
    for rule, rooms in sorted(plan.room.items()):
        if rooms:
            listed = ", ".join(f"{d} {human(r)}" for d, r in rooms)
            out.append(f"  Room above reserve in rule {rule}: {listed}")
    out.append("  To move one: baruch move set UNIT --to DRIVE")
    return out


def _age(mtime: int) -> str:
    from datetime import UTC, datetime

    if not mtime:
        return ""
    return "changed " + datetime.fromtimestamp(mtime, UTC).strftime("%Y-%m-%d")


def unit_start(i: int, n: int, op: UnitOp, now: datetime) -> str:
    """`[3/12] 20:31 home/Photos: copying 1204 files, 48.2 GiB`"""
    what = []
    if op.transfer:
        what.append(f"copying {count(op.copy_files, 'file')}, {human(op.write_bytes)}")
    if op.delete:
        what.append(f"deleting {count(op.delete_files, 'file')}")
    return (
        f"[{i}/{n}] {now.astimezone(UTC).strftime('%H:%M')} "
        f"{names.display(op.unit)}: {', '.join(what)}"
    )


def start_line(i: int, n: int, op: UnitOp, done: int, total: int, now: datetime) -> str:
    """The line printed as a unit's sync starts: where the run stands."""
    return (
        f"{unit_start(i, n, op, now)} ({human(done)} of {human(total)} copied so far)"
    )
