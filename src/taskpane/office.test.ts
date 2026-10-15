import { describe, expect, it } from "vitest";
import { getCompatibility, OfficeRuntime } from "./office";

describe("Office compatibility", () => {
  it("enables the pane only when the required Excel API is supported", () => {
    const supportedOffice: OfficeRuntime = {
      context: { requirements: { isSetSupported: (set, version) => set === "ExcelApi" && version === "1.1" } },
    };

    expect(getCompatibility(supportedOffice)).toBe("supported");
    expect(getCompatibility(undefined)).toBe("unsupported");
  });
});
