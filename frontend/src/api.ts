import type { AnalysisResult } from "./types";

/**
 * In development the UI is served by esbuild on :3000 and the API runs on :8000.
 * In production FastAPI serves the built bundle itself, so a relative path works.
 */
export const API_BASE =
  window.location.port === "3000" ? "http://localhost:8000" : "";

export async function analyze(
  file: File,
  opts: { page: number; useAi: boolean }
): Promise<AnalysisResult> {
  const form = new FormData();
  form.append("file", file);
  form.append("page", String(opts.page));
  form.append("use_ai", String(opts.useAi));

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
