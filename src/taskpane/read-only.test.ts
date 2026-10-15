import { act, render } from "@testing-library/react";
import { createElement } from "react";
import { describe, expect, it } from "vitest";
import { App } from "./App";
import { OfficeRuntime } from "./office";

describe("Story 1.1 read-only guarantee", () => {
  it("does not alter a workbook fixture when the task pane opens", async () => {
    const workbook = {
      worksheets: [{ name: "P&L", cells: [["Revenue", 1250], ["EBITDA", 220]] }],
    };
    const before = structuredClone(workbook);
    const office: OfficeRuntime & { workbook: typeof workbook } = {
      context: { requirements: { isSetSupported: () => true } },
      workbook,
    };

    window.Office = office;
    render(createElement(App));
    await act(async () => undefined);

    expect(workbook).toStrictEqual(before);
  });
});
