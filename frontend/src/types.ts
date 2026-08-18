// Shared types for the Assembly BOM / Balloon Checker frontend.
// Describes a whole multi-sheet document; the old single-sheet fields
// (`sheet`, `page_image`, `page`) are still sent for backwards compatibility.

export type Severity = "error" | "warning" | "info";

export interface Target {
  kind: string;          // "balloon" | "bom_row"
  bbox: number[];        // [x0, y0, x1, y1] in PDF points
  page: number;          // which sheet (0-based) this marker belongs to
}

/** One defensible way of totalling an item's callouts. */
export interface QtyReading {
  key: string;           // "total_of_placements" | "per_sheet" | "per_view" | ...
  label: string;         // plain-language description of the reading
  value: number;         // the total it produces
  breakdown: string;     // "2 + 8"
}

export interface IssueEvidence {
  bom_qty?: number;
  readings?: QtyReading[];
  matched?: string[];    // reading keys that equal the parts-list quantity
  balloons?: {
    id: string;
    page: number;
    view: string;
    qty: number | null;
    text: string;
  }[];
  with_qty?: number;
  without_qty?: number;
}

export interface Issue {
  id: string;
  code: string;
  severity: Severity;
  title: string;
  detail: string;
  item: string | null;
  targets: Target[];
  pages: number[];       // every sheet this issue touches
  recommendation: string;
  /** Which drawing in the PDF this belongs to; empty when there is only one. */
  assembly: string;
  evidence: IssueEvidence;
  source: string;        // "rule"
}

export interface Balloon {
  id: string;
  item: string | null;
  qty: number | null;
  qty_raw: string;
  raw_text: string;
  bbox: number[];
  page_index: number;
  nearby_text: string;
  is_ref: boolean;
  view: string;          // "MAIN" | "VIEW A-A" | "DETAIL B" | ...
  source: "outline" | "text";
  confidence: "high" | "low";
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

export interface ViewLabel {
  label: string;
  bbox: number[];
}

export interface SheetData {
  page_index: number;
  page_width: number;
  page_height: number;
  page_image: string;    // data:image/png;base64,...
  balloons: Balloon[];
  bom_rows: BomRow[];
  title_block: TitleBlock;
  views: ViewLabel[];
  notes: string[];
  stated_total: number | null;
  stated_total_bbox: number[] | null;
  bom_detected: boolean;
  bom_is_extract: boolean;
  split_balloons: boolean;
}

export interface Summary {
  total: number;
  error: number;
  warning: number;
  info: number;
  by_code: Record<string, number>;
}

export interface AiInfo {
  status: string;        // "ok" | "skipped" | "error" | "unavailable"
  message: string;
}

/** One independent drawing inside the PDF. */
export interface AssemblyInfo {
  label: string;          // drawing number, or "Sheet n"
  pages: number[];
  master_page_index: number;
  bom_rows: number;
  balloons: number;
}

export interface AnalysisResult {
  filename: string;
  master_page_index: number;
  summary: Summary;
  ai: AiInfo;
  warnings: string[];
  issues: Issue[];
  sheets: SheetData[];
  assemblies: AssemblyInfo[];
  split_balloons: boolean;
  split_inferred: boolean;

  // ---- backwards-compatible single-sheet fields (the master sheet) ----
  sheet: SheetData;
  page: number;
  page_image: string;
}

/** Sheets in a shape that is always safe to iterate. */
export function sheetsOf(result: AnalysisResult): SheetData[] {
  return result.sheets?.length ? result.sheets : [result.sheet];
}

/** Look a sheet up by its page index, never by array position. */
export function sheetAt(result: AnalysisResult, pageIndex: number): SheetData {
  const sheets = sheetsOf(result);
  return sheets.find((s) => s.page_index === pageIndex) ?? sheets[0];
}
