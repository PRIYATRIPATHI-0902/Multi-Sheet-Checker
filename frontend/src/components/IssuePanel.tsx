import { useEffect, useRef } from "react";
import type { AnalysisResult, Issue, Severity } from "../types";

interface Props {
  result: AnalysisResult;
  issues: Issue[];
  filters: Set<Severity>;
  onToggleFilter: (s: Severity) => void;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  activeSheet?: number; // highlight issues on the current sheet
}

const SEVERITY_LABEL: Record<Severity, string> = {
  error: "Errors",
  warning: "Warnings",
  info: "Notes",
};

export default function IssuePanel({
  result,
  issues,
  filters,
  onToggleFilter,
  selectedId,
  onSelect,
  activeSheet,
}: Props) {
  const listRef = useRef<HTMLDivElement>(null);

  // keep the selected card visible when selection comes from a marker click
  useEffect(() => {
    if (!selectedId) return;
    listRef.current
      ?.querySelector(`[data-issue="${selectedId}"]`)
      ?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [selectedId]);

  const { summary, ai } = result;
  const clean = summary.error === 0 && summary.warning === 0;

  // Document-wide figures (across every sheet) with the master as fallback.
  const sheets = result.sheets ?? [result.sheet];
  const master = sheets[result.master_page_index] ?? result.sheet;
  const totalBalloons = sheets.reduce((n, s) => n + s.balloons.length, 0);
  const bomRowCount = master.bom_rows.length;
  const drawingNo = master.title_block?.drawing_no ?? "\u2014";

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
            <dd className="mono" style={{ fontSize: "1rem" }}>
              {totalBalloons}
            </dd>
          </div>
          <div className="stamp-cell">
            <dt>Parts list rows</dt>
            <dd className="mono" style={{ fontSize: "1rem" }}>
              {bomRowCount}
            </dd>
          </div>
          <div className="stamp-cell">
            <dt>{sheets.length > 1 ? "Sheets" : "Drawing no."}</dt>
            <dd className="mono" style={{ fontSize: sheets.length > 1 ? "1rem" : "0.85rem" }}>
              {sheets.length > 1 ? sheets.length : drawingNo}
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

      {ai.status === "error" || ai.status === "unavailable" ? (
        <p className="banner">{ai.message}</p>
      ) : null}

      {result.warnings.map((w) => (
        <p className="banner" key={w}>
          {w}
        </p>
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
            const issuePage = issue.targets[0]?.page;
            const onThisSheet =
              typeof activeSheet === "number" &&
              issue.targets.some((t) => t.page === activeSheet);
            return (
              <button
                key={issue.id}
                type="button"
                data-issue={issue.id}
                className={`issue sev-${issue.severity}${selected ? " selected" : ""}`}
                onClick={() => onSelect(selected ? null : issue.id)}
                aria-expanded={selected}
              >
                <span className="issue-head">
                  <span className="issue-index mono">{index + 1}</span>
                  <span className="issue-title">{issue.title}</span>
                </span>
                <span className="issue-meta">
                  <span className="tag">{issue.code}</span>
                  {issue.item && <span className="tag">Item {issue.item}</span>}
                  {(sheets.length > 1 && typeof issuePage === "number") && (
                    <span
                      className="tag"
                      style={onThisSheet ? { background: "#2563eb", color: "#fff" } : undefined}
                    >
                      Sheet {issuePage + 1}
                    </span>
                  )}
                  {issue.source !== "rule" && <span className="tag ai">AI</span>}
                </span>
                {selected && (
                  <span className="issue-detail">
                    <p>{issue.detail}</p>
                    {issue.recommendation && (
                      <span className="issue-fix">
                        <strong>What to do</strong>
                        {issue.recommendation}
                      </span>
                    )}
                  </span>
                )}
              </button>
            );
          })
        )}
      </div>
    </aside>
  );
}
