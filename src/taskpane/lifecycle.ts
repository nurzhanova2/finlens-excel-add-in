// Typed analysis lifecycle (Story 3.1). Server stages mirror api/app/domain/contracts.py (ServerStage);
// reading_context and mapping_workbook happen in the task pane before or between API calls.

export type ServerStage =
  | "selecting_artifacts"
  | "planned"
  | "preparing_answer"
  | "running_computation"
  | "retrying_computation"
  | "finalizing"
  | "cancelling"
  | "completed"
  | "failed"
  | "cancelled";

export type AnalysisStage = Exclude<ServerStage, "planned"> | "reading_context" | "mapping_workbook";

export const SERVER_STAGES: readonly ServerStage[] = ["selecting_artifacts", "planned", "preparing_answer", "running_computation", "retrying_computation", "finalizing", "cancelling", "completed", "failed", "cancelled"];
export const TERMINAL_STAGES: ReadonlySet<string> = new Set(["completed", "failed", "cancelled"]);

// Mirrors api/app/domain/computation.py: the first run plus at most two automatic corrections.
export const MAX_EXECUTION_ATTEMPTS = 3;

export interface Progress {
  stage: AnalysisStage;
  /** Sandbox attempt in progress (1 = first run, 2..3 = corrections). */
  computationAttempt?: number | null;
  /** Exact ranges read in workbook phase 2. */
  detail?: string;
  elapsedMs?: number;
}

/** The text the analyst sees and the screen reader announces; never only a spinner or a colour. */
export function stageLabel(progress: Progress): string {
  switch (progress.stage) {
    case "reading_context":
      return progress.detail ? `Читаю только выбранные диапазоны: ${progress.detail}…` : "Читаю выбранные данные…";
    case "mapping_workbook":
      return "Строю карту книги…";
    case "selecting_artifacts":
      return "Выбираю релевантные листы и диапазоны…";
    case "preparing_answer":
      return "Готовлю вывод…";
    case "running_computation":
      return "Выполняю расчёт в изолированной песочнице…";
    case "retrying_computation": {
      const correction = Math.max(1, (progress.computationAttempt ?? 2) - 1);
      return `Исправляю расчёт (попытка ${correction} из ${MAX_EXECUTION_ATTEMPTS - 1})…`;
    }
    case "finalizing":
      return "Завершаю ответ…";
    case "cancelling":
      return "Отменяю анализ и удаляю временные данные…";
    case "completed":
      return "Ответ готов.";
    case "failed":
      return "Анализ не выполнен.";
    case "cancelled":
      return "Анализ отменён.";
  }
}
