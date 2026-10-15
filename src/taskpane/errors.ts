import { ApiError, AnalysisError } from "./analysis-api";
import { ContextError } from "./contracts";

// Story 3.2: one safe catalogue for every failure the task pane can show. Each issue has a plain
// Russian message (pane localisation) and exactly one next action; raw exceptions are never shown.

export type NextActionKind = "retry" | "choose_context" | "edit_question" | "update_excel" | "contact_admin";

export interface UiIssue {
  code: string;
  message: string;
  /** The single next step, as text. */
  nextAction: string;
  /** What the one action button does; update_excel and contact_admin are text-only. */
  action: NextActionKind;
  /** Sandbox attempts made before a computation failure. */
  computationAttempts?: number | null;
}

export const ACTION_LABELS: Record<NextActionKind, string | null> = {
  retry: "Повторить",
  choose_context: "Выбрать другой контекст",
  edit_question: "Изменить вопрос",
  update_excel: null,
  contact_admin: null,
};

const ACTION_BY_CODE: Record<string, NextActionKind> = {
  // Context problems detected in the pane: nothing is sent to the API.
  EMPTY_CONTEXT: "choose_context",
  CONTEXT_UNAVAILABLE: "choose_context",
  CONTEXT_TOO_LARGE: "choose_context",
  WORKBOOK_TOO_LARGE: "choose_context",
  WORKBOOK_SCOPE_TOO_LARGE: "choose_context",
  NO_RELEVANT_DATA: "choose_context",
  WORKBOOK_MAP_MISSING: "choose_context",
  REQUEST_INVALID: "choose_context",
  COMPUTATION_FAILED: "choose_context",
  QUESTION_EMPTY: "edit_question",
  QUESTION_TOO_LONG: "edit_question",
  QWEN_INVALID_RESPONSE: "edit_question",
  UNVERIFIED_COMPUTATION: "edit_question",
  UNSUPPORTED_EXCEL: "update_excel",
  QWEN_NOT_CONFIGURED: "contact_admin",
  SANDBOX_UNAVAILABLE: "contact_admin",
  // Transient or state problems: the same question can simply be sent again.
  NETWORK_UNAVAILABLE: "retry",
  API_UNAVAILABLE: "retry",
  STATUS_UNAVAILABLE: "retry",
  CANCEL_UNCONFIRMED: "retry",
  QWEN_UNAVAILABLE: "retry",
  SANDBOX_BUSY: "retry",
  SESSION_CAPACITY: "retry",
  ANALYSIS_CAPACITY: "retry",
  ANALYSIS_TIMEOUT: "retry",
  ANALYSIS_NOT_FOUND: "retry",
  ANALYSIS_CONFLICT: "retry",
  SESSION_NOT_FOUND: "retry",
  SESSION_SCOPE_MISMATCH: "retry",
  PLAN_NOT_FOUND: "retry",
  ARTIFACTS_MISMATCH: "retry",
  INTERNAL_ERROR: "retry",
};

const GENERIC_MESSAGE = "Не удалось выполнить анализ.";
const GENERIC_NEXT_ACTION = "Отправьте вопрос ещё раз.";
// Defence in depth: server texts are fixed, but anything resembling a trace or a path is never shown.
const UNSAFE_TEXT = /Traceback|File "|\bline \d+, in\b|[A-Za-z]:\\|\/(?:usr|home|srv|opt|var|tmp|Users|etc)\/|Bearer\s|api[_-]?key|FINLENS_/i;

function safe(text: string | undefined, fallback: string): string {
  const value = (text ?? "").trim();
  return value && value.length <= 600 && !UNSAFE_TEXT.test(value) ? value : fallback;
}

export function issue(code: string, message: string, nextAction: string, computationAttempts?: number | null): UiIssue {
  const action = ACTION_BY_CODE[code] ?? "retry";
  return { code, message: safe(message, GENERIC_MESSAGE), nextAction: safe(nextAction, GENERIC_NEXT_ACTION), action, computationAttempts };
}

export function issueFromError(error: AnalysisError, computationAttempts?: number | null): UiIssue {
  return issue(error.code, error.message, error.nextAction, computationAttempts);
}

export const unsupportedExcelIssue = (version: string): UiIssue => issue(
  "UNSUPPORTED_EXCEL",
  `Эта версия Excel не поддерживает ExcelApi ${version}, поэтому анализ недоступен.`,
  `Откройте книгу в Excel Desktop для Windows с поддержкой ExcelApi ${version} (при необходимости обновите Excel).`,
);

/** Any exception from Excel, the network or the API becomes a typed issue; its raw message is never shown. */
export function issueFromException(error: unknown): UiIssue {
  if (error instanceof ContextError) return issue(error.code, error.message, error.nextAction);
  if (error instanceof ApiError) return issue(error.code, error.message, error.nextAction);
  return issue("INTERNAL_ERROR", "Во время анализа произошла непредвиденная ошибка панели FinLens.", GENERIC_NEXT_ACTION);
}
