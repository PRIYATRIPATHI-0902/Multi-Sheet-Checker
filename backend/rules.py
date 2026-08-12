"""
rules.py
Deterministic balloon <-> parts list cross-checks.

These run in plain Python so the numeric findings are exact and repeatable.
The AI pass afterwards only explains them and looks for the softer problems
that arithmetic cannot see.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from extractor import Sheet

ERROR, WARNING, INFO = "error", "warning", "info"


def _target(kind: str, bbox) -> dict[str, Any]:
    return {"kind": kind, "bbox": bbox.as_list() if hasattr(bbox, "as_list") else bbox}


def run_rules(sheet: Sheet) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    n = 0

    def add(code: str, severity: str, title: str, detail: str,
            targets: list[dict], item: str | None = None, fix: str = "") -> None:
        nonlocal n
        n += 1
        issues.append({
            "id": f"R{n}",
            "code": code,
            "severity": severity,
            "title": title,
            "detail": detail,
            "item": item,
            "targets": targets,
            "recommendation": fix,
            "source": "rule",
        })

    bom_by_item: dict[str, list] = defaultdict(list)
    for row in sheet.bom_rows:
        bom_by_item[str(row.item)].append(row)

    balloons_by_item: dict[str, list] = defaultdict(list)
    for b in sheet.balloons:
        if b.item:
            balloons_by_item[b.item].append(b)

    # ---- unreadable balloons -------------------------------------------------
    for b in sheet.balloons:
        if not b.item:
            add("BALLOON_UNREADABLE", WARNING,
                "Balloon could not be read",
                "A balloon was detected but no item number could be extracted from it.",
                [_target("balloon", b.bbox)],
                fix="Check the balloon text is real text and not an outlined/exploded font.")

    # ---- duplicated rows in the parts list -----------------------------------
    for item, rows in bom_by_item.items():
        if len(rows) > 1:
            add("BOM_DUPLICATE_ITEM", ERROR,
                f"Item {item} appears {len(rows)} times in the parts list",
                f"The parts list contains {len(rows)} separate rows for item {item}. "
                "Each item number must be unique.",
                [_target("bom_row", r.bbox) for r in rows if r.bbox],
                item=item,
                fix="Merge the duplicate rows or renumber one of them.")

    # ---- balloon on the drawing with no parts list row -----------------------
    for item, blist in sorted(balloons_by_item.items(), key=lambda kv: _numkey(kv[0])):
        if item not in bom_by_item:
            add("BALLOON_NOT_IN_BOM", ERROR,
                f"Item {item} is ballooned but missing from the parts list",
                f"{len(blist)} balloon(s) on the sheet call out item {item}, but the parts list "
                "has no row for it. The part cannot be purchased, kitted or costed.",
                [_target("balloon", b.bbox) for b in blist],
                item=item,
                fix=f"Add a row for item {item} to the parts list, or delete the balloon(s).")

    # ---- parts list row with no balloon --------------------------------------
    for item, rows in sorted(bom_by_item.items(), key=lambda kv: _numkey(kv[0])):
        if item not in balloons_by_item:
            row = rows[0]
            add("BOM_NOT_BALLOONED", ERROR,
                f"Item {item} is in the parts list but not ballooned",
                f"Item {item} ({row.part_number or 'no part number'} - "
                f"{row.description or 'no description'}) has no balloon on any view. "
                "Assembly cannot tell where the part goes.",
                [_target("bom_row", row.bbox)] if row.bbox else [],
                item=item,
                fix=f"Balloon item {item} on the view where it is installed.")

    # ---- quantity disagreements ----------------------------------------------
    for item, blist in sorted(balloons_by_item.items(), key=lambda kv: _numkey(kv[0])):
        rows = bom_by_item.get(item)
        if not rows:
            continue
        bom_qty = rows[0].qty
        stated = {b.qty for b in blist if b.qty is not None}

        if len(stated) > 1:
            add("BALLOON_QTY_CONFLICT", ERROR,
                f"Item {item} balloons disagree with each other",
                f"Balloons for item {item} state different quantities "
                f"({', '.join(str(q) for q in sorted(stated))}).",
                [_target("balloon", b.bbox) for b in blist],
                item=item,
                fix="Make every balloon for the item state the same quantity.")

        if bom_qty is None:
            add("BOM_QTY_MISSING", WARNING,
                f"Item {item} has no quantity in the parts list",
                f"The QTY cell for item {item} is blank or non-numeric "
                f"(read as '{rows[0].qty_raw}').",
                [_target("bom_row", rows[0].bbox)] if rows[0].bbox else [],
                item=item,
                fix="Enter a numeric quantity.")
        else:
            for q in sorted(stated):
                if q != bom_qty:
                    tgts = [_target("balloon", b.bbox) for b in blist if b.qty == q]
                    if rows[0].bbox:
                        tgts.append(_target("bom_row", rows[0].bbox))
                    add("QTY_MISMATCH", ERROR,
                        f"Item {item}: balloon says {q}, parts list says {bom_qty}",
                        f"The balloon quantity for item {item} ({q}) does not match the parts "
                        f"list quantity ({bom_qty}). Difference of {abs(q - bom_qty)} piece(s).",
                        tgts, item=item,
                        fix="Confirm the installed quantity on the model and correct whichever "
                            "side is wrong.")

    # ---- same item ballooned more than once ----------------------------------
    for item, blist in sorted(balloons_by_item.items(), key=lambda kv: _numkey(kv[0])):
        if len(blist) > 1 and len({b.qty for b in blist}) == 1:
            add("BALLOON_DUPLICATE", INFO,
                f"Item {item} is ballooned {len(blist)} times",
                f"Item {item} carries {len(blist)} balloons on the sheet. This is acceptable "
                "under some drawing standards but is often an unintended duplicate.",
                [_target("balloon", b.bbox) for b in blist],
                item=item,
                fix="Keep one balloon per item unless the standard in use allows repeats.")

    # ---- stated total vs column sum ------------------------------------------
    known = [r.qty for r in sheet.bom_rows if r.qty is not None]
    computed = sum(known)
    if sheet.stated_total is not None and sheet.stated_total != computed:
        add("TOTAL_MISMATCH", ERROR,
            f"Total parts count is {sheet.stated_total}, quantities add up to {computed}",
            f"The parts list states a total of {sheet.stated_total} but the QTY column sums to "
            f"{computed} across {len(known)} rows. Difference of "
            f"{abs(sheet.stated_total - computed)}.",
            [_target("bom_row", sheet.stated_total_bbox)] if sheet.stated_total_bbox else [],
            fix="Recalculate the total, or check for a row that was deleted without updating it.")

    # ---- numbering gaps -------------------------------------------------------
    nums = sorted({int(r.item) for r in sheet.bom_rows if str(r.item).isdigit()})
    if nums:
        gaps = [i for i in range(nums[0], nums[-1] + 1) if i not in nums]
        if gaps:
            add("ITEM_SEQUENCE_GAP", INFO,
                f"Item numbering skips {', '.join(str(g) for g in gaps)}",
                f"Parts list item numbers run {nums[0]} to {nums[-1]} but "
                f"{', '.join(str(g) for g in gaps)} are absent. Gaps usually mean a row was "
                "deleted, and can hide a part that should still be there.",
                [], fix="Renumber consecutively or confirm the gaps are intentional.")

    order = {ERROR: 0, WARNING: 1, INFO: 2}
    issues.sort(key=lambda i: (order[i["severity"]], _numkey(i.get("item"))))
    return issues


def _numkey(v) -> tuple[int, str]:
    s = str(v) if v is not None else ""
    return (int(s), "") if s.isdigit() else (10**6, s)


def summarise(issues: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(issues),
        "error": sum(1 for i in issues if i["severity"] == ERROR),
        "warning": sum(1 for i in issues if i["severity"] == WARNING),
        "info": sum(1 for i in issues if i["severity"] == INFO),
    }
