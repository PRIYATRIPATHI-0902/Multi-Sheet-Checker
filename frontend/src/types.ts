export type Severity = "error" | "warning" | "info";

/** [x0, y0, x1, y1] in PDF points, origin at the top-left of the page. */
export type Bbox = [number, number, number, number];

export interface Target {
  kind: "balloon" | "bom_row" | "region";
  bbox: Bbox;
}

export interface Issue {
  id: string;
  code: string;
  severity: Severity;
  title: string;
  detail: string;
  item: string | null;
  targets: Target[];
  recommendation: string;
  source: "rule" | "rule+ai" | "ai";
}

export interface BalloonData {
  id: string;
  item: string | null;
  qty: number | null;
  raw_text: string;
  bbox: Bbox;
  nearby_text: string;
}

export interface BomRowData {
  item: string | null;
  part_number: string;
  description: string;
  material: string;
  qty: number | null;
  qty_raw: string;
  bbox: Bbox | null;
}

export interface SheetData {
  page_width: number;
  page_height: number;
  balloons: BalloonData[];
  bom_rows: BomRowData[];
  stated_total: number | null;
  stated_total_bbox: Bbox | null;
  notes: string[];
  title_block: Record<string, string>;
}

export interface AnalysisResult {
  filename: string;
  page: number;
  sheet: SheetData;
  page_image: string;
  issues: Issue[];
  summary: { total: number; error: number; warning: number; info: number };
  ai: { status: "ok" | "skipped" | "unavailable" | "error"; message: string };
  warnings: string[];
}
