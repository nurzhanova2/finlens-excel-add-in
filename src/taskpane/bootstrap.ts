import { OfficeRuntime } from "./office";

export const OFFICE_READY_TIMEOUT_MS = 10_000;

export type OfficeReadiness = "office" | "browser" | "timeout";

/**
 * Resolves once Office.js has initialized. Outside an Office host (plain browser,
 * tests) or if initialization never completes, it resolves anyway so the pane can
 * render and its runtime compatibility check explains the unsupported state.
 */
export function waitForOffice(office: OfficeRuntime | undefined = window.Office, timeoutMs = OFFICE_READY_TIMEOUT_MS): Promise<OfficeReadiness> {
  if (typeof office?.onReady !== "function") {
    return Promise.resolve("browser");
  }
  const onReady = office.onReady.bind(office);
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve("timeout"), timeoutMs);
    const settle = (readiness: OfficeReadiness) => {
      clearTimeout(timer);
      resolve(readiness);
    };
    try {
      onReady().then(() => settle("office"), () => settle("timeout"));
    } catch {
      settle("timeout");
    }
  });
}

export async function startTaskPane(render: () => void, office?: OfficeRuntime, timeoutMs?: number): Promise<OfficeReadiness> {
  const readiness = await waitForOffice(office ?? window.Office, timeoutMs);
  render();
  return readiness;
}
