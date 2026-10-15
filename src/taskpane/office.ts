export const REQUIRED_EXCEL_API_SET = "ExcelApi";
// The extractor uses stable ExcelApi 1.1 range/worksheet read APIs only.
export const REQUIRED_EXCEL_API_VERSION = "1.1";

export interface OfficeRuntime {
  onReady?: () => Promise<unknown>;
  context?: {
    requirements?: {
      isSetSupported: (setName: string, minimumVersion?: string) => boolean;
    };
  };
}

export type Compatibility = "checking" | "supported" | "unsupported";

declare global {
  interface Window {
    Office?: OfficeRuntime;
  }
}

/**
 * This adapter intentionally performs capability discovery only. It does not
 * access a workbook and therefore cannot mutate workbook content or structure.
 */
export function getCompatibility(office: OfficeRuntime | undefined = window.Office): Compatibility {
  const requirements = office?.context?.requirements;

  if (!requirements) {
    return "unsupported";
  }

  return requirements.isSetSupported(REQUIRED_EXCEL_API_SET, REQUIRED_EXCEL_API_VERSION)
    ? "supported"
    : "unsupported";
}
