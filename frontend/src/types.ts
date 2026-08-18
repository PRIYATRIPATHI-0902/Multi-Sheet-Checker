// Shared types for the Assembly BOM / Balloon Checker frontend.
// Updated to describe a whole multi-sheet document while keeping the old
// single-sheet fields (`sheet`, `page_image`, `page`) for backwards compat.

export type Severity = "error" | "warning" | "info";

export interface Target {
  kind: string;          // "balloon" | "bom_row" | ...
  bbox: number[];        // [x0, y0, x1, y1] in PDF points
  page: number;          // NEW: which sheet (0-based) this marker belongs to
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
  source: string;        // "rule" | ...
}

export interface Balloon {
  id: string;
  item: string | null;
  qty: number | null;
  raw_text: string;
  bbox: number[];
  page_index: number;
  nearby_text: string;
}

export interface BomRow {
  item: string | null;
  part_number: string;
  description: string;
  material: string;
  qty: number | null;
  qty_raw: string;
  page_index: number;
  bbox: number[] | null;
  is_ar: boolean;
}

export interface TitleBlock {
  drawing_no?: string;
  revision?: string;
  scale?: string;
  sheet?: string;
}

export interface SheetData {
  page_index: number;
  page_width: number;
  page_height: number;
  page_image: string;    // data:image/png;base64,...
  balloons: Balloon[];
  bom_rows: BomRow[];
  title_block: TitleBlock;
  notes: string[];
  stated_total: number | null;
  bom_detected: boolean;
  bom_is_extract: boolean;
  split_balloons: boolean;
}

export interface Summary {
  total: number;
  error: number;
  warning: number;
  info: number;
}

export interface AiInfo {
  status: string;        // "ok" | "skipped" | "error" | "unavailable"
  message: string;
}

export interface AnalysisResult {
  filename: string;
  master_page_index: number;
  summary: Summary;
  ai: AiInfo;
  warnings: string[];
  issues: Issue[];
  sheets: SheetData[];

  // ---- backwards-compatible single-sheet fields (the master sheet) ----
  sheet: SheetData;
  page: number;
  page_image: string;
}
