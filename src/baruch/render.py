"""The plan as text. Warnings and the summary come last, right above the
question, where the terminal shows them first."""

from __future__ import annotations

from baruch import names
from baruch.planner import CLEANUP, FIRST, GONE, UNTRACKED, Plan, UnitOp
from baruch.registry import Registry
from baruch.sizes import human
from baruch.timefmt import show_date


def q(unit: str) -> str:
    return '"' + names.display(unit).replace('"', '\\"') + '"'


def unit_lines(op: UnitOp) -> list[str]:
    name = names.display(op.unit)
    if op.kind == FIRST:
        return [f"{name}: first copy: {op.files} files, {human(op.size)}"]
    if op.kind == CLEANUP:
        return [f"{name}: cleanup ({op.note}): delete {human(op.delete_bytes)}"]
    if op.kind == GONE:
        head = f"{name}: gone on the NAS: delete {op.delete_files} files, "
        return [head + human(op.delete_bytes)]
    head = name
    if op.kind == UNTRACKED:
        head += ": existing folder, not yet recorded"
        if not (op.transfer or op.delete):
            head += ", no changes"
    lines = [head]
    changed = set(op.changed)
    deleted = set(op.delete)
    for rel in op.transfer:
        mark = "~" if rel in changed or rel in deleted else "+"
        lines.append(f"  {mark} {names.display(rel)}")
    for rel in op.delete:
        if rel in set(op.new):
            continue  # replaced by another kind; shown as ~
        lines.append(f"  - {names.display(rel)}")
    return lines


def render_plan(plan: Plan, reg: Registry, where: str) -> str:
    out: list[str] = [f"Plan for {plan.drive} ({where})", ""]
    for op in plan.ops:
        out.extend(unit_lines(op))
    if plan.empty:
        out.append("Nothing to do: everything on this drive is up to date.")

    for rule in plan.markers_missing:
        out.append(
            f"{rule.name}: marker .backup-source missing in {rule.source}: rule "
            "skipped, nothing on the drive is touched for it"
        )
    for unit, msg in plan.unreadable:
        out.append(
            f"{names.display(unit)}: unreadable on the NAS ({msg}): skipped, its "
            "copies stay"
        )
    if plan.scan_errors:
        out.append("Scan errors on the NAS (nothing under these is deleted):")
        for path, msg in plan.scan_errors:
            out.append(f"  {names.display(path)}: can't be read ({msg})")
    for unit, why in plan.skipped:
        out.append(f"{names.display(unit)}: {why}")
    for w in plan.waiting:
        last = reg.drives.get(w.drive)
        when = show_date(last.last_seen) if last else "never"
        out.append(
            f"{names.display(w.unit)}: new unit waiting for {w.drive} "
            f"(last docked {when}), {human(w.size)}"
        )
        if w.here:
            out.append(
                f"  to place it here instead: baruch move set {q(w.unit)} "
                f"--to {plan.drive}"
            )
    for unit, size in plan.nowhere:
        out.append(
            f"{names.display(unit)}: fits on no drive ({human(size)}), no backup"
        )
    if plan.orphans:
        out.append(f"Orphaned data (only `baruch prune {plan.drive}` removes it):")
        for o in plan.orphans:
            out.append(f"  {names.display(o.path)}: {o.note} ({human(o.size)})")
    if plan.reserve_used:
        out.append(reserve_line(plan.reserve_used, plan.reserve))
        for s in plan.suggestions:
            out.append(suggestion_line(s))
    return "\n".join(out) + "\n"


def reserve_line(used: int, reserve: int) -> str:
    share = used / reserve if reserve else 1.0
    return (
        f"Existing units have grown into the reserve: {share:.0%} of the reserve "
        f"used ({human(used)} of {human(reserve)}). Suggested moves:"
    )


def suggestion_line(s) -> str:
    from datetime import UTC, datetime

    since = datetime.fromtimestamp(s.newest, UTC).strftime("%Y-%m") if s.newest else "?"
    return (
        f"  baruch move set {q(s.unit)} --to {s.to}   "
        f"# {human(s.size)}, unchanged since {since}"
    )


def render_summary(plan: Plan) -> str:
    out = ["", "Summary:"]
    for op in plan.ops:
        parts = []
        if op.transfer:
            parts.append(f"copy {op.copy_files} ({human(op.write_bytes)})")
        if op.changed:
            parts.append(f"overwrite {len(op.changed)} ({human(op.overwrite_bytes)})")
        if op.delete:
            parts.append(f"delete {op.delete_files} ({human(op.delete_bytes)})")
        out.append(f"  {names.display(op.unit)}: {', '.join(parts) or 'record only'}")
    return "\n".join(out) + "\n"
