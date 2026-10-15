export type ContextScope = "range" | "sheet" | "workbook";

// Mirrors api/app/domain/contracts.py limits.
export const MAX_CONTEXT_CELLS = 10_000;
export const MAX_MAP_SHEETS = 100;
export const MAX_SHEET_TABLES = 20;
export const MAX_SELECTED_ARTIFACTS = 5;
export const MAX_WORKBOOK_CELLS = 20_000;
export const MAX_QUESTION_CHARS = 4_000;

export interface TabularArtifact {
  sheetName: string;
  address: string;
  values: unknown[][];
  formulas: unknown[][];
  numberFormats: unknown[][];
}

export interface SheetTable {
  name: string;
  address: string;
}

/** Structural metadata only: never cell values or formulas. */
export interface WorkbookSheetMap {
  sheetName: string;
  index: number;
  visibility: "Visible" | "Hidden" | "VeryHidden";
  usedRangeAddress: string | null;
  rowCount: number;
  columnCount: number;
  tables: SheetTable[];
}

export interface WorkbookMap {
  sheets: WorkbookSheetMap[];
}

export interface RangeSelection {
  sheetName: string;
  address: string;
  reason: string;
}

export interface ContextSnapshot {
  scope: ContextScope;
  artifact?: TabularArtifact;
  workbookMap?: WorkbookMap;
}

export interface ContextDescriptor {
  scope: ContextScope;
  sheetName?: string;
  address?: string;
  detail: string;
}

export class ContextError extends Error {
  constructor(
    public readonly code: "CONTEXT_UNAVAILABLE" | "EMPTY_CONTEXT" | "CONTEXT_TOO_LARGE" | "WORKBOOK_TOO_LARGE",
    message: string,
    public readonly nextAction: string,
  ) {
    super(message);
  }
}
