"""Cross-check balloons against the parts list.

The quantity check is the part that changed most. The old code branched on a
single "split balloons?" flag and then guessed between max-per-sheet and
sum-across-sheets, which both hid real errors (a genuine extra placement on a
second sheet was absorbed by the max) and invented false ones (13/2 + 13/8
against a list quantity of 10 was reported twice as a mismatch).

Now every item is evaluated against ALL the readings a drawing office might
have intended:

    total_of_placements   every balloon marks its own place        2 + 8 = 10
    per_sheet             sheets repeat the same content           max sheet
    per_view              views repeat the same content            max view
    distinct_places       the same qty repeated in two views       sum of distinct
    stated_on_each        every balloon states the full quantity   the common value
    one_per_balloon       balloons carry no quantity               balloon count

If the parts list agrees with the primary reading, the item is clean. If it
agrees only with a secondary reading, that is reported as an ambiguity to
resolve, not an error. If nothing agrees, it is an error and the message shows
every reading that was tried, so the checker never silently picks a side.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from extractor import Balloon, BomRow, Document, Sheet, MAIN_VIEW

ERROR, WARNING, INFO = "error", "warning", "info"

# Turn the softer notes off if the panel gets noisy on your drawings.
EMIT_NOTES = True


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _target(kind: str, bbox, page: int) -> dict[str, Any]:
    return {
        "kind": kind,
        "bbox": bbox.as_list() if hasattr(bbox, "as_list") else bbox,
        "page": page,
    }


def _balloon_target(b: Balloon) -> dict[str, Any]:
    return _target("balloon", b.bbox, b.page_index)


def _row_target(r: BomRow, fallback_page: int) -> list[dict[str, Any]]:
    if r.bbox is None:
        return []
    return [_target("bom_row", r.bbox, r.page_index if r.page_index is not None else fallback_page)]


def _numkey(v) -> tuple[int, str]:
    s = str(v) if v is not None else ""
    return (int(s), "") if s.isdigit() else (10 ** 6, s)


def _breakdown(reading: dict) -> str:
    """' (2 + 8)' when the sum has parts, '' when it would just repeat the total."""
    if reading["breakdown"].strip() == str(reading["value"]):
        return ""
    return f" ({reading['breakdown']})"


def _sheet_label(pages: list[int]) -> str:
    uniq = sorted(set(pages))
    if len(uniq) == 1:
        return f"sheet {uniq[0] + 1}"
    return "sheets " + ", ".join(str(p + 1) for p in uniq)


def _as_document(obj) -> Document:
    if isinstance(obj, Document):
        return obj
    if isinstance(obj, Sheet):
        return Document(
            filename="",
            sheets=[obj],
            master_page_index=0,
            bom_rows=obj.bom_rows,
            raw_bom_rows=obj.bom_rows,
            balloons=obj.balloons,
            stated_total=obj.stated_total,
            stated_total_bbox=obj.stated_total_bbox,
            split_balloons=obj.split_balloons,
        )
    raise TypeError("run_rules expects a Document or a Sheet")


# --------------------------------------------------------------------------- #
# quantity readings
# --------------------------------------------------------------------------- #
def _quantity_readings(blist: list[Balloon]) -> tuple[list[dict], list[Balloon], list[Balloon]]:
    """Every defensible total for one item, most likely first."""
    real = [b for b in blist if not b.is_ref]
    with_qty = [b for b in real if b.qty is not None]
    without_qty = [b for b in real if b.qty is None]

    readings: list[dict] = []

    def push(key, label, value, breakdown):
        if value is None:
            return
        if any(r["key"] == key for r in readings):
            return
        readings.append({"key": key, "label": label, "value": int(value),
                         "breakdown": breakdown})

    if with_qty:
        by_sheet: dict[int, list[int]] = defaultdict(list)
        by_view: dict[tuple[int, str], list[int]] = defaultdict(list)
        for b in with_qty:
            by_sheet[b.page_index].append(b.qty)
            by_view[(b.page_index, b.view or MAIN_VIEW)].append(b.qty)

        total = sum(b.qty for b in with_qty)
        push("total_of_placements",
             "every balloon is its own place",
             total,
             " + ".join(str(b.qty) for b in with_qty))

        if len(by_sheet) > 1:
            best_page = max(by_sheet, key=lambda p: sum(by_sheet[p]))
            push("per_sheet",
                 "the sheets repeat the same placements",
                 sum(by_sheet[best_page]),
                 f"sheet {best_page + 1}: " + " + ".join(str(q) for q in by_sheet[best_page]))

        if len(by_view) > 1:
            best_view = max(by_view, key=lambda k: sum(by_view[k]))
            page, view = best_view
            push("per_view",
                 "the views repeat the same placements",
                 sum(by_view[best_view]),
                 f"{view} on sheet {page + 1}: " + " + ".join(str(q) for q in by_view[best_view]))

        distinct = sorted({b.qty for b in with_qty})
        if len(distinct) < len(with_qty):
            push("distinct_places",
                 "identical callouts are the same place shown twice",
                 sum(distinct),
                 " + ".join(str(q) for q in distinct))
            if len(distinct) == 1:
                push("stated_on_each",
                     "each balloon states the whole quantity",
                     distinct[0],
                     str(distinct[0]))

    if not with_qty and real:
        push("one_per_balloon",
             "one balloon per piece",
             len(real),
             f"{len(real)} balloon(s)")

    return readings, with_qty, without_qty


# --------------------------------------------------------------------------- #
# main entry
# --------------------------------------------------------------------------- #
def run_rules(doc_or_sheet) -> list[dict[str, Any]]:
    doc = _as_document(doc_or_sheet)
    master_page = doc.master_page_index
    issues: list[dict[str, Any]] = []
    n = 0

    def add(code, severity, title, detail, targets, item=None, fix="", evidence=None):
        nonlocal n
        n += 1
        pages = sorted({t["page"] for t in targets if isinstance(t.get("page"), int)})
        issues.append({
            "id": f"R{n}",
            "code": code,
            "severity": severity,
            "title": title,
            "detail": detail,
            "item": item,
            "targets": targets,
            "pages": pages,
            "recommendation": fix,
            "evidence": evidence or {},
            "source": "rule",
        })

    master_sheet = doc.master
    master_items = {str(r.item) for r in (master_sheet.bom_rows if master_sheet else [])
                    if r.item is not None}
    extract_pages = {s.page_index for s in doc.sheets if s.bom_is_extract}

    bom_by_item: dict[str, BomRow] = {}
    for row in doc.bom_rows:
        if row.item is not None:
            bom_by_item.setdefault(str(row.item), row)

    balloons_by_item: dict[str, list[Balloon]] = defaultdict(list)
    unreadable: list[Balloon] = []
    for b in doc.balloons:
        if b.item:
            balloons_by_item[b.item].append(b)
        else:
            unreadable.append(b)

    have_bom = bool(bom_by_item)
    have_balloons = bool(doc.balloons)

    # ---- coverage of the read itself ------------------------------------- #
    if have_balloons and not have_bom:
        add("BOM_NOT_READ", WARNING,
            "Balloon / parts-list cross-check skipped",
            "Balloons were found, but no parts list could be read, so no item can be "
            "verified against a row.",
            [], fix="Make sure the parts list is real text in a ruled table, then re-check.")
    if have_bom and not have_balloons:
        add("BALLOONS_NOT_READ", WARNING,
            "Balloon / parts-list cross-check skipped",
            "A parts list was read, but no balloons were detected on any sheet.",
            [], fix="Balloons must be outlines containing real text, not outlined fonts.")

    for b in unreadable:
        add("BALLOON_UNREADABLE", WARNING,
            "Balloon could not be read",
            f"A balloon was detected on sheet {b.page_index + 1} but no item number could be "
            f"taken from it (raw text: '{b.raw_text}').",
            [_balloon_target(b)],
            fix="Check the text inside the balloon is text, not converted to outlines.")

    if EMIT_NOTES:
        low = [b for b in doc.balloons if b.confidence == "low"]
        if low:
            add("BALLOON_LOW_CONFIDENCE", INFO,
                f"{len(low)} callout(s) read without an outline",
                "These callouts were read from plain text (no circle was drawn around "
                "them). They are included in the checks, but confirm them by eye.",
                [_balloon_target(b) for b in low],
                fix="Draw the standard balloon outline so the callout is unambiguous.")

    # ---- parts list integrity -------------------------------------------- #
    for item, rows in sorted(doc.duplicate_rows.items(), key=lambda kv: _numkey(kv[0])):
        qtys = sorted({r.qty for r in rows if r.qty is not None})
        add("BOM_DUPLICATE_ITEM", ERROR,
            f"Item {item} appears {len(rows)} times in the parts list",
            f"The parts list on sheet {rows[0].page_index + 1} has {len(rows)} rows for item "
            f"{item}" + (f" (quantities {', '.join(str(q) for q in qtys)})." if qtys else "."),
            [t for r in rows for t in _row_target(r, master_page)],
            item=item,
            fix="Merge the rows, or renumber one of them if they are different parts.")

    for item, rows in sorted(doc.row_conflicts.items(), key=lambda kv: _numkey(kv[0])):
        if all(r.page_index in extract_pages for r in rows if r.page_index != master_page):
            continue        # EXTRACT_QTY_MISMATCH below says this more precisely
        parts = "; ".join(
            f"sheet {r.page_index + 1}: qty {r.qty if r.qty is not None else r.qty_raw or '—'}"
            f"{', ' + r.part_number if r.part_number else ''}"
            for r in rows
        )
        add("BOM_ROW_CONFLICT", ERROR,
            f"Item {item} is listed differently on different sheets",
            f"Item {item} appears on more than one sheet with details that do not agree "
            f"({parts}). The richest row was used for the checks below.",
            [t for r in rows for t in _row_target(r, master_page)],
            item=item,
            fix="Keep one authoritative parts list, and make any extract on another sheet copy it exactly.")

    for item, row in sorted(bom_by_item.items(), key=lambda kv: _numkey(kv[0])):
        if row.qty is not None and row.qty == 0:
            add("BOM_QTY_ZERO", WARNING,
                f"Item {item} has a quantity of zero",
                f"Item {item} is listed with QTY 0. A zero-quantity row is usually a deleted "
                "part that was never removed.",
                _row_target(row, master_page), item=item,
                fix="Delete the row, or restore the correct quantity.")
        if EMIT_NOTES and not row.part_number and not row.description:
            add("BOM_ROW_INCOMPLETE", WARNING,
                f"Item {item} has no part number or description",
                f"Row {item} was read with neither a part number nor a description, so it "
                "cannot be identified.",
                _row_target(row, master_page), item=item,
                fix="Fill in the part number and description, or check the row is machine-readable.")

    # ---- balloon <-> row coverage ---------------------------------------- #
    if have_bom:
        for item, blist in sorted(balloons_by_item.items(), key=lambda kv: _numkey(kv[0])):
            if item in bom_by_item:
                continue
            if all(b.is_ref for b in blist):
                continue          # covered by the REF note below
            add("BALLOON_NOT_IN_BOM", ERROR,
                f"Item {item} is ballooned but missing from the parts list",
                f"{len(blist)} balloon(s) on {_sheet_label([b.page_index for b in blist])} call out "
                f"item {item}, but the parts list has no row for it.",
                [_balloon_target(b) for b in blist], item=item,
                fix=f"Add a row for item {item}, or delete the balloon(s).")

    if have_balloons:
        for item, row in sorted(bom_by_item.items(), key=lambda kv: _numkey(kv[0])):
            if item in balloons_by_item:
                continue
            if master_items and item not in master_items:
                continue    # only an extract sheet lists it: EXTRACT_ITEM_UNKNOWN covers it
            add("BOM_NOT_BALLOONED", ERROR,
                f"Item {item} is in the parts list but not ballooned",
                f"Item {item} ({row.part_number or 'no part number'} — "
                f"{row.description or 'no description'}) has no balloon on any sheet.",
                _row_target(row, master_page), item=item,
                fix=f"Balloon item {item} on the view where it is installed.")

    # ---- quantities ------------------------------------------------------- #
    for item, blist in sorted(balloons_by_item.items(), key=lambda kv: _numkey(kv[0])):
        row = bom_by_item.get(item)
        if row is None:
            continue

        readings, with_qty, without_qty = _quantity_readings(blist)
        real = [b for b in blist if not b.is_ref]
        pages = [b.page_index for b in real]
        views = {(b.page_index, b.view or MAIN_VIEW) for b in real}

        row_targets = _row_target(row, master_page)
        balloon_targets = [_balloon_target(b) for b in real]

        # mixed style: some balloons carry a per-place quantity, some do not
        if with_qty and without_qty:
            add("BALLOON_STYLE_MIXED", WARNING,
                f"Item {item} balloons are not written the same way",
                f"{len(with_qty)} balloon(s) for item {item} carry a quantity and "
                f"{len(without_qty)} do not, so the placements cannot be totalled reliably.",
                balloon_targets, item=item,
                evidence={"with_qty": len(with_qty), "without_qty": len(without_qty)},
                fix="Use one convention for the item: either every balloon states its quantity, or none do.")

        for b in with_qty:
            if b.qty == 0:
                add("BALLOON_QTY_ZERO", WARNING,
                    f"Item {item} has a balloon with a quantity of zero",
                    f"A balloon on sheet {b.page_index + 1} reads '{b.raw_text}'.",
                    [_balloon_target(b)], item=item,
                    fix="Correct the quantity, or remove the balloon.")

        # same item, same view, identical quantity -> probably a duplicate callout
        if EMIT_NOTES:
            per_view: dict[tuple[int, str], list[Balloon]] = defaultdict(list)
            for b in real:
                per_view[(b.page_index, b.view or MAIN_VIEW)].append(b)
            for (page, view), group in sorted(per_view.items()):
                if len(group) > 1 and len({b.qty for b in group}) == 1:
                    where = f"{view} on sheet {page + 1}" if view != MAIN_VIEW else f"sheet {page + 1}"
                    add("BALLOON_DUPLICATE_IN_VIEW", WARNING,
                        f"Item {item} is ballooned {len(group)} times in one view",
                        f"{where} carries {len(group)} identical balloons for item {item}. "
                        "Either they are separate places that should read differently, or one is a "
                        "leftover copy.",
                        [_balloon_target(b) for b in group], item=item,
                        fix="Keep one balloon per place, and let the quantity say how many pieces go there.")
            if len(views) > 1 and with_qty:
                add("BALLOON_ACROSS_VIEWS", INFO,
                    f"Item {item} is ballooned in {len(views)} views",
                    f"Item {item} is called out in {len(views)} different views "
                    f"({', '.join(sorted(v for _, v in views))}). Views that repeat the same "
                    "placement must not be counted twice.",
                    balloon_targets, item=item,
                    fix="Confirm each view shows a different place.")

        if row.is_ar:
            continue           # A/R rows carry no number to check against

        if row.qty is None:
            add("BOM_QTY_MISSING", WARNING,
                f"Item {item} has no quantity in the parts list",
                f"The QTY cell for item {item} is blank or not a number "
                f"(read as '{row.qty_raw or '—'}'), so the "
                f"{len(real)} balloon(s) cannot be checked against it.",
                row_targets + balloon_targets, item=item,
                fix="Enter a numeric quantity, or mark the row A/R if that is intended.")
            continue

        if not readings:
            continue

        primary = readings[0]
        matches = [r for r in readings if r["value"] == row.qty]
        evidence = {
            "bom_qty": row.qty,
            "readings": readings,
            "matched": [m["key"] for m in matches],
            "balloons": [
                {"id": b.id, "page": b.page_index, "view": b.view,
                 "qty": b.qty, "text": b.raw_text}
                for b in real
            ],
        }

        if not matches:
            delta = primary["value"] - row.qty
            shown = _breakdown(primary)
            detail = (
                f"On {_sheet_label(pages)} the balloons for item {item} give {primary['value']}"
                f"{shown} but the parts list states {row.qty} — "
                f"{abs(delta)} too {'many' if delta > 0 else 'few'}."
            )
            if len(readings) > 1:
                tried = "; ".join(f"{r['value']} ({r['label']})" for r in readings[1:])
                detail += f" No other reading matches either ({tried})."
            add("QTY_MISMATCH", ERROR,
                f"Item {item}: balloons total {primary['value']}, parts list says {row.qty}",
                detail,
                balloon_targets + row_targets, item=item, evidence=evidence,
                fix="Correct the balloon quantities or the parts list, whichever is wrong.")
        elif matches[0]["key"] != primary["key"]:
            add("QTY_AMBIGUOUS", WARNING,
                f"Item {item} only adds up if the callouts repeat",
                f"Added straight up, the balloons for item {item} give {primary['value']} "
                f"({primary['breakdown']}), which is more than the {row.qty} in the parts list. "
                f"It matches if {matches[0]['label']} ({matches[0]['breakdown']} = {row.qty}). "
                "That reading was assumed, but it depends on how the views repeat.",
                balloon_targets + row_targets, item=item, evidence=evidence,
                fix="Say on the drawing which callouts are repeats, or balloon each place once.")

    # ---- REF callouts ----------------------------------------------------- #
    for item, blist in sorted(balloons_by_item.items(), key=lambda kv: _numkey(kv[0])):
        refs = [b for b in blist if b.is_ref]
        if not refs:
            continue
        in_bom = item in bom_by_item
        add("BALLOON_REFERENCE", INFO,
            f"Item {item} has a reference (REF) balloon",
            f"{len(refs)} balloon(s) mark item {item} as REF — shown for reference, detailed or "
            f"installed elsewhere. It is {'listed in' if in_bom else 'NOT in'} the parts list and "
            "is left out of the quantity totals.",
            [_balloon_target(b) for b in refs], item=item,
            fix="" if in_bom else f"Confirm item {item} belongs in this parts list, or that the REF "
                                  "callout is intentional.")

    # ---- totals ----------------------------------------------------------- #
    total_rows = master_sheet.bom_rows if master_sheet else doc.bom_rows
    known = [r.qty for r in total_rows if r.qty is not None]
    computed = sum(known)
    if doc.stated_total is not None and doc.stated_total != computed:
        add("TOTAL_MISMATCH", ERROR,
            f"Stated total is {doc.stated_total}, the quantities add up to {computed}",
            f"The parts list states a total of {doc.stated_total} but the QTY column sums to "
            f"{computed} across {len(known)} numeric row(s); A/R rows are excluded.",
            [_target("bom_row", doc.stated_total_bbox, master_page)] if doc.stated_total_bbox else [],
            fix="Recalculate the total, or look for a row that was deleted without updating it.")

    for s in doc.sheets:
        if s.page_index == master_page or s.stated_total is None:
            continue
        sheet_sum = sum(r.qty for r in s.bom_rows if r.qty is not None)
        if sheet_sum != s.stated_total:
            add("SHEET_TOTAL_MISMATCH", ERROR,
                f"Sheet {s.page_index + 1}: stated total {s.stated_total}, rows add up to {sheet_sum}",
                f"The table on sheet {s.page_index + 1} states {s.stated_total} but its own rows "
                f"sum to {sheet_sum}.",
                [_target("bom_row", s.stated_total_bbox, s.page_index)] if s.stated_total_bbox else [],
                fix="Recalculate the total on that sheet.")

    # ---- extracts must copy the master ------------------------------------ #
    for s in doc.sheets:
        if not s.bom_is_extract or s.page_index == master_page:
            continue
        for r in s.bom_rows:
            if r.item is None or r.qty is None:
                continue
            main = bom_by_item.get(str(r.item)) if str(r.item) in master_items else None
            if main is None:
                add("EXTRACT_ITEM_UNKNOWN", ERROR,
                    f"Item {r.item} is on the sheet {s.page_index + 1} extract but not in the main list",
                    f"The extract table on sheet {s.page_index + 1} lists item {r.item}, which does "
                    f"not appear in the parts list on sheet {master_page + 1}.",
                    _row_target(r, s.page_index), item=str(r.item),
                    fix="Add the item to the main parts list, or remove it from the extract.")
            elif main.qty is not None and main.qty != r.qty:
                add("EXTRACT_QTY_MISMATCH", ERROR,
                    f"Item {r.item}: extract says {r.qty}, main parts list says {main.qty}",
                    f"The extract on sheet {s.page_index + 1} disagrees with the parts list on "
                    f"sheet {master_page + 1} for item {r.item}.",
                    _row_target(r, s.page_index) + _row_target(main, master_page),
                    item=str(r.item),
                    fix="Regenerate the extract from the main parts list.")

    # ---- numbering -------------------------------------------------------- #
    nums = sorted({int(r.item) for r in doc.bom_rows
                   if r.item is not None and str(r.item).isdigit()})
    if nums and EMIT_NOTES:
        gaps = [i for i in range(nums[0], nums[-1] + 1) if i not in nums]
        # A sparse, reserved numbering scheme is legitimate; only a few holes are
        # worth mentioning, otherwise the panel fills with noise.
        if gaps and len(gaps) <= max(3, len(nums) // 2):
            shown = ", ".join(str(g) for g in gaps[:15]) + ("  …" if len(gaps) > 15 else "")
            add("ITEM_SEQUENCE_GAP", INFO,
                f"Item numbering skips {shown}",
                f"Item numbers run {nums[0]} to {nums[-1]} but {len(gaps)} number(s) are absent "
                f"({shown}).",
                [], fix="Renumber consecutively, or confirm the gaps are intentional.")

    order = {ERROR: 0, WARNING: 1, INFO: 2}
    issues.sort(key=lambda i: (order[i["severity"]], _numkey(i.get("item")), i["code"]))
    for idx, issue in enumerate(issues, 1):
        issue["id"] = f"R{idx}"
    return issues


def summarise(issues: list[dict[str, Any]]) -> dict[str, Any]:
    by_code: dict[str, int] = defaultdict(int)
    for i in issues:
        by_code[i["code"]] += 1
    return {
        "total": len(issues),
        "error": sum(1 for i in issues if i["severity"] == ERROR),
        "warning": sum(1 for i in issues if i["severity"] == WARNING),
        "info": sum(1 for i in issues if i["severity"] == INFO),
        "by_code": dict(sorted(by_code.items(), key=lambda kv: -kv[1])),
    }
