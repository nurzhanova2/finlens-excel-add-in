import { FormEvent, KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import { ContextDescriptor, ContextError, ContextScope, MAX_QUESTION_CHARS } from "./contracts";
import { describeContext } from "./excel-context";
import { AnalysisResultView } from "./AnalysisResultView";
import { AnalysisResult, closeSession, createSession } from "./analysis-api";
import { AnalysisRun, RunSession } from "./analysis-run";
import { ACTION_LABELS, issue, issueFromException, UiIssue, unsupportedExcelIssue } from "./errors";
import { MAX_EXECUTION_ATTEMPTS, Progress, stageLabel } from "./lifecycle";
import { Compatibility, getCompatibility, REQUIRED_EXCEL_API_VERSION } from "./office";

const scopeLabels: Record<ContextScope, string> = { range: "Выделение", sheet: "Лист", workbook: "Книга" };
const SCOPES: ContextScope[] = ["range", "sheet", "workbook"];

interface DialogTurn {
  id: string;
  scope: ContextScope;
  question: string;
  result: AnalysisResult;
  cancelTooLate: boolean;
}

type Outcome = { kind: "failed"; issue: UiIssue } | { kind: "cancelled"; message: string; nextAction: string };

function newId(): string {
  return crypto.randomUUID().replaceAll("-", "");
}

const seconds = (ms: number) => Math.max(1, Math.round(ms / 1000));

export function App({ pollIntervalMs }: { pollIntervalMs?: number } = {}) {
  const [compatibility] = useState<Compatibility>(() => getCompatibility());
  const [question, setQuestion] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const [scope, setScope] = useState<ContextScope>("range");
  const [context, setContext] = useState<ContextDescriptor | null>(null);
  const [contextIssue, setContextIssue] = useState<ContextError | null>(null);
  const [turns, setTurns] = useState<DialogTurn[]>([]);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  // Session identity and the active run live only in memory of this open pane; nothing is persisted.
  const session = useRef<RunSession | null>(null);
  const run = useRef<AnalysisRun | null>(null);
  const questionField = useRef<HTMLTextAreaElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const actionButton = useRef<HTMLButtonElement>(null);
  const scopeButtons = useRef<Partial<Record<ContextScope, HTMLButtonElement | null>>>({});
  const pendingFocus = useRef<"question" | "cancel" | "action" | null>(null);
  const isAnalyzing = progress !== null;

  const endSession = useCallback((options: { onUnload?: boolean } = {}) => {
    if (session.current) {
      // Closing the session also cancels its unfinished analysis on the API.
      closeSession(session.current.id, options);
      session.current = null;
    }
  }, []);

  useEffect(() => {
    const onPageHide = () => endSession({ onUnload: true });
    window.addEventListener("pagehide", onPageHide);
    return () => {
      window.removeEventListener("pagehide", onPageHide);
      endSession();
    };
  }, [endSession]);

  // Focus follows the flow: cancel while analysing (the disabled field would drop focus), then back.
  useEffect(() => {
    const target = pendingFocus.current;
    if (!target) return;
    const element = target === "cancel" ? cancelButton.current : target === "action" ? actionButton.current ?? questionField.current : questionField.current;
    if (element && !(element as HTMLButtonElement).disabled) {
      pendingFocus.current = null;
      element.focus();
    }
  });

  useEffect(() => {
    if (compatibility !== "supported") return;
    let active = true;
    describeContext(scope).then(
      (descriptor) => {
        if (!active) return;
        setContext(descriptor);
        setContextIssue(null);
      },
      (error: unknown) => {
        if (!active) return;
        const contextError = error instanceof ContextError
          ? error
          : new ContextError("CONTEXT_UNAVAILABLE", "Не удалось прочитать контекст Excel.", "Откройте книгу в Excel Desktop и повторите попытку.");
        setContextIssue(contextError);
        setAnnouncement(`Ошибка: ${contextError.message} Следующий шаг: ${contextError.nextAction}`);
      },
    );
    return () => { active = false; };
  }, [compatibility, scope]);

  const cancelRun = useCallback(() => {
    void run.current?.cancel();
  }, []);

  // Esc cancels the running analysis unless a chart dialog is open (there Esc closes the dialog).
  useEffect(() => {
    if (!isAnalyzing) return;
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape" && !document.querySelector('[role="dialog"][aria-modal="true"]')) {
        event.preventDefault();
        cancelRun();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [isAnalyzing, cancelRun]);

  function resetDialog() {
    endSession();
    setTurns([]);
    setOutcome(null);
  }

  function chooseScope(nextScope: ContextScope) {
    if (nextScope !== scope) {
      // A different scope always starts a clean session (history, map and artifacts are cleared).
      resetDialog();
    }
    setScope(nextScope);
    setContext(null);
    setContextIssue(null);
    setAnnouncement("");
  }

  function startNewDialog() {
    resetDialog();
    setAnnouncement("Начат новый диалог. История предыдущего сеанса удалена.");
    questionField.current?.focus();
  }

  function showIssue(problem: UiIssue) {
    setOutcome({ kind: "failed", issue: problem });
    setAnnouncement(`Ошибка: ${problem.message} Следующий шаг: ${problem.nextAction}`);
    pendingFocus.current = ACTION_LABELS[problem.action] ? "action" : "question";
  }

  async function ensureSession(signal: AbortSignal): Promise<RunSession> {
    if (session.current?.scope === scope) return session.current;
    endSession();
    const created = await createSession(scope, undefined, signal);
    session.current = { id: created.sessionId, scope, hasWorkbookMap: false };
    return session.current;
  }

  async function submitQuestion() {
    // Only one analysis at a time: a repeated Enter or click while it runs does nothing.
    if (run.current || compatibility !== "supported") return;
    const text = question.trim();
    if (!text) {
      showIssue(issue("QUESTION_EMPTY", "Вопрос не введён.", "Введите вопрос о выбранных данных."));
      return;
    }
    if (text.length > MAX_QUESTION_CHARS) {
      showIssue(issue("QUESTION_TOO_LONG", `Вопрос длиннее ${MAX_QUESTION_CHARS} символов.`, "Сократите вопрос и отправьте его ещё раз."));
      return;
    }
    if (contextIssue || !context) {
      // An invalid or oversized context is explained, never sent to the API.
      showIssue(contextIssue ? issueFromException(contextIssue) : issue("CONTEXT_UNAVAILABLE", "Контекст Excel ещё не прочитан.", "Выберите контекст и повторите вопрос."));
      return;
    }

    const analysis = new AnalysisRun({
      analysisId: newId(),
      scope,
      question: text,
      ensureSession,
      onProgress: (next) => {
        setProgress(next);
        setAnnouncement(`Этап: ${stageLabel(next)}`);
      },
      pollIntervalMs,
    });
    run.current = analysis;
    setOutcome(null);
    setProgress({ stage: "reading_context" });
    // The run announces its real first stage (a workbook follow-up skips building the map).
    setAnnouncement("Анализ запущен.");
    pendingFocus.current = "cancel";

    const result = await analysis.run();
    run.current = null;
    setProgress(null);
    if (result.kind === "completed") {
      const answer = result.result;
      setTurns((current) => [...current, { id: analysis.analysisId, scope, question: text, result: answer, cancelTooLate: result.cancelTooLate }]);
      setQuestion("");
      const computed = answer.computation ? ` Расчёт выполнен (попыток: ${answer.computation.attempts}).` : "";
      const late = result.cancelTooLate ? " Отмена пришла после завершения анализа, поэтому ответ показан." : "";
      const limited = answer.narrative?.insufficientData ? " Данных недостаточно для обоснованного вывода." : "";
      setAnnouncement(`Готово: ответ получен за ${seconds(answer.elapsedMs)} с.${computed}${limited}${late} Можно задать уточняющий вопрос.`);
      pendingFocus.current = "question";
    } else if (result.kind === "cancelled") {
      setOutcome({ kind: "cancelled", message: result.message, nextAction: result.nextAction });
      setAnnouncement(`Отменено: ${result.message} ${result.nextAction}`);
      pendingFocus.current = "question";
    } else {
      if (result.resetSession) {
        // The API has already erased this session, so the local history goes too.
        session.current = null;
        setTurns([]);
      }
      showIssue(result.issue);
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void submitQuestion();
  }

  function handleQuestionKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    // Enter sends, Shift+Enter makes a new line; slash commands switch the context.
    event.preventDefault();
    const command = question.trim().toLowerCase();
    if (["/range", "/sheet", "/workbook"].includes(command)) {
      chooseScope(command.slice(1) as ContextScope);
      setQuestion("");
      return;
    }
    if (canSubmit) void submitQuestion();
  }

  function performAction(problem: UiIssue) {
    if (problem.action === "retry") {
      void submitQuestion();
    } else if (problem.action === "choose_context") {
      scopeButtons.current[scope]?.focus();
    } else {
      questionField.current?.focus();
    }
  }

  const isSupported = compatibility === "supported";
  const canSubmit = isSupported && !contextIssue && Boolean(context) && !isAnalyzing;
  const isChecking = compatibility === "checking";
  const panelIssue = outcome?.kind === "failed" ? outcome.issue : contextIssue ? issueFromException(contextIssue) : null;
  const actionLabel = panelIssue ? ACTION_LABELS[panelIssue.action] : null;

  return (
    <main className="task-pane" aria-labelledby="finlens-title">
      <header className="task-pane__header">
        <h1 id="finlens-title">FinLens</h1>
        <p>Read-only аналитик для Excel</p>
        <div className="context-controls" role="group" aria-label="Контекст Excel">
          <p className="context-badge" aria-live="polite">
            <strong>{scopeLabels[scope]}</strong>
            {context ? ` · ${context.detail}` : contextIssue ? " · данные недоступны" : " · Читаем контекст…"}
          </p>
          <div className="context-buttons">
            {SCOPES.map((option) => (
              <button
                key={option}
                type="button"
                ref={(element) => { scopeButtons.current[option] = element; }}
                aria-pressed={scope === option}
                onClick={() => chooseScope(option)}
                disabled={!isSupported || isAnalyzing}
              >
                {scopeLabels[option]}
              </button>
            ))}
          </div>
        </div>
      </header>

      {compatibility === "unsupported" ? (
        <section className="notice notice--error" aria-labelledby="compatibility-title" role="alert">
          <h2 id="compatibility-title">Эта версия Excel не поддерживается</h2>
          <p>{unsupportedExcelIssue(REQUIRED_EXCEL_API_VERSION).message}</p>
          <p><strong>Следующий шаг:</strong> {unsupportedExcelIssue(REQUIRED_EXCEL_API_VERSION).nextAction}</p>
        </section>
      ) : (
        <section className="conversation" aria-label="Диалог FinLens">
          {turns.length === 0 ? (
            <p className="conversation__empty">Задайте вопрос о выбранных данных.</p>
          ) : (
            <>
              <ol className="dialog" aria-label="История текущего диалога">
                {turns.map((turn, index) => (
                  <li key={turn.id} className="dialog__turn">
                    <p className="dialog__question">
                      <span className="dialog__label">Вопрос {index + 1} · {scopeLabels[turn.scope]}:</span> {turn.question}
                    </p>
                    {turn.cancelTooLate && <p className="help-text">Отмена пришла после завершения анализа, поэтому ответ показан полностью.</p>}
                    <AnalysisResultView result={turn.result} />
                  </li>
                ))}
              </ol>
              <button type="button" className="secondary-button" onClick={startNewDialog} disabled={isAnalyzing}>
                Начать новый диалог
              </button>
            </>
          )}
        </section>
      )}

      {isSupported && !isAnalyzing && panelIssue && (
        <section className="notice notice--error" aria-labelledby="issue-title">
          <h2 id="issue-title">{outcome?.kind === "failed" ? "Не удалось выполнить анализ" : "Анализ недоступен для этого контекста"}</h2>
          <p>{panelIssue.message}</p>
          {panelIssue.computationAttempts ? (
            <p>Попыток расчёта: {panelIssue.computationAttempts} из {MAX_EXECUTION_ATTEMPTS}. Код, таблицы и графики не показаны, потому что расчёт не был успешно выполнен.</p>
          ) : null}
          <p><strong>Следующий шаг:</strong> {panelIssue.nextAction}</p>
          {actionLabel && (
            <button type="button" ref={actionButton} className="secondary-button" onClick={() => performAction(panelIssue)}>
              {actionLabel}
            </button>
          )}
        </section>
      )}

      {!isAnalyzing && outcome?.kind === "cancelled" && (
        <section className="notice notice--neutral" aria-labelledby="cancelled-title">
          <h2 id="cancelled-title">Анализ отменён</h2>
          <p>{outcome.message}</p>
          <p><strong>Следующий шаг:</strong> {outcome.nextAction}</p>
        </section>
      )}

      <form className="question-form" onSubmit={handleSubmit} aria-describedby="question-help">
        <label htmlFor="question">{turns.length ? "Уточняющий вопрос" : "Ваш вопрос"}</label>
        <textarea
          id="question"
          name="question"
          ref={questionField}
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={handleQuestionKeyDown}
          placeholder="Например: Какие показатели требуют проверки?"
          disabled={!isSupported || isAnalyzing}
          rows={4}
        />
        <p id="question-help" className="help-text">
          {isChecking ? "Проверяем совместимость Excel…" : "Enter — отправить, Shift+Enter — новая строка, Esc — отменить идущий анализ. /range, /sheet или /workbook меняют контекст и начинают новый диалог. Книга останется без изменений."}
        </p>
        <div className="question-form__actions">
          <button type="submit" disabled={!canSubmit}>
            {isAnalyzing ? "Анализ выполняется…" : "Отправить вопрос"}
          </button>
          {isAnalyzing && (
            <button type="button" ref={cancelButton} className="secondary-button" onClick={cancelRun}>
              Отменить анализ
            </button>
          )}
        </div>
      </form>

      {/* The live region announces stage changes only; the elapsed time below is not announced on every poll. */}
      <p className={progress ? "status status--active" : "status"} role="status" aria-live="polite">
        {announcement}
      </p>
      {progress?.elapsedMs ? <p className="help-text progress-elapsed">Анализ идёт {seconds(progress.elapsedMs)} с.</p> : null}
    </main>
  );
}
