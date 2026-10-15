import {
  ContextDescriptor,
  ContextError,
  ContextScope,
  ContextSnapshot,
  MAX_CONTEXT_CELLS,
  MAX_MAP_SHEETS,
  MAX_SELECTED_ARTIFACTS,
  MAX_SHEET_TABLES,
  MAX_WORKBOOK_CELLS,
  RangeSelection,
  TabularArtifact,
  WorkbookMap,
  WorkbookSheetMap,
} from "./contracts";

export { MAX_CONTEXT_CELLS } from "./contracts";

interface ExcelRange {
  address: string;
  rowCount: number;
  columnCount: number;
  values: unknown[][];
  formulas: unknown[][];
  numberFormat: unknown[][];
  isNullObject?: boolean;
  load(properties: string): void;
}

interface ExcelTable {
  name: string;
  getRange(): ExcelRange;
}

interface ExcelWorksheet {
  name: string;
  position: number;
  visibility: WorkbookSheetMap["visibility"];
  tables: { items: ExcelTable[]; load(properties: string): void };
  getUsedRangeOrNullObject(): ExcelRange;
  getRange(address: string): ExcelRange;
  load(properties: string): void;
}

interface ExcelWorksheets {
  items: ExcelWorksheet[];
  load(properties: string): void;
}

interface ExcelRequestContext {
  workbook: {
    getSelectedRange(): ExcelRange;
    worksheets: ExcelWorksheets & { getActiveWorksheet(): ExcelWorksheet; getItem(name: string): ExcelWorksheet };
  };
  sync(): Promise<void>;
}

export interface ExcelRuntime {
  run<T>(callback: (context: ExcelRequestContext) => Promise<T>): Promise<T>;
}

declare global {
  interface Window { Excel?: ExcelRuntime; }
}

const NARROW_ACTION = "Сузьте вопрос или выберите контекст «Лист» или «Выделение».";

function getExcel(excel: ExcelRuntime | undefined = window.Excel): ExcelRuntime {
  if (!excel) {
    throw new ContextError("CONTEXT_UNAVAILABLE", "Не удалось получить данные из Excel.", "Откройте книгу в Excel Desktop и повторите попытку.");
  }
  return excel;
}

function ensureBounded(range: Pick<ExcelRange, "rowCount" | "columnCount">): void {
  const cellCount = range.rowCount * range.columnCount;
  if (!Number.isFinite(cellCount) || cellCount <= 0) {
    throw new ContextError("EMPTY_CONTEXT", "В выбранном контексте нет данных.", "Выберите диапазон с данными.");
  }
  if (cellCount > MAX_CONTEXT_CELLS) {
    throw new ContextError("CONTEXT_TOO_LARGE", `Контекст содержит ${cellCount} ячеек, что превышает лимит ${MAX_CONTEXT_CELLS}.`, "Сузьте диапазон или выберите меньший лист.");
  }
}

/** Story 3.2: a context whose cells are all blank (no value and no formula) is not analysable. */
export function hasAnalyzableData(artifact: TabularArtifact): boolean {
  const filled = (value: unknown) => value !== null && value !== undefined && String(value).trim() !== "";
  return artifact.values.some((row, rowIndex) => row.some((value, columnIndex) => filled(value) || filled(artifact.formulas[rowIndex]?.[columnIndex])));
}

function artifactFrom(range: ExcelRange, sheetName: string): TabularArtifact {
  return {
    sheetName,
    address: range.address,
    values: range.values,
    formulas: range.formulas,
    numberFormats: range.numberFormat,
  };
}

async function readRange(range: ExcelRange, sheetName: string, context: ExcelRequestContext): Promise<TabularArtifact> {
  // Metadata is loaded before cell payload so oversized contexts never load values.
  range.load("address,rowCount,columnCount");
  await context.sync();
  ensureBounded(range);
  range.load("values,formulas,numberFormat,address");
  await context.sync();
  return artifactFrom(range, sheetName);
}

async function buildWorkbookMap(context: ExcelRequestContext): Promise<WorkbookMap> {
  const worksheets = context.workbook.worksheets;
  worksheets.load("items/name,items/position,items/visibility");
  await context.sync();
  if (!worksheets.items.length) {
    throw new ContextError("EMPTY_CONTEXT", "В книге нет доступных листов.", "Откройте книгу с данными.");
  }
  if (worksheets.items.length > MAX_MAP_SHEETS) {
    throw new ContextError("WORKBOOK_TOO_LARGE", `Книга содержит ${worksheets.items.length} листов, а безопасный лимит карты — ${MAX_MAP_SHEETS}.`, NARROW_ACTION);
  }
  // Only addresses, dimensions and table names are loaded: no values, formulas or formats.
  const ranges = worksheets.items.map((sheet) => sheet.getUsedRangeOrNullObject());
  ranges.forEach((range) => range.load("isNullObject,address,rowCount,columnCount"));
  worksheets.items.forEach((sheet) => sheet.tables.load("items/name"));
  await context.sync();
  const tableRanges = worksheets.items.map((sheet) => sheet.tables.items.slice(0, MAX_SHEET_TABLES).map((table) => {
    const range = table.getRange();
    range.load("address");
    return { name: table.name, range };
  }));
  await context.sync();
  return {
    sheets: worksheets.items.map((sheet, index) => {
      const range = ranges[index];
      return {
        sheetName: sheet.name,
        index: sheet.position,
        visibility: sheet.visibility,
        usedRangeAddress: range.isNullObject ? null : range.address,
        rowCount: range.isNullObject ? 0 : range.rowCount,
        columnCount: range.isNullObject ? 0 : range.columnCount,
        tables: tableRanges[index].map((table) => ({ name: table.name, address: table.range.address })),
      };
    }),
  };
}

export async function extractContext(scope: ContextScope, excel?: ExcelRuntime): Promise<ContextSnapshot> {
  const runtime = getExcel(excel);
  return runtime.run(async (context) => {
    if (scope === "range") {
      const range = context.workbook.getSelectedRange();
      const sheet = context.workbook.worksheets.getActiveWorksheet();
      sheet.load("name");
      const artifact = await readRange(range, sheet.name, context);
      return { scope, artifact };
    }

    if (scope === "sheet") {
      const sheet = context.workbook.worksheets.getActiveWorksheet();
      sheet.load("name");
      const range = sheet.getUsedRangeOrNullObject();
      range.load("isNullObject");
      await context.sync();
      if (range.isNullObject) {
        throw new ContextError("EMPTY_CONTEXT", "На активном листе нет используемого диапазона.", "Выберите лист с данными или выделите диапазон.");
      }
      const artifact = await readRange(range, sheet.name, context);
      return { scope, artifact };
    }

    return { scope, workbookMap: await buildWorkbookMap(context) };
  });
}

/**
 * Phase 2 of /workbook: reads only the API-planned ranges. Sizes are checked
 * before any cell payload is loaded, per range and in total.
 */
export async function readSelectedRanges(selections: RangeSelection[], excel?: ExcelRuntime): Promise<TabularArtifact[]> {
  if (!selections.length || selections.length > MAX_SELECTED_ARTIFACTS) {
    throw new ContextError("CONTEXT_TOO_LARGE", `Для ответа выбрано ${selections.length} диапазонов, допустимо от 1 до ${MAX_SELECTED_ARTIFACTS}.`, NARROW_ACTION);
  }
  const runtime = getExcel(excel);
  return runtime.run(async (context) => {
    const ranges = selections.map((selection) => {
      const range = context.workbook.worksheets.getItem(selection.sheetName).getRange(selection.address);
      range.load("address,rowCount,columnCount");
      return range;
    });
    await context.sync();
    ranges.forEach(ensureBounded);
    const total = ranges.reduce((sum, range) => sum + range.rowCount * range.columnCount, 0);
    if (total > MAX_WORKBOOK_CELLS) {
      throw new ContextError("CONTEXT_TOO_LARGE", `Выбранные диапазоны содержат ${total} ячеек, а безопасный лимит — ${MAX_WORKBOOK_CELLS}.`, NARROW_ACTION);
    }
    ranges.forEach((range) => range.load("values,formulas,numberFormat,address"));
    await context.sync();
    return ranges.map((range, index) => artifactFrom(range, selections[index].sheetName));
  });
}

export async function describeContext(scope: ContextScope, excel?: ExcelRuntime): Promise<ContextDescriptor> {
  const runtime = getExcel(excel);
  if (scope === "workbook") {
    const snapshot = await extractContext(scope, runtime);
    const sheets = snapshot.workbookMap?.sheets ?? [];
    const withData = sheets.filter((sheet) => sheet.usedRangeAddress).length;
    return { scope, detail: `${sheets.length} лист(ов), с данными: ${withData}` };
  }
  return runtime.run(async (context) => {
    const sheet = context.workbook.worksheets.getActiveWorksheet();
    sheet.load("name");
    const range = scope === "range" ? context.workbook.getSelectedRange() : sheet.getUsedRangeOrNullObject();
    range.load("isNullObject,address,rowCount,columnCount");
    await context.sync();
    if (range.isNullObject) {
      throw new ContextError("EMPTY_CONTEXT", "В выбранном контексте нет данных.", "Выберите диапазон с данными.");
    }
    ensureBounded(range);
    return { scope, sheetName: sheet.name, address: range.address, detail: `${sheet.name} · ${range.address}` };
  });
}
