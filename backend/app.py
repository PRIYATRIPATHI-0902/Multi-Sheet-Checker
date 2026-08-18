from __future__ import annotations

import logging
import os
import pathlib

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

try:  # optional convenience: read backend/.env if python-dotenv is installed
    from dotenv import load_dotenv
    load_dotenv(pathlib.Path(__file__).with_name(".env"))
except ImportError:
    pass

import extractor
import rules

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("checker")

MAX_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))

app = FastAPI(title="Assembly BOM / Balloon Checker", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


def _backfill_pages(issues: list[dict], master_idx: int) -> list[dict]:
    """Safety net only. rules.py now stamps the sheet on every target as it is
    built, so nothing here should normally fire."""
    for issue in issues:
        for t in issue.get("targets", []):
            if not isinstance(t.get("page"), int):
                t["page"] = master_idx
        issue["pages"] = sorted({t["page"] for t in issue.get("targets", [])})
    return issues


@app.post("/api/analyze")
async def analyze(
    file: UploadFile = File(...),
    # Ignored: every sheet in the PDF is read and reconciled. Kept so older
    # clients keep working.
    page: int = Form(0),
) -> JSONResponse:
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF drawing.")

    data = await file.read()
    if len(data) > MAX_MB * 1024 * 1024:
        raise HTTPException(413, f"File is larger than {MAX_MB} MB.")

    try:
        doc = extractor.parse_document(data, filename=file.filename or "", render=True)
    except Exception as exc:
        log.exception("parse failed")
        raise HTTPException(422, f"Could not read the drawing: {exc}")

    issues = _backfill_pages(rules.run_rules(doc), doc.master_page_index)

    sheets_out: list[dict] = []
    for s in doc.sheets:
        sd = s.to_dict()
        sd["page_image"] = f"data:image/png;base64,{s.page_image_b64}" if s.page_image_b64 else ""
        sheets_out.append(sd)

    warnings: list[str] = []
    if not doc.balloons:
        warnings.append(
            "No balloons were found on any sheet. The checker looks for a small outline "
            "with a number inside it; a scanned sheet or outlined text reads as empty."
        )
    if not doc.bom_rows:
        warnings.append(
            "No parts list was found on any sheet. A table with ITEM and QTY headings is required."
        )
    if len(doc.assemblies) > 1:
        names = ", ".join(a.label for a in doc.assemblies)
        warnings.append(
            f"{len(doc.assemblies)} separate drawings were found in this PDF ({names}). "
            "Each is checked against its own parts list — item 4 on one drawing is not "
            "assumed to be item 4 on another."
        )
    elif len(doc.sheets) > 1:
        warnings.append(
            f"{len(doc.sheets)} sheets were read as one assembly. The parts list on sheet "
            f"{doc.master_page_index + 1} is treated as authoritative, and balloons from every "
            "sheet are checked against it."
        )
    if doc.split_inferred:
        warnings.append(
            "Split balloons were assumed: several balloons carry a per-place quantity that adds "
            "up to the parts-list quantity. Add a note on the sheet to make this explicit."
        )
    low = sum(1 for b in doc.balloons if b.confidence == "low")
    if low:
        warnings.append(
            f"{low} callout(s) were read from plain text with no outline drawn around them. "
            "They are included in the checks — confirm them by eye."
        )

    master = sheets_out[doc.master_page_index] if sheets_out else None

    return JSONResponse({
        "filename": file.filename,
        "master_page_index": doc.master_page_index,
        "sheets": sheets_out,
        "issues": issues,
        "summary": rules.summarise(issues),
        "assemblies": [
            {
                "label": a.label,
                "pages": [s.page_index for s in a.sheets],
                "master_page_index": a.master_page_index,
                "bom_rows": len(a.bom_rows),
                "balloons": len(a.balloons),
            }
            for a in doc.assemblies
        ],
        "split_balloons": doc.split_balloons,
        "split_inferred": doc.split_inferred,
        # Kept for frontend compatibility; the app no longer calls any AI service.
        "ai": {"status": "skipped", "message": "Rule checks only (AI review disabled)."},
        "warnings": warnings,
        # ---- backwards-compatible single-sheet fields (point at the master) ---
        "page": doc.master_page_index,
        "sheet": master,
        "page_image": master["page_image"] if master else "",
    })


_dist = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="static")
