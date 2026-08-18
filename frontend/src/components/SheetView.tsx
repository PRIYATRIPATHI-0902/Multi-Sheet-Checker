import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { AnalysisResult, Issue } from "../types";

interface Props {
  result: AnalysisResult;
  issues: Issue[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  activeSheet: number;
  onSheetChange: (i: number) => void;
}

const PAD = 3; // points of breathing room drawn around each marker
const GUTTER = 24; // .canvas-scroll padding, in px

export default function SheetView({
  result,
  issues,
  selectedId,
  onSelect,
  activeSheet,
  onSheetChange,
}: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [fitZoom, setFitZoom] = useState(1);
  /** null means "follow the fit zoom"; a number is an explicit user choice. */
  const [userZoom, setUserZoom] = useState<number | null>(null);
  const [dragging, setDragging] = useState(false);

  // Support both the new multi-sheet shape and the old single-sheet payload.
  const sheets = result.sheets ?? [result.sheet];
  const sheet = sheets[activeSheet] ?? sheets[0];
  const pw = sheet.page_width;
  const ph = sheet.page_height;
  const zoom = userZoom ?? fitZoom;

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const fit = () => setFitZoom(Math.max(0.05, (el.clientWidth - GUTTER * 2) / pw));
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, [pw]);

  // a newly loaded / switched sheet always starts fitted
  useEffect(() => setUserZoom(null), [sheet.page_image]);

  // number of visible issues that place a marker on each sheet (for the tabs)
  const perSheet = sheets.map(
    (s) => issues.filter((i) => i.targets.some((t) => t.page === s.page_index)).length
  );

  // bring the selected marker into view
  useEffect(() => {
    const scroller = scrollRef.current;
    if (!selectedId || !scroller) return;
    const issue = issues.find((i) => i.id === selectedId);
    const target =
      issue?.targets.find((t) => t.page === activeSheet) ?? issue?.targets[0];
    if (!target) return;
    const [x0, y0, x1, y1] = target.bbox;
    scroller.scrollTo({
      left: ((x0 + x1) / 2) * zoom + GUTTER - scroller.clientWidth / 2,
      top: ((y0 + y1) / 2) * zoom + GUTTER - scroller.clientHeight / 2,
      behavior: "smooth",
    });
  }, [selectedId, issues, zoom, activeSheet]);

  // drag anywhere on the sheet to pan
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    let startX = 0, startY = 0, left = 0, top = 0, active = false;
    const down = (e: MouseEvent) => {
      if ((e.target as HTMLElement).closest(".marker")) return;
      active = true;
      setDragging(true);
      startX = e.clientX;
      startY = e.clientY;
      left = el.scrollLeft;
      top = el.scrollTop;
    };
    const move = (e: MouseEvent) => {
      if (!active) return;
      el.scrollLeft = left - (e.clientX - startX);
      el.scrollTop = top - (e.clientY - startY);
    };
    const up = () => {
      active = false;
      setDragging(false);
    };
    el.addEventListener("mousedown", down);
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => {
      el.removeEventListener("mousedown", down);
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
  }, []);

  const step = (factor: number) =>
    setUserZoom(Math.min(6, Math.max(0.15, zoom * factor)));

  return (
    <div className="canvas">
      {sheets.length > 1 && (
        <div
          style={{
            display: "flex",
            gap: 6,
            padding: "8px 12px",
            flexWrap: "wrap",
            borderBottom: "1px solid rgba(0,0,0,0.08)",
            background: "rgba(0,0,0,0.02)",
          }}
        >
          {sheets.map((s, i) => {
            const on = i === activeSheet;
            return (
              <button
                key={s.page_index}
                type="button"
                onClick={() => onSheetChange(i)}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                  padding: "4px 10px",
                  borderRadius: 6,
                  cursor: "pointer",
                  fontSize: "0.8rem",
                  fontWeight: 600,
                  border: on ? "1px solid #2563eb" : "1px solid rgba(0,0,0,0.15)",
                  background: on ? "#2563eb" : "#fff",
                  color: on ? "#fff" : "#333",
                }}
              >
                Sheet {s.page_index + 1}
                {s.page_index === result.master_page_index && (
                  <span
                    title="Authoritative parts list"
                    style={{
                      fontSize: "0.6rem",
                      background: on ? "rgba(255,255,255,0.25)" : "#e0ecff",
                      color: on ? "#fff" : "#2563eb",
                      padding: "1px 5px",
                      borderRadius: 4,
                    }}
                  >
                    BOM
                  </span>
                )}
                {perSheet[i] > 0 && (
                  <span
                    style={{
                      fontSize: "0.65rem",
                      background: on ? "rgba(255,255,255,0.25)" : "#fde2e2",
                      color: on ? "#fff" : "#c0392b",
                      padding: "1px 6px",
                      borderRadius: 10,
                    }}
                  >
                    {perSheet[i]}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      )}

      <div
        className={`canvas-scroll${dragging ? " dragging" : ""}`}
        ref={scrollRef}
        onWheel={(e) => {
          if (!e.ctrlKey && !e.metaKey) return;
          e.preventDefault();
          step(e.deltaY < 0 ? 1.12 : 0.89);
        }}
      >
        <div className="sheet" style={{ width: pw * zoom, height: ph * zoom }}>
          <img
            src={sheet.page_image}
            alt={`Sheet ${sheet.page_index + 1} of ${result.filename}`}
            draggable={false}
          />
          {issues.flatMap((issue, index) =>
            issue.targets
              .filter((target) => target.page === activeSheet) // only this sheet
              .map((target, t) => {
                const [x0, y0, x1, y1] = target.bbox;
                const selected = issue.id === selectedId;
                return (
                  <button
                    key={`${issue.id}-${t}`}
                    type="button"
                    className={[
                      "marker",
                      target.kind === "balloon" ? "balloon" : "",
                      `sev-${issue.severity}`,
                      selected ? "selected" : "",
                      selectedId && !selected ? "dimmed" : "",
                    ]
                      .filter(Boolean)
                      .join(" ")}
                    style={{
                      left: `${((x0 - PAD) / pw) * 100}%`,
                      top: `${((y0 - PAD) / ph) * 100}%`,
                      width: `${((x1 - x0 + PAD * 2) / pw) * 100}%`,
                      height: `${((y1 - y0 + PAD * 2) / ph) * 100}%`,
                    }}
                    title={issue.title}
                    aria-label={`Issue ${index + 1}. ${issue.title}`}
                    onClick={(e) => {
                      e.stopPropagation();
                      onSelect(selected ? null : issue.id);
                    }}
                  >
                    {t === 0 && <span className="marker-tag">{index + 1}</span>}
                  </button>
                );
              })
          )}
        </div>
      </div>

      <div className="zoombar">
        <button type="button" onClick={() => step(0.8)} aria-label="Zoom out">
          &minus;
        </button>
        <span className="readout">{Math.round(zoom * 100)}%</span>
        <button type="button" onClick={() => step(1.25)} aria-label="Zoom in">
          +
        </button>
        <button type="button" onClick={() => setUserZoom(null)}>
          Fit
        </button>
        <button type="button" onClick={() => setUserZoom(1)}>
          1:1
        </button>
      </div>
    </div>
  );
}
