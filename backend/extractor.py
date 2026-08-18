"""Read an assembly drawing PDF into balloons + parts-list rows.

What changed vs. the previous version
-------------------------------------
* Balloon text is parsed from every common notation, not just a geometric
  upper/lower split:  13 | 13/2 | 13(2) | 2X13 | 13-2 (split sheets only) |
  13 REF.  Previously "13/2" written as a single text run lost its quantity
  entirely, which silently disabled every quantity check on that item.
* Balloons may be circles, ovals, rounded rectangles or hexagons; shapes are
  taken from page.curves AND page.rects, de-duplicated by centre and by
  containment (concentric outlines), and excluded inside detected tables.
* Optional low-confidence text-only callouts ("13/2" with no drawn outline),
  gated so imperial fractions and stray numbers cannot slip through.
* Every balloon is assigned to a view (VIEW A-A / SECTION B-B / DETAIL C), so
  the rules can tell "same item shown twice in two views" apart from
  "same item installed in two places".
* Duplicate parts-list rows and cross-sheet disagreements are preserved
  instead of being silently merged away.
* The PDF is opened once for parsing and once for rendering.
"""

from __future__ import annotations

import base64
import io
import re
import statistics
from dataclasses import dataclass, field, asdict
from typing import Any

import pdfplumber
import pypdfium2 as pdfium

RENDER_DPI = 150

# --- balloon geometry ------------------------------------------------------ #
BALLOON_MIN_D = 10.0          # smallest outline that can be a balloon (pt)
BALLOON_MAX_D = 70.0          # largest
BALLOON_MIN_RATIO = 0.45      # min(w,h)/max(w,h): 1.0 = circle, 0.45 = stadium
RECT_MIN_RATIO = 0.55         # rectangles must be closer to square
DEDUPE_CENTRE_TOL = 3.0       # two outlines this close are one balloon

# --- balloon / BOM text ---------------------------------------------------- #
ITEM_RE = re.compile(r"^\d{1,4}$")
REF_RE = re.compile(r"\bREF\b", re.I)
_PN_CODE_RE = re.compile(r"[0-9][0-9\-]{3,}")

# Imperial fractions that must never be read as item/qty (1/2", 3/16", ...)
_FRACTION_DENOMS = {2, 3, 4, 8, 16, 32, 64}

AR_TOKENS = {"A/R", "AR", "A.R.", "AS REQ", "AS REQD", "AS REQ'D", "AS REQUIRED",
             "P/K", "PER KIT", "PK", "REF"}

SPLIT_BALLOON_HINTS = (
    "BALLOONS ARE SPLIT", "SPLIT BALLOON", "UPPER FIGURE = SEQ",
    "LOWER = QTY PER PLACE", "QTY PER PLACE", "QTY/PLACE", "QTY PER PLACE SHOWN",
)

BOM_EXTRACT_HINTS = ("BOM EXTRACT", "ITEMS ON THIS SHEET", "EXTRACT —", "EXTRACT -",
                     "PARTIAL PARTS LIST", "PARTS LIST (EXTRACT")

VIEW_RE = re.compile(
    r"\b(VIEW|SECTION|SECT|DETAIL|SCRAP\s+VIEW|ENLARGED\s+VIEW|ROTATED\s+VIEW)"
    r"\s+([A-Z]{1,2}(?:\s*[-–]\s*[A-Z]{1,2})?)\b"
)
MAIN_VIEW = "MAIN"

HEADER_ITEM = {
    "ITEM", "ITEM NO", "ITEM NO.", "ITEMNO", "ITEM NUMBER",
    "NO", "NO.", "SR", "SR.", "SR NO", "SL", "SL.", "SL NO", "SERIAL",
    "POS", "POS.", "POSITION", "FIND", "FIND NO", "INDEX", "IDX",
    "SEQ", "SEQ.", "SEQ#", "SEQ.#", "SEQ NO", "SEQNO", "SEQUENCE",
    "BALLOON", "BUBBLE", "ITEM#", "ITEM #", "PT",
}
HEADER_QTY = {
    "QTY", "QTY.", "QTY PER", "QUANTITY", "Q'TY", "QNTY", "QUAN", "QUANT",
    "OTY", "AMOUNT", "AMT", "PCS", "PC", "EA", "NO OFF", "NO. OFF", "REQD", "REQ'D",
}
HEADER_PN = {
    "PART", "PART NO", "PART NUMBER", "PARTNO", "PART NR", "ITEM NR", "ITEM NR.",
    "ITEM NO", "ITEM NO.", "DWG", "DWG NO", "DRAWING NO", "MAT NO", "CODE",
    "CAT", "CAT NO", "IDENTIFIER", "STOCK NO", "P/N", "PN", "REFERENCE",
    "REF DWG", "REF.DWG", "REF.DWG.", "ARTICLE", "COMPONENT",
}
HEADER_DESC = {
    "DESCRIPTION", "DESCR", "DESC", "TITLE", "NOMENCLATURE", "PART NAME",
    "NAME", "DENOMINATION", "DESIGNATION", "REMARKS",
}
HEADER_MAT = {"MATERIAL", "MATL", "MAT", "MTL", "SPEC", "SPECIFICATION", "MATERIALS"}
HEADER_IGNORE = {
    "REV", "REV.", "REVISION", "P/L", "PL", "P L", "ZONE",
    "SHEET", "UM", "U/M", "UOM", "UNIT", "WT", "WEIGHT",
}


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #
@dataclass
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    def as_list(self) -> list[float]:
        return [round(self.x0, 2), round(self.y0, 2), round(self.x1, 2), round(self.y1, 2)]

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    def contains(self, other: "Box", pad: float = 1.0) -> bool:
        return (self.x0 - pad <= other.x0 and self.y0 - pad <= other.y0
                and self.x1 + pad >= other.x1 and self.y1 + pad >= other.y1)

    def overlaps(self, other: "Box") -> bool:
        return not (self.x1 < other.x0 or other.x1 < self.x0
                    or self.y1 < other.y0 or other.y1 < self.y0)


@dataclass
class Balloon:
    id: str
    item: str | None
    qty: int | None
    raw_text: str
    bbox: Box
    page_index: int = 0
    nearby_text: str = ""
    is_ref: bool = False           # 'REF' callout: ballooned, but no placement qty
    qty_raw: str = ""
    view: str = MAIN_VIEW          # view label the balloon sits in
    source: str = "outline"        # "outline" | "text"
    confidence: str = "high"       # "high" | "low"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bbox"] = self.bbox.as_list()
        return d


@dataclass
class BomRow:
    item: str | None
    part_number: str = ""
    description: str = ""
    material: str = ""
    qty: int | None = None
    qty_raw: str = ""
    page_index: int = 0
    bbox: Box | None = None

    @property
    def is_ar(self) -> bool:
        return _norm(self.qty_raw) in AR_TOKENS

    def signature(self) -> tuple:
        """Fields that must agree when the same item appears on two sheets."""
        return (self.qty, _norm(self.part_number))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bbox"] = self.bbox.as_list() if self.bbox else None
        d["is_ar"] = self.is_ar
        return d


@dataclass
class Sheet:
    page_index: int
    page_width: float
    page_height: float
    balloons: list[Balloon] = field(default_factory=list)
    bom_rows: list[BomRow] = field(default_factory=list)
    stated_total: int | None = None
    stated_total_bbox: Box | None = None
    notes: list[str] = field(default_factory=list)
    title_block: dict[str, str] = field(default_factory=dict)
    views: list[dict[str, Any]] = field(default_factory=list)
    page_image_b64: str = ""
    bom_detected: bool = False
    bom_is_extract: bool = False
    split_balloons: bool = False
    table_boxes: list[Box] = field(default_factory=list)
    words: list[dict] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_index": self.page_index,
            "page_width": self.page_width,
            "page_height": self.page_height,
            "balloons": [b.to_dict() for b in self.balloons],
            "bom_rows": [r.to_dict() for r in self.bom_rows],
            "stated_total": self.stated_total,
            "stated_total_bbox": self.stated_total_bbox.as_list() if self.stated_total_bbox else None,
            "notes": self.notes,
            "title_block": self.title_block,
            "views": [{"label": v["label"], "bbox": v["bbox"].as_list()} for v in self.views],
            "bom_detected": self.bom_detected,
            "bom_is_extract": self.bom_is_extract,
            "split_balloons": self.split_balloons,
        }


@dataclass
class Document:
    filename: str = ""
    sheets: list[Sheet] = field(default_factory=list)
    master_page_index: int = 0
    bom_rows: list[BomRow] = field(default_factory=list)      # reconciled, one per item
    raw_bom_rows: list[BomRow] = field(default_factory=list)  # every row, every sheet
    balloons: list[Balloon] = field(default_factory=list)
    stated_total: int | None = None
    stated_total_bbox: Box | None = None
    split_balloons: bool = False
    split_inferred: bool = False
    duplicate_rows: dict[str, list[BomRow]] = field(default_factory=dict)
    row_conflicts: dict[str, list[BomRow]] = field(default_factory=dict)
    page_image_b64: str = ""

    @property
    def master(self) -> Sheet | None:
        if not self.sheets:
            return None
        return self.sheets[min(self.master_page_index, len(self.sheets) - 1)]

    def to_dict(self) -> dict[str, Any]:
        m = self.master
        return {
            "filename": self.filename,
            "master_page_index": self.master_page_index,
            "page_width": m.page_width if m else 0.0,
            "page_height": m.page_height if m else 0.0,
            "bom_rows": [r.to_dict() for r in self.bom_rows],
            "balloons": [b.to_dict() for b in self.balloons],
            "stated_total": self.stated_total,
            "stated_total_bbox": self.stated_total_bbox.as_list() if self.stated_total_bbox else None,
            "split_balloons": self.split_balloons,
            "split_inferred": self.split_inferred,
            "sheets": [s.to_dict() for s in self.sheets],
        }


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").upper()).strip().rstrip(".").strip()


def _lead_int(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(r"\d+", text)
    return m.group(0) if m else None


def _norm_item(s: str | None) -> str | None:
    """'02' and '2' must be the same item everywhere."""
    if s is None:
        return None
    s = str(s).strip()
    return str(int(s)) if s.isdigit() else s


def _item_cell_value(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    return _norm_item(s) if ITEM_RE.match(s) else None


def _numkey(v) -> tuple[int, str]:
    s = str(v) if v is not None else ""
    return (int(s), "") if s.isdigit() else (10 ** 6, s)


def _group_lines(words: list[dict], tol: float | None = None) -> list[list[dict]]:
    if not words:
        return []
    heights = sorted(w["bottom"] - w["top"] for w in words)
    med_h = heights[len(heights) // 2] if heights else 8.0
    tol = tol if tol is not None else max(3.0, med_h * 0.6)

    lines: list[dict] = []
    for w in sorted(words, key=lambda w: ((w["top"] + w["bottom"]) / 2, w["x0"])):
        cy = (w["top"] + w["bottom"]) / 2
        placed = False
        for ln in lines:
            if abs(ln["cy"] - cy) <= tol:
                ln["words"].append(w)
                ln["cy"] = (ln["cy"] * ln["n"] + cy) / (ln["n"] + 1)
                ln["n"] += 1
                placed = True
                break
        if not placed:
            lines.append({"cy": cy, "n": 1, "words": [w]})
    for ln in lines:
        ln["words"].sort(key=lambda w: w["x0"])
    lines.sort(key=lambda ln: ln["cy"])
    return [ln["words"] for ln in lines]


def _word_box(words: list[dict]) -> Box:
    return Box(min(w["x0"] for w in words), min(w["top"] for w in words),
               max(w["x1"] for w in words), max(w["bottom"] for w in words))


# --------------------------------------------------------------------------- #
# balloons
# --------------------------------------------------------------------------- #
def _shape_candidates(page, exclude: list[Box]) -> list[dict]:
    raw: list[tuple[str, dict]] = [("curve", o) for o in page.curves]
    raw += [("rect", o) for o in page.rects]

    out: list[dict] = []
    for kind, o in raw:
        w = o["x1"] - o["x0"]
        h = o["bottom"] - o["top"]
        if w <= 0 or h <= 0:
            continue
        big, small = max(w, h), min(w, h)
        if not (BALLOON_MIN_D <= big <= BALLOON_MAX_D):
            continue
        ratio = small / big
        if ratio < (RECT_MIN_RATIO if kind == "rect" else BALLOON_MIN_RATIO):
            continue
        box = Box(o["x0"], o["top"], o["x1"], o["bottom"])
        if any(t.contains(box, pad=2.0) for t in exclude):
            continue           # inside the parts list: a table cell, not a balloon
        out.append({"box": box, "kind": kind, "area": w * h})

    # concentric / repeated outlines are one balloon
    out.sort(key=lambda c: (-c["area"], c["box"].y0, c["box"].x0))
    kept: list[dict] = []
    for c in out:
        b = c["box"]
        if any(abs(b.cx - k["box"].cx) < DEDUPE_CENTRE_TOL
               and abs(b.cy - k["box"].cy) < DEDUPE_CENTRE_TOL for k in kept):
            continue
        if any(k["box"].contains(b, pad=0.5) for k in kept):
            continue
        kept.append(c)
    kept.sort(key=lambda c: (c["box"].y0, c["box"].x0))
    return kept


def _split_pair(s: str) -> tuple[str, str] | None:
    """Item/qty written on one line: 13/2, 13(2), 2X13."""
    s = s.strip()
    m = re.fullmatch(r"(\d{1,4})\s*[/|]\s*(\d{1,4})", s)
    if m:
        return m.group(1), m.group(2)
    m = re.fullmatch(r"(\d{1,4})\s*\(\s*(\d{1,4})\s*\)", s)
    if m:
        return m.group(1), m.group(2)
    m = re.fullmatch(r"(\d{1,3})\s*[xX]\s*(\d{1,4})", s)   # "2X 13" = 2 off item 13
    if m:
        return m.group(2), m.group(1)
    return None


def _parse_balloon_content(upper: str, lower: str, allow_dash: bool):
    """-> (item, qty, qty_raw, is_ref). Handles every notation in one place."""
    utxt, ltxt = upper.strip(), lower.strip()
    whole = f"{utxt} {ltxt}".strip()
    is_ref = bool(REF_RE.search(whole))

    body = REF_RE.sub(" ", whole).strip()
    ubody = REF_RE.sub(" ", utxt).strip()
    lbody = REF_RE.sub(" ", ltxt).strip()

    pair = _split_pair(body) or _split_pair(ubody)
    if pair is None and allow_dash:
        m = re.fullmatch(r"(\d{1,3})\s*[-–]\s*(\d{1,3})", body)
        if m:
            pair = (m.group(1), m.group(2))

    if pair:
        item, qty_raw = pair
    elif ubody and lbody:
        item, qty_raw = _lead_int(ubody) or "", lbody
    else:
        item, qty_raw = _lead_int(body) or "", ""

    item = _norm_item(_lead_int(item))
    qty_txt = _lead_int(qty_raw)
    qty = int(qty_txt) if qty_txt is not None else None
    if is_ref:
        qty = None          # a REF callout never contributes a placement count
    return item, qty, qty_raw.strip(), is_ref


def find_balloons(page, words: list[dict], page_index: int = 0,
                  exclude: list[Box] | None = None,
                  allow_dash: bool = False) -> list[Balloon]:
    exclude = exclude or []
    balloons: list[Balloon] = []

    for idx, cand in enumerate(_shape_candidates(page, exclude)):
        box = cand["box"]
        inside = [w for w in words
                  if box.x0 - 1 <= (w["x0"] + w["x1"]) / 2 <= box.x1 + 1
                  and box.y0 - 1 <= (w["top"] + w["bottom"]) / 2 <= box.y1 + 1]
        if not inside:
            continue
        if not any(re.search(r"\d", w["text"]) for w in inside):
            continue
        # A balloon holds a couple of short tokens, never a sentence.
        if sum(len(w["text"]) for w in inside) > 12 or len(inside) > 4:
            continue

        upper = [w for w in inside if (w["top"] + w["bottom"]) / 2 < box.cy - 1]
        lower = [w for w in inside if (w["top"] + w["bottom"]) / 2 >= box.cy - 1]
        utxt = " ".join(w["text"] for w in sorted(upper, key=lambda w: w["x0"])).strip()
        ltxt = " ".join(w["text"] for w in sorted(lower, key=lambda w: w["x0"])).strip()
        if not utxt:                      # single-line balloon
            utxt, ltxt = ltxt, ""

        item, qty, qty_raw, is_ref = _parse_balloon_content(utxt, ltxt, allow_dash)

        r = max((box.x1 - box.x0), (box.y1 - box.y0)) / 2
        near = [w["text"] for w in words
                if abs((w["top"] + w["bottom"]) / 2 - box.cy) < r
                and -r * 0.4 < (w["x0"] - box.x1) < 90]

        balloons.append(Balloon(
            id=f"P{page_index + 1}B{idx + 1}",
            item=item,
            qty=qty,
            raw_text=f"{item or '?'}/{qty_raw or '-'}",
            bbox=box,
            page_index=page_index,
            nearby_text=" ".join(near)[:80],
            is_ref=is_ref,
            qty_raw=qty_raw,
            source="outline",
        ))
    return balloons


def _looks_like_fraction(num: int, den: int) -> bool:
    return den in _FRACTION_DENOMS and num < den


def find_text_balloons(words: list[dict], drawn: list[Balloon], exclude: list[Box],
                       bom_items: set[str], page_index: int) -> list[Balloon]:
    """Callouts typed as plain text ("13/2") with no outline around them.

    Deliberately conservative: the item must already exist in the parts list,
    the token must sit outside every table, and imperial fractions are ignored.
    Everything found here is tagged confidence="low" so the UI can say so.
    """
    if not bom_items:
        return []
    taken = [b.bbox for b in drawn]
    found: list[Balloon] = []
    for w in words:
        m = re.fullmatch(r"(\d{1,3})\s*/\s*(\d{1,3})", w["text"].strip())
        if not m:
            continue
        item, qty = _norm_item(m.group(1)), int(m.group(2))
        if item not in bom_items:
            continue
        if _looks_like_fraction(int(m.group(1)), qty):
            continue
        box = Box(w["x0"], w["top"], w["x1"], w["bottom"])
        if any(t.contains(box, pad=2.0) for t in exclude):
            continue
        if any(t.overlaps(box) for t in taken):
            continue
        found.append(Balloon(
            id=f"P{page_index + 1}T{len(found) + 1}",
            item=item, qty=qty, raw_text=w["text"].strip(), bbox=box,
            page_index=page_index, qty_raw=str(qty),
            source="text", confidence="low",
        ))
    return found


# --------------------------------------------------------------------------- #
# views
# --------------------------------------------------------------------------- #
def find_views(words: list[dict]) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    for line in _group_lines(words):
        text = " ".join(w["text"] for w in line).upper()
        for m in VIEW_RE.finditer(text):
            label = re.sub(r"\s+", " ", f"{m.group(1)} {m.group(2)}").strip()
            if not any(v["label"] == label for v in views):
                views.append({"label": label, "bbox": _word_box(line)})
    return views


def assign_views(balloons: list[Balloon], views: list[dict], page_w: float, page_h: float) -> None:
    """Nearest-label assignment. Heuristic by nature, so the rules only ever use
    it to soften a verdict (flag ambiguity), never to raise a hard error."""
    if not views:
        return
    limit = 0.30 * ((page_w ** 2 + page_h ** 2) ** 0.5)
    for b in balloons:
        best, best_d = MAIN_VIEW, limit
        for v in views:
            d = ((b.bbox.cx - v["bbox"].cx) ** 2 + (b.bbox.cy - v["bbox"].cy) ** 2) ** 0.5
            if d < best_d:
                best, best_d = v["label"], d
        b.view = best


# --------------------------------------------------------------------------- #
# parts list
# --------------------------------------------------------------------------- #
_NITEM = {_norm(v) for v in HEADER_ITEM}
_NQTY = {_norm(v) for v in HEADER_QTY}
_NPN = {_norm(v) for v in HEADER_PN}
_NDESC = {_norm(v) for v in HEADER_DESC}
_NMAT = {_norm(v) for v in HEADER_MAT}
_NIGNORE = {_norm(v) for v in HEADER_IGNORE}


def _header_hints(text: str) -> set[str]:
    t = _norm(text)
    if not t:
        return set()
    hints: set[str] = set()
    if t in _NITEM:
        hints.add("item")
    if t in _NQTY:
        hints.add("qty")
    if t in _NPN or any(t.startswith(v) for v in _NPN):
        hints.add("part_number")
    if t in _NDESC:
        hints.add("description")
    if t in _NMAT:
        hints.add("material")
    if t in _NIGNORE:
        hints.add("ignore")
    return hints


def _merge_labels(band: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for w in band:
        char_w = (w["x1"] - w["x0"]) / max(len(w["text"]), 1)
        gap = w["x0"] - merged[-1]["x1"] if merged else 1e9
        if merged and gap < max(8.0, char_w * 1.8):
            merged[-1] = {**merged[-1], "text": merged[-1]["text"] + " " + w["text"], "x1": w["x1"]}
        else:
            merged.append(dict(w))
    return merged


def _accept_header(roles: set[str]) -> bool:
    data_roles = {"item", "qty", "part_number", "description", "material"} & roles
    if "qty" in roles and ({"item", "part_number", "description"} & roles):
        return True
    return len(data_roles) >= 3


def _find_header_row(words: list[dict]) -> list[dict] | None:
    best, best_score = None, 0.0
    for band in _group_lines(words):
        merged = _merge_labels(band)
        cells = [dict(w, hints=_header_hints(w["text"])) for w in merged if _header_hints(w["text"])]
        if not cells:
            continue
        roles = set().union(*[c["hints"] for c in cells])
        if not _accept_header(roles):
            continue
        score = len(roles) + 0.25 * len(cells)
        if score > best_score:
            best, best_score = cells, score
    return best


def _is_sequential(ints: list[int]) -> bool:
    if len(ints) < 2:
        return len(ints) == 1 and ints[0] in (0, 1)
    if ints[0] > 3:
        return False
    inc = sum(1 for a, b in zip(ints, ints[1:]) if b > a)
    return inc / (len(ints) - 1) >= 0.8


def _assign_roles(columns: list[dict]) -> None:
    def ints_in_order(vals):
        return [int(v) for v in vals if re.fullmatch(r"\d+", v.strip())]

    for col in columns:
        vals = [v for v in col["values"] if v.strip()]
        iv = ints_in_order(vals)
        col["_int_frac"] = len(iv) / len(vals) if vals else 0.0
        col["_alnum_frac"] = (sum(1 for v in vals if re.search(r"[A-Za-z]", v) and re.search(r"\d", v))
                              / len(vals)) if vals else 0.0
        col["_avg_len"] = (sum(len(v) for v in vals) / len(vals)) if vals else 0.0
        col["_seq"] = _is_sequential(iv)
        col["_space_frac"] = (sum(1 for v in vals if " " in v) / len(vals)) if vals else 0.0
        col["_nonblank"] = len(vals)
        col["_pure_int_frac"] = (sum(1 for v in vals if ITEM_RE.match(v.strip())) / len(vals)) if vals else 0.0
        col["role"] = None

    taken: set[str] = set()

    def claim(col, role):
        col["role"] = role
        taken.add(role)

    for col in columns:
        if "ignore" in col["hints"] and not (col["_alnum_frac"] >= 0.4 or col["_seq"]) \
                and not ({"item", "qty", "part_number", "description", "material"} & col["hints"]):
            claim(col, "ignore")

    pos = [c for c in columns if c["role"] is None and "item" in c["hints"]
           and c["_pure_int_frac"] >= 0.6 and c["_avg_len"] <= 4]
    if pos:
        seqd = [c for c in pos if c["_seq"]]
        claim(min(seqd or pos, key=lambda c: c["x0"]), "item")
    else:
        pos = [c for c in columns if c["role"] is None and c["_seq"]
               and c["_pure_int_frac"] >= 0.6 and c["_avg_len"] <= 4]
        if pos:
            claim(min(pos, key=lambda c: c["x0"]), "item")

    if "item" not in taken:
        pos = [c for c in columns if c["role"] is None and c["_pure_int_frac"] >= 0.6
               and c["_avg_len"] <= 4 and c["_nonblank"] >= 1]
        if pos:
            claim(min(pos, key=lambda c: c["x0"]), "item")

    qty = [c for c in columns if c["role"] is None and "qty" in c["hints"]]
    if not qty:
        qty = [c for c in columns if c["role"] is None and c["_int_frac"] >= 0.5
               and c["_avg_len"] <= 4 and not c["_seq"]]
    if qty and "qty" not in taken:
        claim(max(qty, key=lambda c: c["x0"]), "qty")

    def _code_score(c):
        long_numeric = c["_int_frac"] >= 0.6 and c["_avg_len"] >= 5
        return ("part_number" in c["hints"], long_numeric, c["_alnum_frac"], c["_avg_len"])

    pn = [c for c in columns if c["role"] is None and c["_space_frac"] < 0.35
          and "description" not in c["hints"]
          and ("part_number" in c["hints"] or c["_alnum_frac"] >= 0.4
               or (c["_int_frac"] >= 0.6 and c["_avg_len"] >= 5))]
    if pn and "part_number" not in taken:
        claim(max(pn, key=_code_score), "part_number")

    for role, key in (("material", "material"), ("description", "description")):
        cands = [c for c in columns if c["role"] is None and key in c["hints"]]
        if cands and role not in taken:
            claim(cands[0], role)

    for c in sorted([c for c in columns if c["role"] is None],
                    key=lambda c: (c["_space_frac"], c["_avg_len"]), reverse=True):
        if "description" not in taken:
            claim(c, "description")
        elif "material" not in taken:
            claim(c, "material")
        else:
            claim(c, "ignore")


_TABLE_SETTINGS = [
    {"vertical_strategy": "lines", "horizontal_strategy": "lines",
     "snap_tolerance": 4, "join_tolerance": 4, "intersection_tolerance": 4},
    {"vertical_strategy": "lines", "horizontal_strategy": "text",
     "snap_tolerance": 4, "join_tolerance": 4},
    {"vertical_strategy": "text", "horizontal_strategy": "text",
     "text_x_tolerance": 2, "text_y_tolerance": 2},
]


def _table_bbox(table) -> Box:
    x0, top, x1, bottom = table.bbox
    return Box(x0, top, x1, bottom)


def _row_seen(rows: list[BomRow], candidate: BomRow) -> bool:
    """Same item AND same place = the same physical row read twice.
    Same item in a different place is a genuine duplicate and is kept."""
    for r in rows:
        if str(r.item) != str(candidate.item):
            continue
        if r.bbox is None or candidate.bbox is None:
            return True
        if r.bbox.overlaps(candidate.bbox):
            return True
    return False


def _extract_with_pdfplumber(page):
    """Parse the parts list from ruled/implicit tables.

    One settings pass only: mixing passes used to merge the same physical table
    twice under slightly different geometry, which both invented rows and hid
    real duplicates.
    """
    for settings in _TABLE_SETTINGS:
        try:
            tables = page.find_tables(table_settings=settings)
        except Exception:
            continue
        if not tables:
            continue

        parsed = []
        for t in tables:
            info = _parse_pdfplumber_table(t, want_meta=True)
            if info is None:
                continue
            data_rows = sum(1 for r in info[0] if r.item is not None)
            parsed.append((data_rows, info, t))
        if not parsed:
            continue

        parsed.sort(key=lambda p: p[0], reverse=True)
        _, (rows, total, tbbox, spans, roles, header_y), primary = parsed[0]
        merged = list(rows)

        for _, _, t in parsed[1:]:
            for r in _parse_continuation_table(t, spans, roles, header_y):
                if not _row_seen(merged, r):
                    merged.append(r)

        merged = _merge_wrapped(merged)
        merged.sort(key=lambda r: _numkey(r.item))
        boxes = [_table_bbox(t) for _, _, t in parsed]
        if any(r.item is not None for r in merged):
            return merged, total, tbbox, True, boxes
    return [], None, None, False, []


def _parse_continuation_table(table, spans, role_by_col, header_y):
    grid = table.extract()
    if not grid:
        return []
    if _locate_grid_header(grid)[0] is not None:
        return []
    try:
        tbl_top = min(c[1] for row in table.rows for c in row.cells if c)
    except ValueError:
        return []
    if tbl_top <= header_y:
        return []

    out: list[BomRow] = []
    for ri in range(len(grid)):
        def cell(role):
            for ci, r in role_by_col.items():
                if r == role and ci < len(grid[ri]):
                    return (grid[ri][ci] or "").replace("\n", " ").strip()
            return ""
        item = _item_cell_value(cell("item"))
        if not item:
            continue
        qty_raw = cell("qty")
        out.append(BomRow(
            item=item,
            part_number=cell("part_number"),
            description=cell("description"),
            material=cell("material"),
            qty=int(qty_raw) if re.fullmatch(r"\d+", qty_raw) else None,
            qty_raw=qty_raw,
            bbox=_row_bbox(table.rows, ri),
        ))
    return out


def _parse_pdfplumber_table(table, want_meta: bool = False):
    grid = table.extract()
    if not grid or len(grid) < 2:
        return None

    col_spans = _table_column_spans(table)
    ncols = max(len(r) for r in grid)
    if len(col_spans) < ncols:
        col_spans += [(0.0, 0.0)] * (ncols - len(col_spans))

    header_idx, header_hints = _locate_grid_header(grid)
    if header_idx is None:
        return None

    columns = []
    for ci in range(ncols):
        x0, x1 = col_spans[ci] if ci < len(col_spans) else (0.0, 0.0)
        vals = []
        for ri in range(header_idx + 1, len(grid)):
            cell = grid[ri][ci] if ci < len(grid[ri]) else None
            vals.append((cell or "").replace("\n", " ").strip())
        columns.append({"x0": x0, "x1": x1, "hints": header_hints.get(ci, set()), "values": vals})

    _assign_roles(columns)
    role_by_col = {ci: columns[ci]["role"] for ci in range(ncols)}

    rows: list[BomRow] = []
    stated_total: int | None = None
    total_bbox: Box | None = None

    for ri in range(header_idx + 1, len(grid)):
        def cell(role: str) -> str:
            for ci, r in role_by_col.items():
                if r == role and ci < len(grid[ri]):
                    return (grid[ri][ci] or "").replace("\n", " ").strip()
            return ""

        row_bbox = _row_bbox(table.rows, ri)
        item_raw, qty_raw = cell("item"), cell("qty")
        item = _item_cell_value(item_raw)

        joined = " ".join((grid[ri][ci] or "") for ci in range(len(grid[ri]))).upper()
        if "TOTAL" in joined and re.fullmatch(r"\d+", qty_raw):
            stated_total, total_bbox = int(qty_raw), row_bbox
            continue

        if not item:
            if cell("description"):
                rows.append(BomRow(item=None, description=cell("description"), bbox=row_bbox))
            continue

        is_ar = _norm(qty_raw) in AR_TOKENS
        if not re.fullmatch(r"\d+", qty_raw) and not is_ar \
                and not (cell("part_number") or cell("description")):
            continue

        rows.append(BomRow(
            item=item,
            part_number=cell("part_number"),
            description=cell("description"),
            material=cell("material"),
            qty=int(qty_raw) if re.fullmatch(r"\d+", qty_raw) else None,
            qty_raw=qty_raw,
            bbox=row_bbox,
        ))

    rows = _merge_wrapped(rows)
    rows.sort(key=lambda r: _numkey(r.item))
    if not any(r.item is not None for r in rows):
        return None
    if want_meta:
        _hy = _row_bbox(table.rows, header_idx)
        header_y = _hy.y1 if _hy else 0.0
        return rows, stated_total, total_bbox, col_spans, role_by_col, header_y
    return rows, stated_total, total_bbox


def _table_column_spans(table) -> list[tuple[float, float]]:
    spans: dict[int, tuple[float, float]] = {}
    for row in table.rows:
        for ci, cell in enumerate(row.cells):
            if cell is None:
                continue
            x0, _, x1, _ = cell
            if ci not in spans:
                spans[ci] = (x0, x1)
            else:
                lo, hi = spans[ci]
                spans[ci] = (min(lo, x0), max(hi, x1))
    if not spans:
        return []
    return [spans.get(ci, (0.0, 0.0)) for ci in range(max(spans) + 1)]


def _row_bbox(table_rows, ri: int) -> Box | None:
    if ri >= len(table_rows):
        return None
    cells = [c for c in table_rows[ri].cells if c is not None]
    if not cells:
        return None
    x0 = min(c[0] for c in cells)
    top = min(c[1] for c in cells)
    x1 = max(c[2] for c in cells)
    bottom = max(c[3] for c in cells)
    return Box(x0, top, x1, bottom)


def _locate_grid_header(grid: list[list]) -> tuple[int | None, dict[int, set]]:
    best_idx, best_hints, best_score = None, {}, 0.0
    for ri in range(len(grid)):
        hints_by_col: dict[int, set] = {}
        for ci, cell in enumerate(grid[ri]):
            h = _header_hints((cell or "").replace("\n", " "))
            if h:
                hints_by_col[ci] = h
        roles = set().union(*hints_by_col.values()) if hints_by_col else set()
        if not _accept_header(roles):
            continue
        score = len(roles) + 0.25 * len(hints_by_col)
        if score > best_score:
            best_idx, best_hints, best_score = ri, hints_by_col, score
    return best_idx, best_hints


def _extract_text_anchored(page, words: list[dict]):
    header = _find_header_row(words)
    if not header:
        return [], None, None, False, []

    header = sorted(header, key=lambda w: w["x0"])
    header_cy = sum((w["top"] + w["bottom"]) / 2 for w in header) / len(header)

    anchors = [((w["x0"] + w["x1"]) / 2, w["hints"]) for w in header]
    left_gate = header[0]["x0"] - 30
    right_gate = header[-1]["x1"] + 40

    body = [w for w in words
            if (w["top"] + w["bottom"]) / 2 > header_cy + 1
            and left_gate <= (w["x0"] + w["x1"]) / 2 <= right_gate]
    if not body:
        return [], None, None, False, []

    body_lines = _group_lines(body)
    centres = [sum((w["top"] + w["bottom"]) / 2 for w in ln) / len(ln) for ln in body_lines]
    gaps = [b - a for a, b in zip(centres, centres[1:])]
    pitch = sorted(gaps)[len(gaps) // 2] if gaps else 14.0

    stop_gap = max(pitch * 2.5, 24.0)
    big_gap = max(pitch * 6.0, 60.0)

    def _looks_like_item_row(ln: list[dict]) -> bool:
        if not ln:
            return False
        first = min(ln, key=lambda w: w["x0"])
        return bool(re.fullmatch(r"\d{1,4}", first["text"].strip()))

    kept: list[list[dict]] = []
    prev = header_cy
    for ln, cy in zip(body_lines, centres):
        gap = cy - prev
        if kept and gap > stop_gap:
            if gap > big_gap or not _looks_like_item_row(ln):
                break
        kept.append(ln)
        prev = cy

    ncols = len(anchors)
    columns = [{"x0": anchors[i][0], "x1": anchors[i][0], "hints": anchors[i][1], "values": []}
               for i in range(ncols)]

    def nearest(cx: float) -> int:
        return min(range(ncols), key=lambda i: abs(cx - anchors[i][0]))

    row_records = []
    for ln in kept:
        rc: dict[int, list[dict]] = {}
        for w in ln:
            rc.setdefault(nearest((w["x0"] + w["x1"]) / 2), []).append(w)
        row_records.append((ln, rc))
        for ci in range(ncols):
            ws = sorted(rc.get(ci, []), key=lambda w: w["x0"])
            columns[ci]["values"].append(" ".join(w["text"] for w in ws).strip())

    _assign_roles(columns)
    role_by_col = {ci: columns[ci]["role"] for ci in range(ncols)}

    rows: list[BomRow] = []
    stated_total: int | None = None
    total_bbox: Box | None = None

    for ln, rc in row_records:
        def cell(role: str) -> str:
            for ci, r in role_by_col.items():
                if r == role and ci in rc:
                    ws = sorted(rc[ci], key=lambda w: w["x0"])
                    return " ".join(w["text"] for w in ws).strip()
            return ""

        bbox = _word_box(ln)
        item_raw, qty_raw = cell("item"), cell("qty")
        item = _item_cell_value(item_raw)

        joined = " ".join(w["text"] for w in ln).upper()
        if "TOTAL" in joined and re.fullmatch(r"\d+", qty_raw):
            stated_total, total_bbox = int(qty_raw), bbox
            continue
        if not item:
            if cell("description"):
                rows.append(BomRow(item=None, description=cell("description"), bbox=bbox))
            continue

        is_ar = _norm(qty_raw) in AR_TOKENS
        if not re.fullmatch(r"\d+", qty_raw) and not is_ar \
                and not (cell("part_number") or cell("description")):
            continue

        rows.append(BomRow(
            item=item,
            part_number=cell("part_number"),
            description=cell("description"),
            material=cell("material"),
            qty=int(qty_raw) if re.fullmatch(r"\d+", qty_raw) else None,
            qty_raw=qty_raw,
            bbox=bbox,
        ))

    rows = _merge_wrapped(rows)
    rows.sort(key=lambda r: _numkey(r.item))
    if not any(r.item is not None for r in rows):
        return [], None, None, False, []

    boxed = [r.bbox for r in rows if r.bbox]
    span = [Box(min(b.x0 for b in boxed), min(b.y0 for b in boxed),
                max(b.x1 for b in boxed), max(b.y1 for b in boxed))] if boxed else []
    return rows, stated_total, total_bbox, True, span


def find_bom(page, words: list[dict]):
    """-> (rows, stated_total, total_bbox, detected, table_boxes)"""
    rows, total, tbbox, ok, boxes = _extract_with_pdfplumber(page)
    if ok:
        return rows, total, tbbox, True, boxes
    rows, total, tbbox, ok, boxes = _extract_text_anchored(page, words)
    if ok:
        return rows, total, tbbox, True, boxes
    return [], None, None, False, []


def _merge_wrapped(rows: list[BomRow]) -> list[BomRow]:
    out: list[BomRow] = []
    for r in rows:
        is_real = (
            r.item
            and re.fullmatch(r"\d+", str(r.item))
            and (r.part_number or r.qty is not None or r.qty_raw or r.description)
        )
        if is_real:
            out.append(r)
        elif out and r.description:
            out[-1].description = (out[-1].description + " " + r.description).strip()
    return out


def find_notes(page) -> list[str]:
    words = page.extract_words()
    notes: list[str] = []
    for band in _group_lines(words):
        segment: list[dict] = []
        segments: list[list[dict]] = []
        for w in band:
            if segment and w["x0"] - segment[-1]["x1"] > 40:
                segments.append(segment)
                segment = []
            segment.append(w)
        if segment:
            segments.append(segment)
        for seg in segments:
            text = " ".join(w["text"] for w in seg).strip()
            if re.match(r"^\d+[.)]\s+\S", text) and len(text) > 12:
                notes.append(text)
    return notes[:20]


def find_title_block(page) -> dict[str, str]:
    text = (page.extract_text() or "").upper()
    tb: dict[str, str] = {}
    for label, pattern in (
        ("drawing_no", r"(?:DRAWING NO\.?|DWG\.? NO\.?|DRAWING NUMBER)\s*[:\s]\s*((?=[A-Z0-9\-/]*\d)[A-Z0-9\-/]{2,})"),
        ("revision", r"\bREV(?:ISION)?\.?\s*[:\s]\s*(\d{1,3}[A-Z]?|[A-Z]\d{0,2})\b"),
        ("scale", r"\bSCALE\s*[:\s]\s*([0-9]+\s*:\s*[0-9]+)"),
        ("sheet", r"\bSHEET\s*[:\s]\s*(\d+\s*OF\s*\d+)"),
    ):
        m = re.search(pattern, text)
        if m:
            tb[label] = m.group(1).strip()
    return tb


def _detect_split_balloons(page) -> bool:
    text = (page.extract_text() or "").upper()
    return any(h in text for h in SPLIT_BALLOON_HINTS)


def _detect_bom_extract(page) -> bool:
    text = (page.extract_text() or "").upper()
    return any(h in text for h in BOM_EXTRACT_HINTS)


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def render_pages(pdf_bytes: bytes, indices: list[int], dpi: int = RENDER_DPI) -> dict[int, str]:
    """Render several pages from a single pdfium handle."""
    out: dict[int, str] = {}
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        for i in indices:
            if i >= len(doc):
                continue
            bitmap = doc[i].render(scale=dpi / 72)
            buf = io.BytesIO()
            bitmap.to_pil().save(buf, format="PNG")
            out[i] = base64.b64encode(buf.getvalue()).decode()
    finally:
        doc.close()
    return out


def render_page(pdf_bytes: bytes, page_index: int = 0, dpi: int = RENDER_DPI) -> str:
    return render_pages(pdf_bytes, [page_index], dpi).get(page_index, "")


# --------------------------------------------------------------------------- #
# per-page parse
# --------------------------------------------------------------------------- #
def _parse_page(page, page_index: int) -> Sheet:
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    rows, total, total_bbox, detected, table_boxes = find_bom(page, words)
    split_hint = _detect_split_balloons(page)

    balloons = find_balloons(page, words, page_index=page_index,
                             exclude=table_boxes, allow_dash=split_hint)
    bom_items = {str(r.item) for r in rows if r.item is not None}
    balloons += find_text_balloons(words, balloons, table_boxes, bom_items, page_index)

    views = find_views(words)
    assign_views(balloons, views, float(page.width), float(page.height))

    for r in rows:
        r.page_index = page_index

    return Sheet(
        page_index=page_index,
        page_width=float(page.width),
        page_height=float(page.height),
        balloons=balloons,
        bom_rows=rows,
        stated_total=total,
        stated_total_bbox=total_bbox,
        notes=find_notes(page),
        title_block=find_title_block(page),
        views=views,
        bom_detected=detected,
        bom_is_extract=_detect_bom_extract(page),
        split_balloons=split_hint,
        table_boxes=table_boxes,
        words=words,
    )


def parse_sheet(pdf_bytes: bytes, page_index: int = 0, render: bool = True) -> Sheet:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        if page_index >= len(pdf.pages):
            raise ValueError(f"page {page_index + 1} does not exist in this PDF")
        sheet = _parse_page(pdf.pages[page_index], page_index)
    if render:
        sheet.page_image_b64 = render_page(pdf_bytes, page_index)
    return sheet


# --------------------------------------------------------------------------- #
# salvage: rebuild BOM rows the table parser missed, using raw words
# --------------------------------------------------------------------------- #
def _salvage_relaxed_match(left: str, it: str) -> bool:
    if left == it:
        return True
    return bool(re.fullmatch(r"[A-Za-z,\.]+" + re.escape(it), left))


def _salvage_missing_bom_items(master: Sheet | None, balloons: list[Balloon]) -> None:
    if master is None or not master.words:
        return

    by_item: dict[str, BomRow] = {}
    for r in master.bom_rows:
        if r.item is not None:
            r.item = _norm_item(r.item)
            by_item.setdefault(str(r.item), r)

    def _needs_fix(row: BomRow | None) -> bool:
        if row is None:
            return True
        if not row.part_number:
            return True
        if row.qty is None and not row.is_ar:
            return True
        return False

    balloon_items = {_norm_item(b.item) for b in balloons if b.item}
    candidates = set(balloon_items) | set(by_item.keys())
    targets = sorted((it for it in candidates if it and _needs_fix(by_item.get(it))), key=_numkey)
    if not targets:
        return

    lines = _group_lines(master.words)
    if not lines:
        return

    pn_xs = [w["x0"] for ln in lines for w in ln
             if _PN_CODE_RE.fullmatch(w["text"].strip()) and len(w["text"].strip()) >= 5]
    dom_pn_x = statistics.median(pn_xs) if pn_xs else None

    def _fields(ln: list[dict], pnw: dict):
        pn = pnw["text"].strip()
        right = sorted([w for w in ln if w["x0"] > pnw["x0"] + 1], key=lambda w: w["x0"])
        toks = [w["text"].strip() for w in right]
        while toks and re.fullmatch(r"[A-Z]", toks[-1]):
            toks.pop()                      # trailing zone/grid letter, not a qty
        qty_raw = toks[-1] if toks else ""
        body = toks[:-1] if toks else []
        desc_toks = [a for a in body
                     if re.search(r"[A-Za-z]", a)
                     and not re.fullmatch(r"[A-Z]", a)
                     and not (len(a) <= 3 and "," in a)]
        desc = " ".join(desc_toks).strip().lstrip(", ").strip()
        qty = int(qty_raw) if re.fullmatch(r"\d+", qty_raw) else None
        return pn, desc, qty, qty_raw, _word_box(ln)

    def _find_line_for(it: str, strict: bool):
        cands = []
        for ln in lines:
            s = sorted(ln, key=lambda w: w["x0"])
            for i in range(1, len(s)):
                pn = s[i]["text"].strip()
                if not (_PN_CODE_RE.fullmatch(pn) and len(pn) >= 5):
                    continue
                left = s[i - 1]["text"].strip()
                ok = (left == it) if strict else _salvage_relaxed_match(left, it)
                if not ok:
                    continue
                dist = abs(s[i]["x0"] - dom_pn_x) if dom_pn_x is not None else 0.0
                cands.append((dist, ln, s[i]))
        if not cands:
            return None
        cands.sort(key=lambda c: c[0])
        return cands[0][1], cands[0][2]

    for it in targets:
        found = _find_line_for(it, strict=True) or _find_line_for(it, strict=False)
        if not found:
            continue
        pn, desc, qty, qty_raw, bbox = _fields(*found)
        existing = by_item.get(it)
        if existing is None:
            row = BomRow(item=it, part_number=pn, description=desc, qty=qty,
                         qty_raw=qty_raw, page_index=master.page_index, bbox=bbox)
            master.bom_rows.append(row)
            by_item[it] = row
        else:
            existing.part_number = pn
            existing.description = desc or existing.description
            existing.qty = qty
            existing.qty_raw = qty_raw
            if existing.bbox is None:
                existing.bbox = bbox

    master.bom_rows.sort(key=lambda r: _numkey(r.item))


# --------------------------------------------------------------------------- #
# document assembly
# --------------------------------------------------------------------------- #
def _row_richness(r: BomRow) -> int:
    score = 0
    if r.part_number:
        score += 2
    if r.description:
        score += 1
    if r.qty is not None:
        score += 1
    if r.is_ar:
        score += 1
    return score


def _reconcile_bom(sheets: list[Sheet], master: Sheet | None):
    """Collapse every sheet's rows into one row per item, and remember what
    disagreed on the way (duplicates on one sheet, conflicts between sheets)."""
    reconciled: dict[str, BomRow] = {}
    order: list[str] = []
    seen: dict[str, list[BomRow]] = {}

    ordered_sheets = ([master] if master else []) + [s for s in sheets if s is not master]

    for s in ordered_sheets:
        for r in s.bom_rows:
            if r.item is None:
                continue
            txt = str(r.item).strip()
            if txt.isdigit() and int(txt) == 0:
                continue
            key = _norm_item(txt)
            seen.setdefault(key, []).append(r)
            cur = reconciled.get(key)
            if cur is None:
                reconciled[key] = r
                order.append(key)
            elif _row_richness(r) > _row_richness(cur):
                reconciled[key] = r

    duplicates: dict[str, list[BomRow]] = {}
    conflicts: dict[str, list[BomRow]] = {}
    for key, rows in seen.items():
        same_sheet: dict[int, list[BomRow]] = {}
        for r in rows:
            same_sheet.setdefault(r.page_index, []).append(r)
        for page_rows in same_sheet.values():
            if len(page_rows) > 1:
                duplicates[key] = page_rows
                break
        sigs = {r.signature() for r in rows if r.qty is not None or r.part_number}
        if len(sigs) > 1:
            conflicts[key] = rows

    return [reconciled[k] for k in order], duplicates, conflicts


def _infer_split(sheets: list[Sheet], bom_by_item: dict[str, BomRow]) -> bool:
    """Split-balloon convention without the note on the sheet.

    True when at least one item carries several qty-bearing balloons whose sum
    matches the parts list while no single balloon does. Without this a drawing
    using 13/2 + 13/8 for a qty of 10 produced two bogus 'balloon says 2, list
    says 10' errors.
    """
    by_item: dict[str, list[Balloon]] = {}
    for s in sheets:
        for b in s.balloons:
            if b.item and b.qty is not None and not b.is_ref:
                by_item.setdefault(b.item, []).append(b)

    for item, blist in by_item.items():
        if len(blist) < 2:
            continue
        row = bom_by_item.get(item)
        if row is None or row.qty is None:
            continue
        qtys = [b.qty for b in blist]
        if any(q == row.qty for q in qtys):
            continue
        if sum(qtys) == row.qty:
            return True
        per_sheet: dict[int, int] = {}
        for b in blist:
            per_sheet[b.page_index] = per_sheet.get(b.page_index, 0) + b.qty
        if any(v == row.qty for v in per_sheet.values()):
            return True
    return False


def parse_document(pdf_bytes: bytes, filename: str = "", render: bool = False) -> Document:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        sheets = [_parse_page(page, i) for i, page in enumerate(pdf.pages)]

    def bom_score(s: Sheet) -> tuple[int, int]:
        data_rows = sum(1 for r in s.bom_rows if r.item is not None)
        return (0 if s.bom_is_extract else 1, data_rows)

    candidates = [s for s in sheets if s.bom_detected and any(r.item for r in s.bom_rows)]
    master = max(candidates, key=bom_score) if candidates else (sheets[0] if sheets else None)
    master_idx = master.page_index if master else 0

    all_balloons: list[Balloon] = []
    for s in sheets:
        all_balloons.extend(s.balloons)

    _salvage_missing_bom_items(master, all_balloons)

    reconciled, duplicates, conflicts = _reconcile_bom(sheets, master)
    bom_by_item = {str(r.item): r for r in reconciled if r.item is not None}

    hinted = any(s.split_balloons for s in sheets)
    inferred = False if hinted else _infer_split(sheets, bom_by_item)

    doc = Document(
        filename=filename,
        sheets=sheets,
        master_page_index=master_idx,
        bom_rows=reconciled,
        raw_bom_rows=[r for s in sheets for r in s.bom_rows if r.item is not None],
        balloons=all_balloons,
        stated_total=master.stated_total if master else None,
        stated_total_bbox=master.stated_total_bbox if master else None,
        split_balloons=hinted or inferred,
        split_inferred=inferred,
        duplicate_rows=duplicates,
        row_conflicts=conflicts,
    )

    if render:
        images = render_pages(pdf_bytes, [s.page_index for s in sheets])
        for s in sheets:
            s.page_image_b64 = images.get(s.page_index, "")
        doc.page_image_b64 = images.get(master_idx, "")
    return doc
