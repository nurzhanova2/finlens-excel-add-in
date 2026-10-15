import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// Static guards (AD-1, AD-6): the task pane never writes to the workbook and never persists data in the browser.
const root = join(process.cwd(), "src");

function sources(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return sources(path);
    return /\.(ts|tsx)$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) && !path.includes(`${join("src", "test")}`) ? [path] : [];
  });
}

const files = sources(root).map((path) => ({ path, text: readFileSync(path, "utf8") }));

const WRITE_PATTERNS = [
  /\.(values|formulas|numberFormat|formulasLocal|formulasR1C1|name|visibility|position)\s*=(?!=)/,
  /\.(insert|clear|delete|merge|unmerge|activate|calculate|copyFrom|moveTo|autofitColumns|autofitRows|setValues|setFormulas)\s*\(/,
  /\.(worksheets|tables|charts|names|comments|shapes)\.add\s*\(/,
  /\.add\s*\(/,
];
const STORAGE_PATTERNS = [/localStorage/, /sessionStorage/, /indexedDB/, /document\.cookie/, /caches\.open/, /Office\.context\.document\.settings/];

describe("static guards", () => {
  it("finds the task pane sources", () => {
    expect(files.length).toBeGreaterThan(8);
  });

  it("contains no Office.js write API", () => {
    for (const { path, text } of files) {
      for (const pattern of WRITE_PATTERNS) {
        expect({ path, match: text.match(pattern)?.[0] ?? null }).toEqual({ path, match: null });
      }
    }
  });

  it("uses no browser storage for history, results or session state", () => {
    for (const { path, text } of files) {
      for (const pattern of STORAGE_PATTERNS) {
        expect({ path, match: text.match(pattern)?.[0] ?? null }).toEqual({ path, match: null });
      }
    }
  });

  it("holds no Qwen credentials", () => {
    for (const { path, text } of files) {
      expect({ path, secret: /FINLENS_QWEN|API_KEY|Bearer\s/.test(text) }).toEqual({ path, secret: false });
    }
  });
});
