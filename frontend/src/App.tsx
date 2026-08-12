import { useMemo, useState } from "react";
import { analyze } from "./api";
import Dropzone from "./components/Dropzone";
import IssuePanel from "./components/IssuePanel";
import SheetView from "./components/SheetView";
import type { AnalysisResult, Severity } from "./types";

const ALL: Severity[] = ["error", "warning", "info"];

export default function App() {
  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<AnalysisResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [useAi, setUseAi] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [filters, setFilters] = useState<Set<Severity>>(new Set(ALL));

  const run = async (f: File, ai: boolean) => {
    setBusy(true);
    setError(null);
    setSelectedId(null);
    try {
      setResult(await analyze(f, { page: 0, useAi: ai }));
      setFile(f);
    } catch (e) {
      setError(e instanceof Error ? e.message : "The check could not be completed.");
      setResult(null);
    } finally {
      setBusy(false);
    }
  };

  const visible = useMemo(
    () => (result ? result.issues.filter((i) => filters.has(i.severity)) : []),
    [result, filters]
  );

  const toggleFilter = (s: Severity) => {
    setFilters((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s);
      else next.add(s);
      return next;
    });
    setSelectedId(null);
  };

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">Sheet Check</span>
          <span className="brand-sub">balloon &amp; parts list</span>
        </div>
        {file && <span className="topbar-file">{file.name}</span>}
        <span className="topbar-spacer" />

        <div className="topbar-actions">
          <label className="toggle">
            <input
              type="checkbox"
              checked={useAi}
              onChange={(e) => {
                setUseAi(e.target.checked);
                if (file) void run(file, e.target.checked);
              }}
            />
            AI review
          </label>
          {result && (
            <>
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() => file && void run(file, useAi)}
                disabled={busy}
              >
                Re-check
              </button>
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() => {
                  setResult(null);
                  setFile(null);
                  setSelectedId(null);
                }}
              >
                New sheet
              </button>
            </>
          )}
        </div>
      </header>

      <div className="workspace">
        {result ? (
          <>
            <SheetView
              result={result}
              issues={visible}
              selectedId={selectedId}
              onSelect={setSelectedId}
            />
            <IssuePanel
              result={result}
              issues={visible}
              filters={filters}
              onToggleFilter={toggleFilter}
              selectedId={selectedId}
              onSelect={setSelectedId}
            />
          </>
        ) : (
          <Dropzone onFile={(f) => void run(f, useAi)} error={error} />
        )}

        {busy && (
          <div className="overlay">
            <div className="progress">
              <span className="label">
                {useAi ? "Reading the sheet, then asking Gemini" : "Reading the sheet"}
              </span>
              <span className="bar">
                <i />
              </span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
