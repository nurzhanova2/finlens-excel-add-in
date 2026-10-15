import { ExcelRuntime } from "../taskpane/excel-context";

export interface FakeSheet {
  name: string;
  /** Used range in local A1 notation, e.g. "A1:C3"; null for an empty sheet. */
  usedRange: string | null;
  visibility?: "Visible" | "Hidden" | "VeryHidden";
  tables?: { name: string; address: string }[];
}

export interface FakeExcel {
  excel: ExcelRuntime;
  /** Every property list passed to `load`, so tests can prove which payloads were read. */
  loads: string[];
  /** Every address requested through worksheet.getRange. */
  requestedRanges: string[];
  /** Any attempted property assignment on an Excel proxy object (must stay empty). */
  writes: string[];
}

function columnIndex(letters: string): number {
  return [...letters].reduce((index, letter) => index * 26 + letter.charCodeAt(0) - 64, 0);
}

function dimensions(address: string): { rows: number; columns: number } {
  const local = address.split("!").pop() ?? address;
  const [first, last = first] = local.split(":");
  const parse = (cell: string) => {
    const match = /^([A-Z]+)(\d+)$/.exec(cell);
    if (!match) throw new Error(`Bad address ${cell}`);
    return { column: columnIndex(match[1]), row: Number(match[2]) };
  };
  const start = parse(first);
  const end = parse(last);
  return { rows: end.row - start.row + 1, columns: end.column - start.column + 1 };
}

function quote(sheet: string): string {
  return /^[A-Za-z0-9_]+$/.test(sheet) ? sheet : `'${sheet.replaceAll("'", "''")}'`;
}

export function createFakeExcel(sheets: FakeSheet[], options: { selected?: string; /** All cells empty: no values and no formulas. */ blank?: boolean } = {}): FakeExcel {
  const loads: string[] = [];
  const requestedRanges: string[] = [];
  const writes: string[] = [];

  const readOnly = <T extends object>(target: T, label: string): T => new Proxy(target, {
    set(_, property) {
      writes.push(`${label}.${String(property)}`);
      throw new Error("Excel objects are read-only in FinLens tests.");
    },
  });

  const makeRange = (sheet: string, local: string | null, label: string) => {
    const size = local ? dimensions(local) : { rows: 0, columns: 0 };
    const matrix = (cell: (row: number, column: number) => unknown) => Array.from({ length: size.rows }, (_, row) => Array.from({ length: size.columns }, (__, column) => cell(row, column)));
    return readOnly({
      address: local ? `${quote(sheet)}!${local}` : "",
      rowCount: size.rows,
      columnCount: size.columns,
      values: matrix((row, column) => options.blank ? "" : `${sheet}:${row},${column}`),
      formulas: matrix(() => ""),
      numberFormat: matrix(() => "General"),
      isNullObject: !local,
      load: (properties: string) => { loads.push(`${label}:${properties}`); },
    }, label);
  };

  const worksheets = sheets.map((sheet, index) => readOnly({
    name: sheet.name,
    position: index,
    visibility: sheet.visibility ?? "Visible",
    tables: { items: (sheet.tables ?? []).map((table) => ({ name: table.name, getRange: () => makeRange(sheet.name, table.address, `table:${table.name}`) })), load: (properties: string) => { loads.push(`tables:${properties}`); } },
    getUsedRangeOrNullObject: () => makeRange(sheet.name, sheet.usedRange, `used:${sheet.name}`),
    getRange: (address: string) => {
      requestedRanges.push(`${sheet.name}!${address}`);
      return makeRange(sheet.name, address, `range:${sheet.name}`);
    },
    load: (properties: string) => { loads.push(`sheet:${properties}`); },
  }, `sheet:${sheet.name}`));

  const collection = {
    items: worksheets,
    load: (properties: string) => { loads.push(`worksheets:${properties}`); },
    getActiveWorksheet: () => worksheets[0],
    getItem: (name: string) => {
      const found = worksheets.find((sheet) => sheet.name === name);
      if (!found) throw new Error("ItemNotFound");
      return found;
    },
  };

  const excel = {
    run: async <T,>(callback: (context: never) => Promise<T>) => callback({
      workbook: { getSelectedRange: () => makeRange(sheets[0].name, options.selected ?? sheets[0].usedRange, "selection"), worksheets: collection },
      // Like the real host, sync yields to the event loop so UI stages can render.
      sync: () => new Promise<void>((resolve) => setTimeout(resolve, 0)),
    } as never),
  } as ExcelRuntime;

  return { excel, loads, requestedRanges, writes };
}
