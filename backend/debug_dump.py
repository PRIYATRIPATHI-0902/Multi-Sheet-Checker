"""Deep diagnostic for the Assembly BOM / Balloon Checker.

Usage:
    python debug_dump.py path/to/drawing.pdf
"""
from __future__ import annotations

import io
import re
import sys

import pdfplumber
import extractor
from extractor import _group_lines, _numkey, _PN_CODE_RE, _salvage_relaxed_match


def _master_lines(pdf_bytes: bytes, master_idx: int):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page = pdf.pages[master_idx]
        words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    return _group_lines(words)


def _looks_like_table_line(toks: list[str]) -> bool:
    if len(toks) < 2:
        return False
    for t in toks[:3]:
        if re.fullmatch(r"\d{1,4}", t) or re.fullmatch(r"[A-Z]{1,3},?[A-Z]?\d{1,4}", t):
            return True
    return False


def _dump_raw_table_lines(lines) -> None:
    print("-" * 78)
    print("RAW MASTER-PAGE TABLE LINES  (idx | x0:token):")
    for li, ln in enumerate(lines):
        toks = [w["text"].strip() for w in ln]
        if not _looks_like_table_line(toks):
            continue
        cells = " | ".join(f"{round(w['x0'],1)}:{w['text'].strip()}" for w in ln)
        print(f"  L{li:>3}  {cells}")


def _salvage_trace(lines, items) -> None:
    import statistics
    pn_xs = [w["x0"] for ln in lines for w in ln
             if _PN_CODE_RE.fullmatch(w["text"].strip()) and len(w["text"].strip()) >= 5]
    dom = statistics.median(pn_xs) if pn_xs else None
    print("-" * 78)
    print(f"SALVAGE TRACE  (dominant PN column x ~= {round(dom,1) if dom else None}):")

    def candidates(it, strict):
        found = []
        for li, ln in enumerate(lines):
            s = sorted(ln, key=lambda w: w["x0"])
            for i in range(1, len(s)):
                pn = s[i]["text"].strip()
                if not (_PN_CODE_RE.fullmatch(pn) and len(pn) >= 5):
                    continue
                left = s[i - 1]["text"].strip()
                ok = (left == it) if strict else _salvage_relaxed_match(left, it)
                if ok:
                    dist = abs(s[i]["x0"] - dom) if dom else 0
                    found.append((li, left, round(s[i]["x0"], 1), pn, round(dist, 1)))
        return sorted(found, key=lambda c: c[4])

    for it in items:
        strict = candidates(it, True)
        relaxed = candidates(it, False)
        print(f"\n  item {it!r}")
        print(f"    strict  (by column): {strict or 'none'}")
        print(f"    relaxed (by column): {relaxed or 'none'}")
        pick = (strict or relaxed)
        if pick:
            li, left, x, pn, d = pick[0]
            print(f"    -> PICK L{li} left={left!r} part_number={pn!r} (col-dist {d})")
        else:
            print("    -> NO MATCH")


def main(path: str) -> None:
    data = open(path, "rb").read()
    doc = extractor.parse_document(data, filename=path)

    print(f"\nFILE: {path}")
    print(f"sheets: {len(doc.sheets)}   master sheet: {doc.master_page_index + 1}")
    print("=" * 78)

    print("PARSED BOM ROWS (item | qty_raw | is_ar | part_number | description):")
    for r in doc.bom_rows:
        print(f"  {str(r.item):>4} | {r.qty_raw!r:>6} | {str(r.is_ar):>5} | {r.part_number:<12} | {r.description}")

    print("-" * 78)
    print("BALLOONS (item | qty | is_ref | RAW text) per sheet:")
    for s in doc.sheets:
        print(f"  sheet {s.page_index + 1}:")
        for b in sorted(s.balloons, key=lambda b: _numkey(b.item)):
            print(f"      item={str(b.item):>4}  qty={str(b.qty):>4}  ref={str(getattr(b,'is_ref',False)):>5}  raw={b.raw_text!r}")

    print("-" * 78)
    bom_items = {str(r.item) for r in doc.bom_rows if r.item is not None}
    ball_items = {b.item for b in doc.balloons if b.item}
    missing = sorted(ball_items - bom_items, key=_numkey)
    print(f"BALLOONED ITEMS:  {sorted(ball_items, key=_numkey)}")
    print(f"BOM ITEMS:        {sorted(bom_items, key=_numkey)}")
    print(f"BALLOONED BUT MISSING FROM BOM: {missing or 'none'}")

    pn_seen: dict[str, list[str]] = {}
    for r in doc.bom_rows:
        if r.part_number:
            pn_seen.setdefault(r.part_number, []).append(str(r.item))
    dupe = {pn: its for pn, its in pn_seen.items() if len(its) > 1}
    if dupe:
        print(f"SUSPICIOUS (same part_number on multiple items): {dupe}")

    lines = _master_lines(data, doc.master_page_index)
    _dump_raw_table_lines(lines)
    trace_items = sorted(set(missing) | {i for i in ball_items if i.isdigit() and int(i) >= 100},
                         key=_numkey)
    if trace_items:
        _salvage_trace(lines, trace_items)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python debug_dump.py path/to/drawing.pdf")
        raise SystemExit(1)
    main(sys.argv[1])
