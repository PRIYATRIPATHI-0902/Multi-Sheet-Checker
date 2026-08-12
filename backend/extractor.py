"""
extractor.py
Pulls structured data out of an assembly drawing PDF:
  - balloons  (split balloons: item number over quantity)
  - parts list / BOM rows
  - a rendered page image for the frontend overlay

Everything is returned in PDF points with the origin at the TOP-LEFT of the
page, which is the same convention pdfplumber uses and makes the frontend
overlay maths trivial (x / page_width * 100 -> percent).
"""
from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass, field, asdict
from typing import Any

import pdfplumber
import pypdfium2 as pdfium

RENDER_DPI = 150

# a balloon circle on a normal sheet sits in this size band (points)
BALLOON_MIN_D = 18.0
BALLOON_MAX_D = 48.0
BALLOON_ROUNDNESS = 0.15      # |w-h| / w must be under this

HEADER_ITEM = {"ITEM", "ITEM NO", "ITEMNO", "NO", "POS", "FIND"}
HEADER_QTY = {"QTY", "QTY.", "QUANTITY", "Q'TY", "QNTY"}
HEADER_PN = {"PART", "PART NUMBER", "PART NO", "PARTNO", "DWG", "IDENTIFIER"}
HEADER_DESC = {"DESCRIPTION", "TITLE", "NOMENCLATURE", "PART NAME"}
HEADER_MAT = {"MATERIAL", "MATL", "SPEC", "SPECIFICATION"}


@dataclass
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    def as_list(self) -> list[float]:
        return [round(self.x0, 2), round(self.y0, 2), round(self.x1, 2), round(self.y1, 2)]


@dataclass
class Balloon:
    id: str
    item: str | None
    qty: int | None
    raw_text: str
    bbox: Box
    nearby_text: str = ""

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
    bbox: Box | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bbox"] = self.bbox.as_list() if self.bbox else None
        return d


@dataclass
class Sheet:
    page_width: float
    page_height: float
    balloons: list[Balloon] = field(default_factory=list)
    bom_rows: list[BomRow] = field(default_factory=list)
    stated_total: int | None = None
    stated_total_bbox: Box | None = None
    notes: list[str] = field(default_factory=list)
    title_block: dict[str, str] = field(default_factory=dict)
    page_image_b64: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_width": self.page_width,
            "page_height": self.page_height,
            "balloons": [b.to_dict() for b in self.balloons],
            "bom_rows": [r.to_dict() for r in self.bom_rows],
            "stated_total": self.stated_total,
            "stated_total_bbox": self.stated_total_bbox.as_list() if self.stated_total_bbox else None,
            "notes": self.notes,
            "title_block": self.title_block,
        }


# --------------------------------------------------------------------------- #
# balloons
# --------------------------------------------------------------------------- #
def _circle_like(obj: dict) -> bool:
    w = obj["x1"] - obj["x0"]
    h = obj["bottom"] - obj["top"]
    if not (BALLOON_MIN_D <= w <= BALLOON_MAX_D):
        return False
    if w <= 0 or abs(w - h) / w > BALLOON_ROUNDNESS:
        return False
    return True


def find_balloons(page, words: list[dict]) -> list[Balloon]:
    """
    A balloon is a small circle with at least one number inside it.
    Requiring text inside is what separates balloons from bolt heads,
    washers, holes and other small circles in the geometry.
    """
    candidates = [c for c in page.curves if _circle_like(c)]
    # de-duplicate concentric/overlapping paths (fill + stroke drawn separately)
    unique: list[dict] = []
    for c in candidates:
        cx, cy = (c["x0"] + c["x1"]) / 2, (c["top"] + c["bottom"]) / 2
        if any(abs(cx - (u["x0"] + u["x1"]) / 2) < 3 and abs(cy - (u["top"] + u["bottom"]) / 2) < 3
               for u in unique):
            continue
        unique.append(c)

    balloons: list[Balloon] = []
    for idx, c in enumerate(sorted(unique, key=lambda o: (o["top"], o["x0"]))):
        cx, cy = (c["x0"] + c["x1"]) / 2, (c["top"] + c["bottom"]) / 2
        r = (c["x1"] - c["x0"]) / 2
        inside = [
            w for w in words
            if ((w["x0"] + w["x1"]) / 2 - cx) ** 2 + ((w["top"] + w["bottom"]) / 2 - cy) ** 2 < (r * 0.95) ** 2
        ]
        if not inside:
            continue
        if not any(re.search(r"\d", w["text"]) for w in inside):
            continue

        upper = [w for w in inside if (w["top"] + w["bottom"]) / 2 < cy]
        lower = [w for w in inside if (w["top"] + w["bottom"]) / 2 >= cy]

        item = " ".join(w["text"] for w in sorted(upper, key=lambda w: w["x0"])).strip() or None
        qty_txt = " ".join(w["text"] for w in sorted(lower, key=lambda w: w["x0"])).strip()

        # plain (non-split) balloon: single value = item number, quantity not stated
        if item is None and qty_txt:
            item, qty_txt = qty_txt, ""

        qty = int(qty_txt) if re.fullmatch(r"\d+", qty_txt) else None

        # any short annotation sitting just outside the balloon (e.g. a part number note)
        near = [
            w["text"] for w in words
            if abs((w["top"] + w["bottom"]) / 2 - cy) < r
            and -r * 0.4 < (w["x0"] - (cx + r)) < 90
        ]

        balloons.append(Balloon(
            id=f"B{idx + 1}",
            item=item,
            qty=qty,
            raw_text=f"{item or '?'}/{qty_txt or '-'}",
            bbox=Box(c["x0"], c["top"], c["x1"], c["bottom"]),
            nearby_text=" ".join(near)[:80],
        ))
    return balloons


# --------------------------------------------------------------------------- #
# parts list
# --------------------------------------------------------------------------- #
def _classify_header(text: str) -> str | None:
    t = text.upper().strip().rstrip(".")
    if t in HEADER_ITEM:
        return "item"
    if t in HEADER_QTY:
        return "qty"
    if t in HEADER_MAT:
        return "material"
    if t in HEADER_DESC:
        return "description"
    if any(t.startswith(p) for p in HEADER_PN):
        return "part_number"
    return None


def _find_header_row(words: list[dict]) -> list[dict] | None:
    """Group words into baselines and return the one that looks like a BOM header."""
    rows: dict[int, list[dict]] = {}
    for w in words:
        rows.setdefault(round(w["top"] / 4), []).append(w)

    best, best_score = None, 0
    for band in rows.values():
        # merge adjacent words so "PART NUMBER" is seen as one label
        band = sorted(band, key=lambda w: w["x0"])
        merged: list[dict] = []
        for w in band:
            if merged and w["x0"] - merged[-1]["x1"] < 6:
                merged[-1] = {**merged[-1], "text": merged[-1]["text"] + " " + w["text"], "x1": w["x1"]}
            else:
                merged.append(dict(w))
        kinds = {}
        for w in merged:
            k = _classify_header(w["text"])
            if k and k not in kinds:
                kinds[k] = w
        score = len(kinds)
        if "item" in kinds and "qty" in kinds and score > best_score:
            best, best_score = [dict(w, kind=_classify_header(w["text"])) for w in merged
                                if _classify_header(w["text"])], score
    return best


def _table_region(page, x0: float, x1: float, header_top: float) -> tuple[float, float]:
    """
    Vertical extent of the parts list, taken from the ruling rectangles that sit
    inside the header's column band. Falls back to the whole page.
    """
    width = x1 - x0
    tops, bottoms = [], []
    for r in page.rects + page.lines:
        if r["x0"] >= x0 - 12 and r["x1"] <= x1 + 12 and (r["x1"] - r["x0"]) > 0.55 * width:
            tops.append(r["top"])
            bottoms.append(r["bottom"])
    if not tops:
        return 0.0, float(page.height)
    top, bottom = min(tops), max(bottoms)
    # keep only the block continuous with the header row
    if not (top - 6 <= header_top <= bottom + 6):
        return 0.0, float(page.height)
    return top - 4, bottom + 4


def _column_edges(page, x0: float, x1: float, y0: float, y1: float) -> list[float]:
    """Cluster the x positions of vertical ruling lines inside the table."""
    xs: list[float] = []
    for ln in page.lines:
        if abs(ln["x1"] - ln["x0"]) < 1 and ln["bottom"] > y0 and ln["top"] < y1:
            if x0 - 12 <= ln["x0"] <= x1 + 12:
                xs.append((ln["x0"] + ln["x1"]) / 2)
    for r in page.rects:
        if r["top"] >= y0 - 4 and r["bottom"] <= y1 + 4:
            for x in (r["x0"], r["x1"]):
                if x0 - 12 <= x <= x1 + 12:
                    xs.append(x)
    xs.sort()
    edges: list[float] = []
    for x in xs:
        if not edges or x - edges[-1] > 6:
            edges.append(x)
    return edges


def find_bom(page, words: list[dict]) -> tuple[list[BomRow], int | None, Box | None]:
    header = _find_header_row(words)
    if not header:
        return [], None, None

    header = sorted(header, key=lambda w: w["x0"])
    header_top = min(w["top"] for w in header)
    header_bottom = max(w["bottom"] for w in header)

    approx_x0 = header[0]["x0"] - 24
    approx_x1 = header[-1]["x1"] + 34
    y0, y1 = _table_region(page, approx_x0, approx_x1, header_top)
    edges = _column_edges(page, approx_x0, approx_x1, y0, y1)

    bounds: list[tuple[float, float, str]] = []
    if len(edges) >= len(header) + 1:
        # real ruled columns: label each band by the header word inside it
        for left, right in zip(edges, edges[1:]):
            kind = next((w["kind"] for w in header
                         if left <= (w["x0"] + w["x1"]) / 2 <= right), None)
            if kind:
                bounds.append((left, right, kind))
    if len(bounds) < 2:
        # unruled table: split half-way between headings
        for i, w in enumerate(header):
            left = w["x0"] - 24 if i == 0 else (header[i - 1]["x1"] + w["x0"]) / 2
            right = w["x1"] + 34 if i == len(header) - 1 else (w["x1"] + header[i + 1]["x0"]) / 2
            bounds.append((left, right, w["kind"]))

    table_x0, table_x1 = bounds[0][0], bounds[-1][1]

    body = [
        w for w in words
        if table_x0 <= (w["x0"] + w["x1"]) / 2 <= table_x1
        and y0 <= w["top"] and w["bottom"] <= y1
        and not (header_top - 2 <= w["top"] <= header_bottom + 2)
    ]

    lines: dict[int, list[dict]] = {}
    for w in body:
        lines.setdefault(round(w["top"] / 4), []).append(w)

    rows: list[BomRow] = []
    stated_total: int | None = None
    total_bbox: Box | None = None

    for key in sorted(lines):
        band = lines[key]
        cells: dict[str, list[dict]] = {}
        for w in band:
            cx = (w["x0"] + w["x1"]) / 2
            for left, right, kind in bounds:
                if left <= cx <= right:
                    cells.setdefault(kind, []).append(w)
                    break

        def cell(kind: str) -> str:
            ws = sorted(cells.get(kind, []), key=lambda w: w["x0"])
            return " ".join(w["text"] for w in ws).strip()

        item, qty_raw = cell("item"), cell("qty")
        bbox = Box(min(w["x0"] for w in band), min(w["top"] for w in band),
                   max(w["x1"] for w in band), max(w["bottom"] for w in band))

        joined = " ".join(w["text"] for w in band).upper()
        if "TOTAL" in joined and re.fullmatch(r"\d+", qty_raw):
            stated_total, total_bbox = int(qty_raw), bbox
            continue

        if not re.fullmatch(r"\d+", item):
            continue
        # guard against page furniture (zone letters, stray numerals) that happens
        # to line up with the table columns
        if not re.fullmatch(r"\d+", qty_raw) and not (cell("part_number") and cell("description")):
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

    rows.sort(key=lambda r: int(r.item))
    return rows, stated_total, total_bbox


# --------------------------------------------------------------------------- #
# free text (notes + title block) - context for the AI pass
# --------------------------------------------------------------------------- #
def find_notes(page) -> list[str]:
    """
    Note lines, rebuilt from words. Splitting a text line wherever there is a
    large horizontal gap stops unrelated columns (like the parts list) being
    concatenated onto the end of a note.
    """
    words = page.extract_words()
    lines: dict[int, list[dict]] = {}
    for w in words:
        lines.setdefault(round(w["top"] / 4), []).append(w)

    notes: list[str] = []
    for key in sorted(lines):
        band = sorted(lines[key], key=lambda w: w["x0"])
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
        ("drawing_no", r"(?:DRAWING NO\.?|DWG\.? NO\.?)\s*[:\s]\s*([A-Z0-9\-_/]+)"),
        ("revision", r"\bREV\.?\s*[:\s]\s*([A-Z0-9]{1,3})\b"),
        ("scale", r"\bSCALE\s*[:\s]\s*([0-9]+\s*:\s*[0-9]+)"),
        ("sheet", r"\bSHEET\s*[:\s]\s*(\d+\s*OF\s*\d+)"),
    ):
        m = re.search(pattern, text)
        if m:
            tb[label] = m.group(1).strip()
    return tb


# --------------------------------------------------------------------------- #
# page raster
# --------------------------------------------------------------------------- #
def render_page(pdf_bytes: bytes, page_index: int = 0, dpi: int = RENDER_DPI) -> str:
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        bitmap = doc[page_index].render(scale=dpi / 72)
        buf = io.BytesIO()
        bitmap.to_pil().save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()
    finally:
        doc.close()


# --------------------------------------------------------------------------- #
def parse_sheet(pdf_bytes: bytes, page_index: int = 0) -> Sheet:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        if page_index >= len(pdf.pages):
            raise ValueError(f"page {page_index + 1} does not exist in this PDF")
        page = pdf.pages[page_index]
        words = page.extract_words(keep_blank_chars=False, use_text_flow=False)

        rows, total, total_bbox = find_bom(page, words)
        sheet = Sheet(
            page_width=float(page.width),
            page_height=float(page.height),
            balloons=find_balloons(page, words),
            bom_rows=rows,
            stated_total=total,
            stated_total_bbox=total_bbox,
            notes=find_notes(page),
            title_block=find_title_block(page),
        )

    sheet.page_image_b64 = render_page(pdf_bytes, page_index)
    return sheet
