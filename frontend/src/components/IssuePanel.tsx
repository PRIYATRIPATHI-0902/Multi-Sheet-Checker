import { useEffect, useRef } from "react";
import type { AnalysisResult, Issue, QtyReading, Severity } from "../types";
import { sheetAt, sheetsOf } from "../types";

interface Props {
  result: AnalysisResult;
  issues: Issue[];
  filters: Set<Severity>;
  onToggleFilter: (s: Severity) => void;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  activeSheet?: number;
  onSheetChange?: (pageIndex: number) => void;
}

const SEVERITY_LABEL: Record<Severity, string> = {
  error: "Errors",
  warning: "Warnings",
  info: "Notes",
};

/** The arithmetic behind a quantity verdict, so the call can be checked. */
function Readings({ readings, bomQty, matched }: {
  readings: QtyReading[];
  bomQty?: number;
  matched: string[];
}) {
  return (
    <span style={{ display: "block", marginTop: "0.6rem" }}>
      <strong style={{ display: "block", marginBottom: "0.25rem" }}>
        How the callouts add up
      </strong>
      <span style={{ display: "block" }}>
        {readings.map((r) => {
          const hit = matched.includes(r.key);
          return (
            <span
              key={r.key}
              style={{
                display: "flex",
                gap: 8,
                alignItems: "baseline",
                padding: "2px 0",
                fontSize: "0.78rem",
                color: hit ? "#166534" : "inherit",
                fontWeight: hit ? 600 : 400,
              }}
            >
              <span className="mono" style={{ minWidth: "2.2rem" }}>{r.value}</span>
              <span>
                {r.label}
                {r.breakdown !== String(r.value) && ` — ${r.breakdown}`}
                {hit && " ✓ matches the parts list"}
              </span>
            </span>
          );
        })}
        {typeof bomQty === "number" && (
          <span
            style={{
              display: "flex",
              gap: 8,
              alignItems: "baseline",
              padding: "4px 0 0",
              fontSize: "0.78rem",
              borderTop: "1px solid rgba(0,0,0,0.08)",
              marginTop: 4,
            }}
          >
            <span className="mono" style={{ minWidth: "2.2rem" }}>{bomQty}</span>
            <span>parts list</span>
          </span>
        )}
      </span>
    </span>
  );
}

export default function IssuePanel({
  result,
  issues,
  filters,
  onToggleFilter,
  selectedId,
  onSelect,
  activeSheet,
  onSheetChange,
}: Props) {
  const listRef = useRef<HTMLDivElement>(null);

  // Keep the selected card visible when the selection comes from a marker click.
  useEffect(() => {
    if (!selectedId) return;
    listRef.current
      ?.querySelector(`[data-issue="${selectedId}"]`)
      ?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [selectedId]);

  const { summary, ai } = result;
  const clean = summary.error === 0 && summary.warning === 0;

  const sheets = sheetsOf(result);
  const master = sheetAt(result, result.master_page_index);
  const totalBalloons = sheets.reduce((n, s) => n + s.balloons.length, 0);
  const drawingNo = master.title_block?.drawing_no ?? "\u2014";
  const multi = (result.assemblies?.length ?? 0) > 1;
  const bomRowCount = multi
    ? result.assemblies.reduce((n, a) => n + a.bom_rows, 0)
    : master.bom_rows.filter((r) => r.item !== null).length;

  return (
    <aside className="sidebar">
      <div className="stamp">
        <div className="stamp-row">
          <div className="stamp-cell err">
            <dt>Errors</dt>
            <dd className="mono">{String(summary.error).padStart(2, "0")}</dd>
          </div>
          <div className="stamp-cell warn">
            <dt>Warnings</dt>
            <dd className="mono">{String(summary.warning).padStart(2, "0")}</dd>
          </div>
          <div className="stamp-cell info">
            <dt>Notes</dt>
            <dd className="mono">{String(summary.info).padStart(2, "0")}</dd>
          </div>
        </div>
        <div className="stamp-row">
          <div className="stamp-cell">
            <dt>Balloons</dt>
            <dd className="mono" style={{ fontSize: "1rem" }}>{totalBalloons}</dd>
          </div>
          <div className="stamp-cell">
            <dt>Parts list rows</dt>
            <dd className="mono" style={{ fontSize: "1rem" }}>{bomRowCount}</dd>
          </div>
          <div className="stamp-cell">
            <dt>{multi ? "Drawings" : "Drawing no."}</dt>
            <dd className="mono" style={{ fontSize: multi ? "1rem" : "0.85rem" }}>
              {multi ? `${result.assemblies.length} / ${sheets.length} sh` : drawingNo}
            </dd>
          </div>
        </div>
        <div className="stamp-verdict">
          <span className={`verdict-text ${clean ? "pass" : "fail"}`}>
            {clean ? "No blocking issues" : "Not ready to release"}
          </span>
          <span
            className={`ai-chip ${ai.status === "ok" ? "ok" : ai.status === "skipped" ? "" : "bad"}`}
            title={ai.message}
          >
            {ai.status === "ok"
              ? "AI review on"
              : ai.status === "skipped"
                ? "Rules only"
                : "AI unavailable"}
          </span>
        </div>
      </div>

      {result.split_balloons && (
        <p className="banner">
          Split balloons: the lower figure is read as the quantity at that place.
          {result.split_inferred && " Assumed from the callouts — no note on the drawing says so."}
        </p>
      )}

      {ai.status === "error" || ai.status === "unavailable" ? (
        <p className="banner">{ai.message}</p>
      ) : null}

      {result.warnings.map((w) => (
        <p className="banner" key={w}>{w}</p>
      ))}

      <div className="filters">
        {(Object.keys(SEVERITY_LABEL) as Severity[]).map((s) => (
          <button
            key={s}
            type="button"
            className="chip"
            aria-pressed={filters.has(s)}
            onClick={() => onToggleFilter(s)}
          >
            {SEVERITY_LABEL[s]} {result.issues.filter((i) => i.severity === s).length}
          </button>
        ))}
      </div>

      <div className="issues" ref={listRef}>
        {issues.length === 0 ? (
          <p className="empty-issues">
            {result.issues.length === 0
              ? "Balloons and the parts list agree on every item."
              : "No issues match the current filters."}
          </p>
        ) : (
          issues.map((issue, index) => {
            const selected = issue.id === selectedId;
            const pages = issue.pages?.length
              ? issue.pages
              : Array.from(new Set(issue.targets.map((t) => t.page)));
            const readings = issue.evidence?.readings ?? [];
            return (
              <div
                key={issue.id}
                data-issue={issue.id}
                className={`issue sev-${issue.severity}${selected ? " selected" : ""}`}
                role="button"
                tabIndex={0}
                aria-expanded={selected}
                onClick={() => onSelect(selected ? null : issue.id)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onSelect(selected ? null : issue.id);
                  }
                }}
              >
                <span className="issue-head">
                  <span className="issue-index mono">{index + 1}</span>
                  <span className="issue-title">{issue.title}</span>
                </span>
                <span className="issue-meta">
                  {issue.assembly && <span className="tag">{issue.assembly}</span>}
                  <span className="tag">{issue.code}</span>
                  {issue.item && <span className="tag">Item {issue.item}</span>}
                  {sheets.length > 1 &&
                    pages.map((p) => (
                      <span
                        key={p}
                        className="tag"
                        style={p === activeSheet ? { background: "#2563eb", color: "#fff" } : undefined}
                        onClick={(e) => {
                          e.stopPropagation();
                          onSheetChange?.(p);
                        }}
                      >
                        Sheet {p + 1}
                      </span>
                    ))}
                  {issue.source !== "rule" && <span className="tag ai">AI</span>}
                </span>
                {selected && (
                  <span className="issue-detail">
                    <p>{issue.detail}</p>
                    {readings.length > 0 && (
                      <Readings
                        readings={readings}
                        bomQty={issue.evidence?.bom_qty}
                        matched={issue.evidence?.matched ?? []}
                      />
                    )}
                    {issue.recommendation && (
                      <span className="issue-fix">
                        <strong>What to do</strong>
                        {issue.recommendation}
                      </span>
                    )}
                  </span>
                )}
              </div>
            );
          })
        )}
      </div>
    </aside>
  );
}
