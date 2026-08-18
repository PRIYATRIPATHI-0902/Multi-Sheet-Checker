import type { AnalysisResult } from "./types";

/**
 * In development the UI is served by esbuild on :3000 and the API runs on :8000.
 * In production FastAPI serves the built bundle itself, so a relative path works.
 */
export const API_BASE =
  window.location.port === "3000" ? "http://localhost:8000" : "";

/**
 * Analyze a drawing. The whole PDF (every sheet) is now read and reconciled
 * server-side, so no page index is required. The `opts` argument is optional
 * and ignored, kept only so existing callers keep compiling.
 */
export async function analyze(
  file: File,
  _opts?: { page?: number }
): Promise<AnalysisResult> {
  const form = new FormData();
  form.append("file", file);

  const res = await fetch(`${API_BASE}/api/analyze`, { method: "POST", body: form });
  if (!res.ok) {
    let message = `The server returned ${res.status}.`;
    try {
      const body = await res.json();
      if (body?.detail) message = String(body.detail);
    } catch {
      /* keep the status message */
    }
    throw new Error(message);
  }
  return res.json();
}
