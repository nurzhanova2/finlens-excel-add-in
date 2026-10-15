import { afterEach, describe, expect, it, vi } from "vitest";
import { startTaskPane, waitForOffice } from "./bootstrap";
import { getCompatibility } from "./office";

describe("task pane bootstrap", () => {
  afterEach(() => {
    vi.useRealTimers();
    delete window.Office;
  });

  it("renders only after Office.onReady resolves and then sees the initialized context", async () => {
    let ready!: () => void;
    const office: NonNullable<typeof window.Office> = { onReady: () => new Promise<void>((resolve) => { ready = resolve; }) };
    const render = vi.fn(() => getCompatibility(office));
    const started = startTaskPane(render, office);

    await Promise.resolve();
    expect(render).not.toHaveBeenCalled();
    office.context = { requirements: { isSetSupported: () => true } };
    ready();

    await expect(started).resolves.toBe("office");
    expect(render).toHaveBeenCalledTimes(1);
    expect(render).toHaveReturnedWith("supported");
  });

  it("falls back to rendering immediately outside Office for browser tests", async () => {
    const render = vi.fn();
    await expect(startTaskPane(render, undefined)).resolves.toBe("browser");
    expect(render).toHaveBeenCalledTimes(1);
  });

  it("renders the unsupported state if Office never becomes ready or fails", async () => {
    vi.useFakeTimers();
    const hanging = waitForOffice({ onReady: () => new Promise(() => undefined) }, 5_000);
    await vi.advanceTimersByTimeAsync(5_000);
    await expect(hanging).resolves.toBe("timeout");

    await expect(waitForOffice({ onReady: () => Promise.reject(new Error("host error")) }, 5_000)).resolves.toBe("timeout");
    await expect(waitForOffice({ onReady: () => { throw new Error("sync error"); } }, 5_000)).resolves.toBe("timeout");
  });
});
