"""The plan as text. The summary, the deletions and, last, the warnings come
right above the question, where the terminal shows them first."""

from __future__ import annotations

import posixpath

from baruch import names, stale
from baruch.planner import FIRST, Plan, UnitOp
from baruch.scan import FILES_UNIT
from baruch.sizes import human
from baruch.timefmt import show_date

MAX_DELETION_LINES = 50


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
        return [f"{name}: first sync: {op.files} files, {human(op.size)}"]
    head = name
    if op.unrecorded:
        head += ": existing folder, no record yet"
    if not op.has_changes:
        lines = [head + (", no changes" if op.unrecorded else ": no changes")]
        return lines + [f"  ! {names.display(e)}" for e in op.errors]
    lines = [head]
    own, excl = set(op.own), set(op.excluded)
    changed, deleted, new = set(op.changed), set(op.delete), set(op.new)
    for rel in op.transfer:
        if rel in own:
            continue
        mark = "~" if rel in changed or rel in deleted else "+"
        lines.append(f"  {mark} {_inner(op, rel)}")
    for rel in op.delete:
        if rel in new or rel in own or rel in excl:
            continue
        lines.append(f"  - {_inner(op, rel)}")
    if excl:
        lines.append(f"  - {len(excl)} excluded names (now matched by an exclude)")
    if own:
        lines.append(f"  ~ Baruch's own state: {len(own)} files")
    for e in op.errors:
        lines.append(f"  ! {names.display(e)}")
    return lines


def render_plan(plan: Plan, where: str) -> str:
    out: list[str] = [f"Plan for {plan.drive} ({where})", ""]
    for op in plan.ops:
        if op.has_changes or op.kind == FIRST or op.unrecorded or op.errors:
            out.extend(unit_lines(op))
    unchanged = sum(
        1
        for op in plan.ops
        if not (op.has_changes or op.kind == FIRST or op.unrecorded or op.errors)
    )
    if not plan.needs_answer:
        out.append("Nothing to do: everything on this drive is up to date.")
    elif unchanged:
        out.append(f"{unchanged} units unchanged.")
    changes = plan.registry_changes()
    if changes:
        out.append("Registry changes:")
        out.extend(f"  {names.display(c)}" for c in changes)
    if plan.skipped:
        out.append("Skipped:")
        out.extend(f"  {names.display(u)}: {why}" for u, why in plan.skipped)
    if plan.unplaced:
        out.append("Unplaced (fit on no drive of their rule):")
        out.extend(f"  {names.display(u)}: {human(s)}" for u, s in plan.unplaced)
    for rule in plan.markers_missing:
        out.append(f"rule {rule} skipped: marker missing (.backup-source)")
    for path, msg in plan.scan_errors:
        out.append(
            f"{names.display(path)} can't be read on the NAS: {msg} (nothing under "
            "it is deleted)"
        )
    for unit, msg in plan.unreadable:
        out.append(
            f"{names.display(unit)}: unreadable on the NAS ({msg}), never taken as gone"
        )
    for path, msg in plan.drive_errors:
        out.append(f"{names.display(path)} can't be read on the drive: {msg}")
    for drive, n, seen in plan.pending_elsewhere:
        out.append(
            f"{n} units copy pending on {drive}, last docked {show_date(seen)}: "
            "`baruch move set UNIT --to DRIVE` gives them a drive that is docked"
        )
    if not plan.needs_answer:
        out.extend(_gone_lines(plan))
    out.extend(stale_lines(plan.stale, plan.drive))
    return "\n".join(out) + "\n"


def _gone_lines(plan: Plan) -> list[str]:
    return [
        f"{len(units)} units of rule {rule} gone on the NAS: nothing is deleted, "
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
            f"Stale data on {drive}: {len(prune)} units, {human(size)} can be pruned "
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
        lines = [f"  {names.display(op.unit)}"]
        for rel in plain:
            note = "replaced in the same folder" if _replaced(op, rel) else ""
            lines.append(f"    - {_inner(op, rel)}" + (f"    {note}" if note else ""))
        if excl:
            lines.append(f"    {len(excl)} excluded names, listed in the plan file")
        own_deleted = own & set(op.delete)
        if own_deleted:
            lines.append(f"    Baruch's own state: {len(own_deleted)} deleted")
        total += len(plain) + len(excl) + len(own_deleted)
        units.append(lines)
    if not units:
        return []
    out = [f"Deletions on {plan.drive} ({total} entries, {len(units)} units):"]
    shown = 0
    for lines in units:
        for line in lines:
            if shown >= MAX_DELETION_LINES:
                rest = sum(len(u) for u in units) - shown
                out.append(f"  … {rest} more lines in the plan file")
                return out
            out.append(line)
            shown += 1
    return out


def render_summary(plan: Plan, log) -> str:
    out = ["", "Summary:"]
    for op in plan.ops:
        if not op.has_changes:
            continue
        out.append(
            f"  {names.display(op.unit)}: copy {len(op.transfer)} "
            f"({human(op.write_bytes)}), overwrite {len(op.changed)} "
            f"({human(op.overwrite_bytes)}), delete {op.delete_files} "
            f"({human(op.delete_bytes)})"
        )
    if log is not None:
        out.append(f"Plan file: {log}")
    out.extend(_gone_lines(plan))
    out.extend(deletion_lines(plan))
    out.extend(plan.warnings)
    return "\n".join(out) + "\n"


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
