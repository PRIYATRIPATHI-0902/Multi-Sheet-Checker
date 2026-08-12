"""
app.py - FastAPI service for the assembly sheet checker.

POST /api/analyze   multipart: file=<pdf>, page=<int>, use_ai=<bool>
GET  /api/health
Static frontend is served from ../frontend/dist when it has been built.
"""
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

import ai
import extractor
import rules

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("checker")

MAX_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))

app = FastAPI(title="Assembly BOM / Balloon Checker", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "ai_configured": bool(os.getenv("GEMINI_API_KEY"))}


@app.post("/api/analyze")
async def analyze(
    file: UploadFile = File(...),
    page: int = Form(0),
    use_ai: bool = Form(True),
) -> JSONResponse:
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF drawing.")

    data = await file.read()
    if len(data) > MAX_MB * 1024 * 1024:
        raise HTTPException(413, f"File is larger than {MAX_MB} MB.")

    try:
        sheet = extractor.parse_sheet(data, page_index=page)
    except Exception as exc:
        log.exception("parse failed")
        raise HTTPException(422, f"Could not read the drawing: {exc}")

    issues = rules.run_rules(sheet)

    ai_status, ai_message = "skipped", "AI review turned off for this run."
    if use_ai:
        try:
            result = ai.review(sheet, issues)
            issues = ai.merge(sheet, issues, result)
            ai_status, ai_message = "ok", "Reviewed by Gemini."
        except ai.AIUnavailable as exc:
            ai_status = "unavailable"
            ai_message = f"Rule checks only - {exc}"
            log.warning("AI unavailable: %s", exc)
        except Exception as exc:
            ai_status = "error"
            ai_message = f"Rule checks only - AI review failed: {exc}"
            log.exception("AI review failed")

    warnings: list[str] = []
    if not sheet.balloons:
        warnings.append(
            "No balloons were detected. The checker looks for small circles containing a "
            "number; if the sheet is a scan or the balloon text is outlined, nothing is found."
        )
    if not sheet.bom_rows:
        warnings.append(
            "No parts list was detected. A table with ITEM and QTY column headings is required."
        )

    return JSONResponse({
        "filename": file.filename,
        "page": page,
        "sheet": sheet.to_dict(),
        "page_image": f"data:image/png;base64,{sheet.page_image_b64}",
        "issues": issues,
        "summary": rules.summarise(issues),
        "ai": {"status": ai_status, "message": ai_message},
        "warnings": warnings,
    })


_dist = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="static")
