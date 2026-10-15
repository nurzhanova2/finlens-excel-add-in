import { describe, expect, it } from "vitest";
import { createFakeExcel } from "../test/fake-excel";
import { ContextError, MAX_MAP_SHEETS } from "./contracts";
import { extractContext, MAX_CONTEXT_CELLS, readSelectedRanges } from "./excel-context";

const cellPayload = /values|formulas|numberFormat/;

describe("read-only Excel context extractor", () => {
  it("extracts only the selected range including values, formulas, formats, sheet and address", async () => {
    const fake = createFakeExcel([{ name: "P&L", usedRange: "A1:D9" }], { selected: "B2:C3" });
    const snapshot = await extractContext("range", fake.excel);
    expect(snapshot.artifact).toMatchObject({ sheetName: "P&L", address: "'P&L'!B2:C3", values: [["P&L:0,0", "P&L:0,1"], ["P&L:1,0", "P&L:1,1"]], numberFormats: [["General", "General"], ["General", "General"]] });
    expect(fake.writes).toEqual([]);
  });

  it("reads the active sheet used range and rejects an empty sheet before a cell payload", async () => {
    await expect(extractContext("sheet", createFakeExcel([{ name: "P&L", usedRange: "A1:B1" }]).excel)).resolves.toMatchObject({ artifact: { address: "'P&L'!A1:B1" } });
    const empty = createFakeExcel([{ name: "P&L", usedRange: null }]);
    await expect(extractContext("sheet", empty.excel)).rejects.toMatchObject({ code: "EMPTY_CONTEXT" } satisfies Partial<ContextError>);
    expect(empty.loads.some((entry) => cellPayload.test(entry))).toBe(false);
  });

  it("builds a metadata-only workbook map with structure but no cell payload", async () => {
    const fake = createFakeExcel([
      { name: "P&L", usedRange: "A1:C3", tables: [{ name: "PnlTable", address: "A1:C3" }] },
      { name: "Notes", usedRange: null, visibility: "Hidden" },
    ]);
    const map = await extractContext("workbook", fake.excel);
    expect(map).toEqual({ scope: "workbook", workbookMap: { sheets: [
      { sheetName: "P&L", index: 0, visibility: "Visible", usedRangeAddress: "'P&L'!A1:C3", rowCount: 3, columnCount: 3, tables: [{ name: "PnlTable", address: "'P&L'!A1:C3" }] },
      { sheetName: "Notes", index: 1, visibility: "Hidden", usedRangeAddress: null, rowCount: 0, columnCount: 0, tables: [] },
    ] } });
    expect(fake.loads.some((entry) => cellPayload.test(entry))).toBe(false);
    expect(JSON.stringify(map)).not.toContain("P&L:0,0");
  });

  it("explains a workbook that exceeds the safe map limit", async () => {
    const sheets = Array.from({ length: MAX_MAP_SHEETS + 1 }, (_, index) => ({ name: `S${index}`, usedRange: "A1" }));
    await expect(extractContext("workbook", createFakeExcel(sheets).excel)).rejects.toMatchObject({ code: "WORKBOOK_TOO_LARGE", nextAction: expect.stringContaining("Лист") });
  });

  it("blocks oversized range payloads before values are loaded", async () => {
    const fake = createFakeExcel([{ name: "P&L", usedRange: "A1" }], { selected: `A1:A${MAX_CONTEXT_CELLS + 1}` });
    await expect(extractContext("range", fake.excel)).rejects.toMatchObject({ code: "CONTEXT_TOO_LARGE" } satisfies Partial<ContextError>);
    expect(fake.loads.some((entry) => cellPayload.test(entry))).toBe(false);
  });
});

describe("bounded reading of planned workbook ranges", () => {
  const sheets = [{ name: "P&L", usedRange: "A1:Z500" }, { name: "Debt", usedRange: "A1:C9" }, { name: "Secret", usedRange: "A1:B2" }];

  it("reads only the planned ranges", async () => {
    const fake = createFakeExcel(sheets);
    const artifacts = await readSelectedRanges([{ sheetName: "P&L", address: "A1:C4", reason: "" }, { sheetName: "Debt", address: "A1:C9", reason: "" }], fake.excel);
    expect(fake.requestedRanges).toEqual(["P&L!A1:C4", "Debt!A1:C9"]);
    expect(artifacts.map((artifact) => [artifact.sheetName, artifact.address, artifact.values.length])).toEqual([["P&L", "'P&L'!A1:C4", 4], ["Debt", "Debt!A1:C9", 9]]);
    expect(JSON.stringify(artifacts)).not.toContain("Secret:");
    expect(fake.writes).toEqual([]);
  });

  it("refuses too many or too large selections before loading any values", async () => {
    const fake = createFakeExcel(sheets);
    await expect(readSelectedRanges(Array.from({ length: 6 }, () => ({ sheetName: "Debt", address: "A1", reason: "" })), fake.excel)).rejects.toMatchObject({ code: "CONTEXT_TOO_LARGE" });
    await expect(readSelectedRanges([{ sheetName: "P&L", address: "A1:Z400", reason: "" }], fake.excel)).rejects.toMatchObject({ code: "CONTEXT_TOO_LARGE" });
    await expect(readSelectedRanges([{ sheetName: "P&L", address: "A1:Y300", reason: "" }, { sheetName: "P&L", address: "A1:Y300", reason: "" }, { sheetName: "P&L", address: "A1:Y300", reason: "" }], fake.excel)).rejects.toMatchObject({ code: "CONTEXT_TOO_LARGE", message: expect.stringContaining("20000") });
    expect(fake.loads.some((entry) => cellPayload.test(entry))).toBe(false);
  });
});
