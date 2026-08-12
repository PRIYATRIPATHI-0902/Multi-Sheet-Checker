"""
ai.py
Gemini pass over the sheet.

Division of labour: the rule engine owns the arithmetic, Gemini owns judgement.
It writes the engineer-facing explanation for each rule hit and looks for the
problems that counting cannot find - part numbers quoted in a leader note that
disagree with the parts list, general notes that contradict a quantity, a view
that visibly shows a different number of instances than the balloon claims.

Gemini never returns coordinates. It refers to balloon ids and item numbers,
and the server resolves those to boxes, so an overlay marker can never land in
the wrong place because of a hallucinated bounding box.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from extractor import Sheet

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
TIMEOUT = float(os.getenv("GEMINI_TIMEOUT", "90"))

SYSTEM = """You are a senior mechanical checker reviewing an assembly drawing before release.
You are given (a) an image of the sheet, (b) the balloons and parts list rows already
extracted from it by a parser, and (c) findings from a deterministic rule engine.

The rule engine has ALREADY found every arithmetic disagreement between balloon
quantities and parts list quantities, every orphan balloon, every un-ballooned row,
duplicate items, total-row errors and numbering gaps. Do not repeat those.

Your job is two things.

1. explanations - for each rule finding, write the practical consequence in one or two
   sentences an engineer would accept: what goes wrong downstream if this ships.
   Be concrete and specific to the part involved. No filler, no restating the numbers.

2. additional_issues - only problems the rule engine structurally cannot see:
   - a part number, revision or description quoted in a leader note, general note or
     annotation that disagrees with the parts list row for that item
   - a general note that contradicts a quantity, material or process in the parts list
   - a view that visibly shows a different number of instances of a part than the
     balloon or parts list states (count what you can actually see in the image)
   - a balloon whose leader clearly points at a feature inconsistent with its
     description in the parts list
   - the same item number used on two obviously different physical parts
   - missing material, missing part number, or a placeholder left in a row
   Report nothing you cannot point to in the image or the supplied data. An empty
   list is a correct answer. Do not speculate.

severity: "error" for something that would cause a wrong build or wrong purchase,
"warning" for something ambiguous a checker must resolve, "info" for a drafting
standard observation.

Reply with JSON only, matching this shape exactly:
{
  "explanations": [{"id": "R1", "detail": "...", "recommendation": "..."}],
  "additional_issues": [{
     "code": "SHORT_UPPER_SNAKE_CODE",
     "severity": "error|warning|info",
     "title": "one line, under 90 characters",
     "detail": "two or three sentences",
     "recommendation": "one sentence",
     "item": "item number this concerns, or null",
     "refers_to": {"balloon_ids": ["B3"], "bom_items": ["4"]}
  }]
}"""


class AIUnavailable(RuntimeError):
    pass


def _payload(sheet: Sheet, rule_issues: list[dict[str, Any]]) -> dict[str, Any]:
    context = {
        "title_block": sheet.title_block,
        "balloons": [
            {"id": b.id, "item": b.item, "qty_in_balloon": b.qty,
             "annotation_beside_balloon": b.nearby_text or None}
            for b in sheet.balloons
        ],
        "parts_list": [
            {"item": r.item, "part_number": r.part_number, "description": r.description,
             "material": r.material, "qty": r.qty}
            for r in sheet.bom_rows
        ],
        "stated_total_parts_count": sheet.stated_total,
        "general_notes": sheet.notes,
        "rule_findings": [
            {"id": i["id"], "code": i["code"], "item": i["item"], "title": i["title"]}
            for i in rule_issues
        ],
    }
    return context


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("model did not return JSON")
    return json.loads(text[start:end + 1])


def review(sheet: Sheet, rule_issues: list[dict[str, Any]],
           api_key: str | None = None, model: str | None = None) -> dict[str, Any]:
    key = api_key or os.getenv("GEMINI_API_KEY")
    if not key:
        raise AIUnavailable("GEMINI_API_KEY is not set")

    model = model or DEFAULT_MODEL
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{
            "role": "user",
            "parts": [
                {"text": "Extracted sheet data:\n" + json.dumps(_payload(sheet, rule_issues), indent=1)},
                {"inline_data": {"mime_type": "image/png", "data": sheet.page_image_b64}},
                {"text": "Review this sheet and reply with the JSON described in your instructions."},
            ],
        }],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "maxOutputTokens": 4096,
        },
    }

    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.post(
            API_URL.format(model=model),
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json=body,
        )
    if r.status_code != 200:
        raise AIUnavailable(f"Gemini returned {r.status_code}: {r.text[:300]}")

    data = r.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError):
        raise AIUnavailable(f"unexpected Gemini response: {json.dumps(data)[:300]}")

    return _extract_json(text)


def merge(sheet: Sheet, rule_issues: list[dict[str, Any]],
          ai_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Fold Gemini's output into the issue list, resolving its references to boxes."""
    balloon_box = {b.id: b.bbox.as_list() for b in sheet.balloons}
    item_balloons: dict[str, list[list[float]]] = {}
    for b in sheet.balloons:
        if b.item:
            item_balloons.setdefault(b.item, []).append(b.bbox.as_list())
    row_box = {str(r.item): r.bbox.as_list() for r in sheet.bom_rows if r.bbox}

    by_id = {i["id"]: i for i in rule_issues}
    for exp in ai_result.get("explanations", []) or []:
        issue = by_id.get(str(exp.get("id")))
        if not issue:
            continue
        if exp.get("detail"):
            issue["detail"] = str(exp["detail"]).strip()
        if exp.get("recommendation"):
            issue["recommendation"] = str(exp["recommendation"]).strip()
        issue["source"] = "rule+ai"

    issues = list(rule_issues)
    for n, extra in enumerate(ai_result.get("additional_issues", []) or [], start=1):
        refs = extra.get("refers_to") or {}
        targets: list[dict[str, Any]] = []
        for bid in refs.get("balloon_ids") or []:
            if bid in balloon_box:
                targets.append({"kind": "balloon", "bbox": balloon_box[bid]})
        for it in refs.get("bom_items") or []:
            it = str(it)
            if it in row_box:
                targets.append({"kind": "bom_row", "bbox": row_box[it]})
            for bb in item_balloons.get(it, []):
                targets.append({"kind": "balloon", "bbox": bb})

        sev = extra.get("severity")
        issues.append({
            "id": f"A{n}",
            "code": str(extra.get("code") or "AI_FINDING")[:40],
            "severity": sev if sev in ("error", "warning", "info") else "warning",
            "title": str(extra.get("title") or "AI finding")[:140],
            "detail": str(extra.get("detail") or "").strip(),
            "item": str(extra["item"]) if extra.get("item") not in (None, "") else None,
            "targets": targets,
            "recommendation": str(extra.get("recommendation") or "").strip(),
            "source": "ai",
        })

    order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: (order[i["severity"]], i["source"] == "ai"))
    return issues
