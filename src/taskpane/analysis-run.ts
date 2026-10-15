import { AnalysisResult, AnalysisStatus, ApiError, cancelAnalysis, getAnalysisStatus, planWorkbook, requestAnalysis, requestWorkbookAnalysis } from "./analysis-api";
import { ContextError, ContextScope } from "./contracts";
import { issue, issueFromError, issueFromException, UiIssue } from "./errors";
import { ExcelRuntime, extractContext, hasAnalyzableData, readSelectedRanges } from "./excel-context";
import { Progress, TERMINAL_STAGES } from "./lifecycle";

// Story 3.1: one analysis from the first Excel read to a typed outcome. Progress comes from polling the
// typed status endpoint; cancel is an explicit API request whose confirmation decides the outcome.

export const DEFAULT_POLL_INTERVAL_MS = 500;
// Consecutive failed status requests tolerated before the progress is declared lost (never shown as active).
const MAX_STATUS_FAILURES = 3;
// The API has already erased these sessions, so the local history must go too.
const SESSION_RESET_CODES = new Set(["SESSION_NOT_FOUND", "SESSION_SCOPE_MISMATCH"]);

export interface RunSession {
  id: string;
  scope: ContextScope;
  hasWorkbookMap: boolean;
}

export type RunOutcome =
  | { kind: "completed"; result: AnalysisResult; /** The cancel arrived after the API had already committed the answer. */ cancelTooLate: boolean }
  | { kind: "failed"; issue: UiIssue; resetSession: boolean }
  | { kind: "cancelled"; message: string; nextAction: string };

export interface RunOptions {
  analysisId: string;
  scope: ContextScope;
  question: string;
  ensureSession: (signal: AbortSignal) => Promise<RunSession>;
  onProgress: (progress: Progress) => void;
  fetcher?: typeof fetch;
  excel?: ExcelRuntime;
  pollIntervalMs?: number;
}

const abortError = () => new DOMException("The analysis was cancelled.", "AbortError");
const isAbort = (error: unknown) => error instanceof DOMException && error.name === "AbortError";

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(abortError());
    const timer = setTimeout(() => { signal.removeEventListener("abort", onAbort); resolve(); }, ms);
    const onAbort = () => { clearTimeout(timer); reject(abortError()); };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

export class AnalysisRun {
  readonly analysisId: string;
  private readonly abort = new AbortController();
  private readonly fetcher: typeof fetch;
  private readonly pollIntervalMs: number;
  private cancellation: Promise<RunOutcome> | null = null;
  private serverTouched = false;

  constructor(private readonly options: RunOptions) {
    this.analysisId = options.analysisId;
    this.fetcher = options.fetcher ?? ((...args) => fetch(...args));
    this.pollIntervalMs = options.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS;
  }

  get cancelRequested(): boolean {
    return this.cancellation !== null;
  }

  async run(): Promise<RunOutcome> {
    try {
      const outcome = await this.execute();
      // A cancel that arrived while the answer was finishing is decided by the API, never by the race.
      return this.cancellation ? await this.cancellation : outcome;
    } catch (error) {
      if (this.cancellation) return await this.cancellation;
      if (this.serverTouched) {
        // The pane gives up on this analysis (e.g. status lost): ask the API to stop it as well.
        void cancelAnalysis(this.analysisId, this.fetcher).catch(() => undefined);
      }
      return { kind: "failed", issue: issueFromException(error), resetSession: false };
    }
  }

  /** Explicit cancel: abort local waits, ask the API to stop and clean up, and wait for its confirmation. */
  cancel(): Promise<RunOutcome> {
    if (!this.cancellation) {
      this.options.onProgress({ stage: "cancelling" });
      this.cancellation = this.confirmCancel();
      this.abort.abort();
    }
    return this.cancellation;
  }

  private async confirmCancel(): Promise<RunOutcome> {
    try {
      // Always sent, even before the first request: the API then refuses a late start under this ID.
      const status = await cancelAnalysis(this.analysisId, this.fetcher);
      return this.outcomeOf(await this.follow(status, { untilPlanned: false, report: false, signal: new AbortController().signal }));
    } catch {
      return {
        kind: "failed",
        resetSession: false,
        issue: issue("CANCEL_UNCONFIRMED", "Не удалось подтвердить отмену: нет связи с FinLens API. Ответ этого анализа показан не будет; сервер остановит его по таймауту.", "Проверьте подключение к корпоративной сети и нажмите «Повторить»."),
      };
    }
  }

  private check(): void {
    if (this.cancellation) throw abortError();
  }

  private progress(progress: Progress): void {
    if (!this.cancellation) this.options.onProgress(progress);
  }

  private async execute(): Promise<RunOutcome> {
    const { scope, question, excel } = this.options;
    const signal = this.abort.signal;
    const session = await this.options.ensureSession(signal);
    this.check();
    let status: AnalysisStatus;
    if (scope === "workbook") {
      let workbookMap;
      if (!session.hasWorkbookMap) {
        this.progress({ stage: "mapping_workbook" });
        workbookMap = (await extractContext("workbook", excel)).workbookMap;
        this.check();
      }
      this.progress({ stage: "selecting_artifacts" });
      this.serverTouched = true;
      status = await this.follow(await planWorkbook(session.id, { analysisId: this.analysisId, question, workbookMap }, this.fetcher, signal), { untilPlanned: true, report: true, signal });
      if (status.stage !== "planned" || !status.plan) return this.outcomeOf(status);
      session.hasWorkbookMap = true;
      const plan = status.plan;
      this.progress({ stage: "reading_context", detail: `${plan.selections.length} из карты книги — ${plan.selections.map((selection) => `${selection.sheetName}!${selection.address}`).join(", ")}` });
      const artifacts = await readSelectedRanges(plan.selections, excel);
      this.check();
      if (!artifacts.some(hasAnalyzableData)) {
        throw new ContextError("EMPTY_CONTEXT", "Выбранные для ответа диапазоны книги не содержат анализируемых данных.", "Выберите лист с нужными данными (контекст «Лист») и повторите вопрос.");
      }
      this.progress({ stage: "preparing_answer" });
      status = await requestWorkbookAnalysis(session.id, { analysisId: this.analysisId, artifacts }, this.fetcher, signal);
    } else {
      this.progress({ stage: "reading_context" });
      const snapshot = await extractContext(scope, excel);
      this.check();
      if (!snapshot.artifact) {
        throw new ContextError("CONTEXT_UNAVAILABLE", "Не удалось получить ограниченный контекст Excel.", "Выделите диапазон с данными и повторите вопрос.");
      }
      if (!hasAnalyzableData(snapshot.artifact)) {
        // Story 3.2: an all-blank context is never sent to Qwen or the sandbox.
        throw new ContextError("EMPTY_CONTEXT", "В выбранном контексте нет анализируемых данных: все ячейки пустые.", "Выделите диапазон с данными или выберите другой контекст.");
      }
      this.progress({ stage: "preparing_answer" });
      this.serverTouched = true;
      status = await requestAnalysis({ analysisId: this.analysisId, sessionId: session.id, scope, question, artifact: snapshot.artifact }, this.fetcher, signal);
    }
    return this.outcomeOf(await this.follow(status, { untilPlanned: false, report: true, signal }));
  }

  private async follow(initial: AnalysisStatus, options: { untilPlanned: boolean; report: boolean; signal: AbortSignal }): Promise<AnalysisStatus> {
    let status = initial;
    let failures = 0;
    while (!TERMINAL_STAGES.has(status.stage) && !(options.untilPlanned && status.stage === "planned")) {
      if (status.analysisId !== this.analysisId || status.stage === "planned") {
        throw new ApiError("API_UNAVAILABLE", "FinLens API вернул состояние другого анализа.", "Нажмите «Повторить».");
      }
      if (options.report) {
        this.progress({ stage: status.stage, computationAttempt: status.computationAttempt, elapsedMs: status.elapsedMs });
      }
      await sleep(this.pollIntervalMs, options.signal);
      try {
        status = await getAnalysisStatus(this.analysisId, this.fetcher, options.signal);
        failures = 0;
      } catch (error) {
        if (isAbort(error)) throw error;
        failures += 1;
        if (!(error instanceof ApiError) || failures >= MAX_STATUS_FAILURES) {
          // Progress can no longer be updated: it is replaced by an error, never left looking active.
          throw new ApiError("STATUS_UNAVAILABLE", "Связь с FinLens API потеряна во время анализа, его ход больше нельзя обновить.", "Проверьте подключение к корпоративной сети и нажмите «Повторить».");
        }
      }
    }
    if (status.analysisId !== this.analysisId) {
      throw new ApiError("API_UNAVAILABLE", "FinLens API вернул состояние другого анализа.", "Нажмите «Повторить».");
    }
    return status;
  }

  private outcomeOf(status: AnalysisStatus): RunOutcome {
    const result = status.result;
    if (!result) throw new ApiError("API_UNAVAILABLE", "FinLens API вернул неполное состояние анализа.", "Нажмите «Повторить».");
    if (result.status === "completed") return { kind: "completed", result, cancelTooLate: this.cancellation !== null };
    if (result.status === "cancelled") return { kind: "cancelled", message: result.message ?? "Анализ отменён.", nextAction: result.nextAction ?? "Задайте новый вопрос." };
    const error = result.error!;
    return { kind: "failed", issue: issueFromError(error, result.computationAttempts), resetSession: SESSION_RESET_CODES.has(error.code) };
  }
}
