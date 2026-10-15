import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createFakeExcel, FakeExcel } from "../test/fake-excel";
import { App } from "./App";

const SESSION_ID = "s".repeat(43);

interface ApiCall { url: string; method: string; body: Record<string, unknown> | null }
type Route = (body: Record<string, unknown>, url: string) => Response | Promise<Response>;

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const narrative = (conclusion: string) => ({ conclusion, evidence: ["Факт из диапазона"], risks: ["Нужна сверка"], assumptions: [], nextCheck: "Проверьте первичные данные." });
const status = (analysisId: unknown, stage: string, extra: Record<string, unknown> = {}) => ({ analysisId, stage, computationAttempt: null, maxComputationAttempts: 3, elapsedMs: 3000, plan: null, result: null, ...extra });
const done = (result: Record<string, unknown>) => status(result.analysisId, String(result.status), { result });
const failure = (analysisId: unknown, code: string, message: string, nextAction: string, extra: Record<string, unknown> = {}) => done({ analysisId, status: "failed", elapsedMs: 2, error: { code, message, nextAction }, ...extra });

/** A fake FinLens API: start endpoints answer with a typed status; polls and cancel can be overridden. */
function mockApi(routes: { plan?: Route; workbookAnalysis?: Route; analysis?: Route; poll?: Route; cancel?: Route } = {}) {
  const calls: ApiCall[] = [];
  let sessions = 0;
  const fetcher = vi.fn(async (url: string, init: RequestInit = {}) => {
    const body = typeof init.body === "string" ? JSON.parse(init.body) as Record<string, unknown> : null;
    calls.push({ url, method: init.method ?? "GET", body });
    await new Promise((resolve) => setTimeout(resolve, 0));
    if (url.endsWith("/api/sessions")) {
      sessions += 1;
      return json({ sessionId: `${SESSION_ID}${sessions}`, scope: body?.scope, ttlSeconds: 1800 }, 201);
    }
    if (init.method === "DELETE") return new Response(null, { status: 204 });
    const analysisId = url.match(/\/api\/analyses\/([^/]+)/)?.[1] ?? body?.analysisId;
    if (url.endsWith("/cancel")) return routes.cancel?.({ analysisId }, url) ?? json(done({ analysisId, status: "cancelled", elapsedMs: 4, message: "Анализ отменён. Временные данные запроса удалены, ответ не сформирован.", nextAction: "Задайте новый вопрос." }));
    if ((init.method ?? "GET") === "GET") return routes.poll?.({ analysisId }, url) ?? json(status(analysisId, "preparing_answer"));
    if (url.endsWith("/workbook/plan")) {
      return routes.plan?.(body!, url) ?? json(status(analysisId, "planned", { plan: { analysisId, status: "planned", sheetsInWorkbook: 3, elapsedMs: 3, selections: [{ sheetName: "P&L", address: "A1:C3", reason: "Выручка" }, { sheetName: "Debt", address: "B2:C4", reason: "Долг" }] } }), 202);
    }
    if (url.endsWith("/workbook/analysis")) {
      return routes.workbookAnalysis?.(body!, url) ?? json(done({ analysisId, status: "completed", elapsedMs: 9000, narrative: narrative(`Ответ ${calls.filter((call) => call.url.endsWith("/workbook/analysis")).length}`), sources: [{ sheetName: "P&L", address: "'P&L'!A1:C3" }, { sheetName: "Debt", address: "Debt!B2:C4" }], coverage: { sheetsInWorkbook: 3, sheetsWithData: 2, sheetsUsed: ["P&L", "Debt"], complete: false } }), 202);
    }
    return routes.analysis?.(body!, url) ?? json(done({ analysisId, status: "completed", elapsedMs: 12000, narrative: narrative("Выручка выросла."), sources: [{ sheetName: "P&L", address: "'P&L'!A1:C3" }] }), 202);
  });
  vi.stubGlobal("fetch", fetcher);
  return { calls, fetcher };
}

const liveRegion = () => document.querySelector("p.status[role='status']")!;

function recordAnnouncements(): string[] {
  const announcements: string[] = [];
  const element = liveRegion();
  new MutationObserver(() => announcements.push(element.textContent ?? "")).observe(element, { childList: true, characterData: true, subtree: true });
  return announcements;
}

const sendButton = () => screen.getByRole("button", { name: /Отправить вопрос|Анализ выполняется/ });

async function type(text: string) {
  const question = await screen.findByRole("textbox");
  fireEvent.change(question, { target: { value: text } });
  await waitFor(() => expect(screen.getByRole("button", { name: "Отправить вопрос" })).toBeEnabled());
  return question;
}

async function ask(text: string) {
  await type(text);
  fireEvent.click(screen.getByRole("button", { name: "Отправить вопрос" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Отправить вопрос" })).toBeInTheDocument());
}

async function chooseWorkbook() {
  fireEvent.click(await screen.findByRole("button", { name: "Книга" }));
  expect(await screen.findByText(/3 лист\(ов\), с данными: 2/)).toBeVisible();
}

describe("FinLens task pane", () => {
  let fake: FakeExcel;

  afterEach(() => vi.unstubAllGlobals());
  beforeEach(() => {
    window.Office = { context: { requirements: { isSetSupported: () => true } } };
    fake = createFakeExcel([
      { name: "P&L", usedRange: "A1:C3" },
      { name: "Debt", usedRange: "B2:C4" },
      { name: "Notes", usedRange: null, visibility: "Hidden" },
    ]);
    window.Excel = fake.excel;
  });

  it("renders the accessible empty conversation and question controls", async () => {
    render(<App pollIntervalMs={1} />);

    expect(await screen.findByRole("heading", { name: "FinLens" })).toBeVisible();
    expect(screen.getByLabelText("Диалог FinLens")).toHaveTextContent("Задайте вопрос о выбранных данных.");
    expect(screen.getByLabelText("Ваш вопрос")).toBeEnabled();
    expect(await screen.findByRole("button", { name: "Отправить вопрос" })).toBeEnabled();
    expect(liveRegion()).toHaveAttribute("aria-live", "polite");
    expect(screen.queryByRole("button", { name: "Отменить анализ" })).not.toBeInTheDocument();
  });

  it("explains an unsupported Excel version with one next step before any analysis", async () => {
    window.Office = { context: { requirements: { isSetSupported: () => false } } };
    const api = mockApi();
    render(<App pollIntervalMs={1} />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Эта версия Excel не поддерживается");
    expect(alert).toHaveTextContent("Следующий шаг: Откройте книгу в Excel Desktop для Windows с поддержкой ExcelApi 1.1");
    expect(screen.getByLabelText("Ваш вопрос")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Отправить вопрос" })).toBeDisabled();
    expect(api.fetcher).not.toHaveBeenCalled();
  });

  it("keeps the Tab order equal to the reading order", async () => {
    mockApi();
    render(<App pollIntervalMs={1} />);
    await ask("Что изменилось?");
    await screen.findByLabelText("Ответ аналитика");
    const focusable = [...document.querySelectorAll<HTMLElement>("button, textarea, summary, [tabindex='0']")].filter((element) => !(element as HTMLButtonElement).disabled);
    const order = focusable.map((element) => element.tagName === "TEXTAREA" ? "textarea" : element.textContent?.trim());
    expect(order).toEqual(["Выделение", "Лист", "Книга", "Копировать источники", "Начать новый диалог", "textarea", "Отправить вопрос"]);
  });

  it("sends with Enter, keeps Shift+Enter for a new line and switches context with a slash command", async () => {
    const api = mockApi();
    render(<App pollIntervalMs={1} />);
    const question = await type("Первая строка");
    fireEvent.keyDown(question, { key: "Enter", shiftKey: true });
    expect(api.calls.some((call) => call.url.endsWith("/api/analysis"))).toBe(false);
    fireEvent.keyDown(question, { key: "Enter" });
    expect(await screen.findByLabelText("Ответ аналитика")).toHaveTextContent("Выручка выросла.");

    fireEvent.change(question, { target: { value: "/sheet" } });
    fireEvent.keyDown(question, { key: "Enter" });
    expect(await screen.findByRole("button", { name: "Лист", pressed: true })).toBeVisible();
    await waitFor(() => expect(document.querySelector(".context-badge")).toHaveTextContent("Лист · P&L · 'P&L'!A1:C3"));
  });

  it("renders a structured answer with exact copyable sources and keeps the session for a follow-up", async () => {
    const api = mockApi();
    render(<App pollIntervalMs={1} />);
    const announcements = recordAnnouncements();
    await ask("Что изменилось?");
    const answer = await screen.findByLabelText("Ответ аналитика");
    expect(answer).toHaveTextContent("Выручка выросла.");
    expect(within(answer).getByRole("list", { name: "Использованные листы и диапазоны" })).toHaveTextContent("P&L · A1:C3");
    await waitFor(() => expect(screen.getByLabelText("Уточняющий вопрос")).toHaveFocus());
    expect(announcements).toContain("Этап: Читаю выбранные данные…");
    expect(announcements.at(-1)).toMatch(/^Готово: ответ получен за 12 с\. Можно задать уточняющий вопрос\.$/);

    await ask("А почему?");
    await waitFor(() => expect(screen.getAllByLabelText("Ответ аналитика")).toHaveLength(2));
    const analyses = api.calls.filter((call) => call.url.endsWith("/api/analysis"));
    expect(analyses.map((call) => call.body?.sessionId)).toEqual([`${SESSION_ID}1`, `${SESSION_ID}1`]);
    expect(api.calls.filter((call) => call.url.endsWith("/api/sessions"))).toHaveLength(1);
  });

  it("shows progress text, blocks a second submission and moves focus to «Отменить анализ»", async () => {
    let stage = "running_computation";
    const api = mockApi({
      analysis: (body) => json(status(body.analysisId, "preparing_answer"), 202),
      poll: ({ analysisId }) => json(status(analysisId, stage, { computationAttempt: stage === "preparing_answer" ? null : 2 })),
    });
    render(<App pollIntervalMs={1} />);
    const announcements = recordAnnouncements();
    const question = await type("Посчитай рост");
    fireEvent.keyDown(question, { key: "Enter" });

    const cancel = await screen.findByRole("button", { name: "Отменить анализ" });
    await waitFor(() => expect(cancel).toHaveFocus());
    expect(sendButton()).toBeDisabled();
    expect(sendButton()).toHaveTextContent("Анализ выполняется…");
    expect(question).toBeDisabled();
    stage = "retrying_computation";
    await waitFor(() => expect(liveRegion()).toHaveTextContent("Этап: Исправляю расчёт (попытка 1 из 2)…"));
    expect(screen.getByText("Анализ идёт 3 с.")).toBeInTheDocument();

    fireEvent.keyDown(question, { key: "Enter" });
    fireEvent.submit(question.closest("form")!);
    expect(api.calls.filter((call) => call.url.endsWith("/api/analysis"))).toHaveLength(1);
    expect(announcements).toContain("Этап: Готовлю вывод…");
    fireEvent.click(cancel);
    await screen.findByRole("heading", { name: "Анализ отменён" });
  });

  it("cancels with a real API request, announces it and lets the analyst ask again", async () => {
    const api = mockApi({ analysis: (body) => json(status(body.analysisId, "preparing_answer"), 202) });
    render(<App pollIntervalMs={1} />);
    const announcements = recordAnnouncements();
    await type("Долгий вопрос");
    fireEvent.click(screen.getByRole("button", { name: "Отправить вопрос" }));
    const cancel = await screen.findByRole("button", { name: "Отменить анализ" });
    await waitFor(() => expect(api.calls.some((call) => call.url.endsWith("/api/analysis"))).toBe(true));
    fireEvent.click(cancel);

    const panel = (await screen.findByRole("heading", { name: "Анализ отменён" })).closest("section")!;
    expect(panel).toHaveTextContent("Временные данные запроса удалены, ответ не сформирован.");
    expect(panel).toHaveTextContent("Следующий шаг: Задайте новый вопрос.");
    expect(announcements).toContain("Этап: Отменяю анализ и удаляю временные данные…");
    expect(announcements.at(-1)).toMatch(/^Отменено: /);
    const analysisId = api.calls.find((call) => call.url.endsWith("/api/analysis"))!.body!.analysisId;
    expect(api.calls.some((call) => call.method === "POST" && call.url === `/api/analyses/${analysisId}/cancel`)).toBe(true);
    expect(screen.queryByLabelText("Ответ аналитика")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Ваш вопрос")).toHaveFocus());
    expect(screen.getByRole("button", { name: "Отправить вопрос" })).toBeEnabled();
    expect(screen.getByLabelText("Ваш вопрос")).toHaveValue("Долгий вопрос");
  });

  it("cancels the running analysis with Esc", async () => {
    const api = mockApi({ analysis: (body) => json(status(body.analysisId, "preparing_answer"), 202) });
    render(<App pollIntervalMs={1} />);
    await type("Вопрос");
    fireEvent.click(screen.getByRole("button", { name: "Отправить вопрос" }));
    await screen.findByRole("button", { name: "Отменить анализ" });
    fireEvent.keyDown(document, { key: "Escape" });
    await screen.findByRole("heading", { name: "Анализ отменён" });
    expect(api.calls.filter((call) => call.url.endsWith("/cancel"))).toHaveLength(1);
  });

  it("replaces lost progress with an error and exactly one «Повторить» action that resends the question", async () => {
    let offline = true;
    const api = mockApi({
      analysis: (body) => (offline ? json(status(body.analysisId, "preparing_answer"), 202) : json(done({ analysisId: body.analysisId, status: "completed", elapsedMs: 1000, narrative: narrative("Готово."), sources: [{ sheetName: "P&L", address: "A1:C3" }] }), 202)),
      poll: () => { throw new TypeError("Failed to fetch"); },
    });
    render(<App pollIntervalMs={1} />);
    await ask("Что изменилось?");
    const panel = (await screen.findByRole("heading", { name: "Не удалось выполнить анализ" })).closest("section")!;
    expect(panel).toHaveTextContent("Связь с FinLens API потеряна во время анализа");
    expect(panel).not.toHaveTextContent("Failed to fetch");
    expect(within(panel).getAllByRole("button")).toHaveLength(1);
    expect(screen.queryByText(/^Этап:/)).not.toBeInTheDocument();
    expect(liveRegion()).toHaveTextContent(/^Ошибка: .*Следующий шаг: Проверьте подключение/);
    const retry = within(panel).getByRole("button", { name: "Повторить" });
    await waitFor(() => expect(retry).toHaveFocus());

    offline = false;
    fireEvent.click(retry);
    expect(await screen.findByLabelText("Ответ аналитика")).toHaveTextContent("Готово.");
    expect(api.calls.filter((call) => call.url.endsWith("/api/analysis"))).toHaveLength(2);
  });

  it("explains an unreachable API on the first request with a retry action", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    render(<App pollIntervalMs={1} />);
    await ask("Вопрос");
    const panel = (await screen.findByRole("heading", { name: "Не удалось выполнить анализ" })).closest("section")!;
    expect(panel).toHaveTextContent("Нет связи с FinLens API.");
    expect(within(panel).getByRole("button", { name: "Повторить" })).toBeVisible();
  });

  it("does not analyse an all-blank selection and offers to choose another context", async () => {
    window.Excel = createFakeExcel([{ name: "P&L", usedRange: "A1:C3" }], { blank: true }).excel;
    const api = mockApi();
    render(<App pollIntervalMs={1} />);
    await ask("Что здесь?");
    const panel = (await screen.findByRole("heading", { name: "Не удалось выполнить анализ" })).closest("section")!;
    expect(panel).toHaveTextContent("нет анализируемых данных");
    fireEvent.click(within(panel).getByRole("button", { name: "Выбрать другой контекст" }));
    expect(screen.getByRole("button", { name: "Выделение", pressed: true })).toHaveFocus();
    expect(api.calls.some((call) => call.url.endsWith("/api/analysis"))).toBe(false);
  });

  it("blocks an oversized selection before any request and explains the limit", async () => {
    window.Excel = createFakeExcel([{ name: "P&L", usedRange: "A1:Z1000" }]).excel;
    const api = mockApi();
    render(<App pollIntervalMs={1} />);
    const panel = (await screen.findByRole("heading", { name: "Анализ недоступен для этого контекста" })).closest("section")!;
    expect(panel).toHaveTextContent("26000 ячеек, что превышает лимит 10000");
    expect(panel).toHaveTextContent("Следующий шаг:");
    expect(within(panel).getAllByRole("button")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Отправить вопрос" })).toBeDisabled();
    expect(api.fetcher).not.toHaveBeenCalled();
  });

  it("rejects an over-long question locally", async () => {
    const api = mockApi();
    render(<App pollIntervalMs={1} />);
    await ask("x".repeat(4001));
    const panel = (await screen.findByRole("heading", { name: "Не удалось выполнить анализ" })).closest("section")!;
    expect(panel).toHaveTextContent("Вопрос длиннее 4000 символов.");
    expect(within(panel).getByRole("button", { name: "Изменить вопрос" })).toBeVisible();
    expect(api.calls.some((call) => call.url.endsWith("/api/analysis"))).toBe(false);
  });

  it("runs the two-phase workbook flow with visible stages, bounded reads and explicit partial coverage", async () => {
    const api = mockApi();
    render(<App pollIntervalMs={1} />);
    await chooseWorkbook();
    const announcements = recordAnnouncements();
    await ask("Что это за модель и какие риски?");

    const answer = await screen.findByLabelText("Ответ аналитика");
    expect(answer).toHaveTextContent("Проанализирована только часть книги. Использованы 2 из 2 лист(ов) с данными (всего листов в книге: 3) — P&L, Debt, и только диапазоны, перечисленные ниже. Это не анализ всей книги.");
    expect(answer).toHaveTextContent("Debt · B2:C4");
    expect(announcements).toEqual(expect.arrayContaining(["Этап: Строю карту книги…", "Этап: Выбираю релевантные листы и диапазоны…", "Этап: Готовлю вывод…"]));
    expect(announcements).toContain("Этап: Читаю только выбранные диапазоны: 2 из карты книги — P&L!A1:C3, Debt!B2:C4…");

    const plan = api.calls.find((call) => call.url.endsWith("/workbook/plan"))!;
    expect(plan.url).toContain(`/api/sessions/${SESSION_ID}1/workbook/plan`);
    expect(plan.body).toMatchObject({ question: "Что это за модель и какие риски?", workbookMap: { sheets: expect.any(Array) } });
    expect(JSON.stringify(plan.body)).not.toMatch(/P&L:0,0|"values"|"formulas"/);

    expect(fake.requestedRanges).toEqual(["P&L!A1:C3", "Debt!B2:C4"]);
    const analysis = api.calls.find((call) => call.url.endsWith("/workbook/analysis"))!;
    expect((analysis.body!.artifacts as { address: string }[]).map((artifact) => artifact.address)).toEqual(["'P&L'!A1:C3", "Debt!B2:C4"]);
    expect(analysis.body!.analysisId).toBe(plan.body!.analysisId);
    expect(fake.writes).toEqual([]);
  });

  it("answers a workbook follow-up from session memory without rebuilding or resending the map", async () => {
    const api = mockApi();
    render(<App pollIntervalMs={1} />);
    await chooseWorkbook();
    await ask("Какие ключевые допущения?");
    await screen.findByLabelText("Ответ аналитика");
    const announcements = recordAnnouncements();
    await ask("А что с долгом?");

    await waitFor(() => expect(screen.getAllByLabelText("Ответ аналитика")).toHaveLength(2));
    const history = screen.getByRole("list", { name: "История текущего диалога" });
    expect(within(history).getByText("Какие ключевые допущения?")).toBeVisible();
    expect(within(history).getByText("А что с долгом?")).toBeVisible();
    const plans = api.calls.filter((call) => call.url.endsWith("/workbook/plan"));
    expect(plans[1].body).not.toHaveProperty("workbookMap");
    expect(plans[1].url).toBe(plans[0].url);
    expect(announcements).not.toContain("Этап: Строю карту книги…");
    expect(screen.getByRole("button", { name: "Книга", pressed: true })).toBeVisible();
  });

  it("starts a clean session when the scope changes and clears it on pane unload", async () => {
    const api = mockApi();
    const sendBeacon = vi.fn(() => true);
    vi.stubGlobal("navigator", { ...navigator, sendBeacon });
    render(<App pollIntervalMs={1} />);
    await chooseWorkbook();
    await ask("Что это за модель?");
    await screen.findByLabelText("Ответ аналитика");

    fireEvent.click(screen.getByRole("button", { name: "Лист" }));
    expect(api.calls.some((call) => call.method === "DELETE" && call.url.endsWith(`/api/sessions/${SESSION_ID}1`))).toBe(true);
    expect(screen.queryByLabelText("Ответ аналитика")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Диалог FinLens")).toHaveTextContent("Задайте вопрос о выбранных данных.");

    await ask("Что на листе?");
    await screen.findByLabelText("Ответ аналитика");
    expect(api.calls.filter((call) => call.url.endsWith("/api/sessions")).map((call) => call.body?.scope)).toEqual(["workbook", "sheet"]);

    window.dispatchEvent(new Event("pagehide"));
    expect(sendBeacon).toHaveBeenCalledWith(expect.stringMatching(new RegExp(`/api/sessions/${SESSION_ID}2/close$`)));
  });

  it("explains an oversized workbook scope with one next action and no data read", async () => {
    mockApi({ plan: (body) => json(failure(body.analysisId, "WORKBOOK_SCOPE_TOO_LARGE", "Релевантные данные занимают 50000 ячеек, а безопасный лимит одного анализа — 20000 ячеек.", "Выберите контекст «Лист» или «Выделение» с нужными данными и повторите вопрос.")) });
    render(<App pollIntervalMs={1} />);
    await chooseWorkbook();
    await ask("Проанализируй всё");

    const panel = (await screen.findByRole("heading", { name: "Не удалось выполнить анализ" })).closest("section")!;
    expect(panel).toHaveTextContent("Релевантные данные занимают 50000 ячеек");
    expect(panel).toHaveTextContent("Следующий шаг: Выберите контекст «Лист» или «Выделение» с нужными данными и повторите вопрос.");
    expect(within(panel).getAllByRole("button").map((button) => button.textContent)).toEqual(["Выбрать другой контекст"]);
    expect(fake.requestedRanges).toEqual([]);
    expect(screen.getByLabelText("Ваш вопрос")).toHaveValue("Проанализируй всё");
  });

  it("shows a computed answer with its execution artifacts without writing to Excel", async () => {
    const png = "iVBORw0KGgoAAAANSUhEUgAAAAQAAAADCAIAAAA7ljmRAAAAEklEQVR4nGP4//8/AxJgxOQDAKKXBfvtPbkzAAAAAElFTkSuQmCC";
    const computation = { status: "succeeded", code: "print(1)\n", method: "Сумма по столбцам.", attempts: 1, inputs: [{ sheetName: "P&L", address: "'P&L'!A1:C3" }], tables: [{ title: "Итоги", columns: ["Столбец", "Сумма"], rows: [["B", 3]], totalRows: 1, truncated: false }], charts: [{ title: "Сумма", pngBase64: png, widthPx: 4, heightPx: 3, description: { metric: "Сумма", period: "Строки 2–3", insight: "Максимум в B" } }], stdout: "", durationMs: 800 };
    mockApi({
      analysis: (body) => json(status(body.analysisId, "running_computation", { computationAttempt: 1 }), 202),
      poll: ({ analysisId }) => json(done({ analysisId, status: "completed", elapsedMs: 900, narrative: narrative("Сумма по столбцу B — 3 (расчёт)."), sources: [{ sheetName: "P&L", address: "'P&L'!A1:C3" }], computation })),
    });
    render(<App pollIntervalMs={1} />);
    const announcements = recordAnnouncements();
    await ask("Посчитай суммы");

    expect(await screen.findByRole("region", { name: "Проверяемый расчёт" })).toHaveTextContent("Расчёт выполнен в изолированной песочнице с первой попытки");
    expect(screen.getByRole("table", { name: "Итоги" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Метрика: Сумма\. Период: Строки 2–3\. Ключевой вывод: Максимум в B/ })).toBeInTheDocument();
    expect(announcements).toContain("Этап: Выполняю расчёт в изолированной песочнице…");
    await waitFor(() => expect(liveRegion()).toHaveTextContent("Расчёт выполнен (попыток: 1)"));

    // Keyboard: open the chart with Enter on its button, close with Esc, focus returns to the chart.
    const open = screen.getByRole("button", { name: "Увеличить график «Сумма»" });
    open.focus();
    fireEvent.click(open);
    expect(screen.getByRole("dialog", { name: "Сумма" })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(open).toHaveFocus();
    expect(fake.writes).toEqual([]);
  });

  it("flags insufficient data in the answer and the announcement", async () => {
    mockApi({ analysis: (body) => json(done({ analysisId: body.analysisId, status: "completed", elapsedMs: 900, narrative: { ...narrative("Подтвердить рост нельзя."), insufficientData: true, missingData: ["Данные 2024 года"] }, sources: [{ sheetName: "P&L", address: "A1:C3" }] }), 202) });
    render(<App pollIntervalMs={1} />);
    await ask("Сравни годы");
    expect(await screen.findByRole("region", { name: "Ограничение вывода" })).toHaveTextContent("Данные 2024 года");
    expect(liveRegion()).toHaveTextContent("Данных недостаточно для обоснованного вывода.");
  });

  it("shows an honest computation failure with one next action and no artifacts", async () => {
    mockApi({ analysis: (body) => json(failure(body.analysisId, "COMPUTATION_FAILED", "Расчёт не удалось выполнить после 3 попыток.", "Выделите меньший диапазон с нужными данными и повторите вопрос.", { computationAttempts: 3 })) });
    render(<App pollIntervalMs={1} />);
    await ask("Посчитай суммы");
    const panel = (await screen.findByRole("heading", { name: "Не удалось выполнить анализ" })).closest("section")!;
    expect(panel).toHaveTextContent("Расчёт не удалось выполнить после 3 попыток.");
    expect(panel).toHaveTextContent("Попыток расчёта: 3 из 3");
    expect(within(panel).getAllByRole("button")).toHaveLength(1);
    expect(screen.queryByRole("region", { name: "Проверяемый расчёт" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Копировать код" })).not.toBeInTheDocument();
    expect(fake.writes).toEqual([]);
  });

  it("shows typed Qwen and sandbox failures as safe text with exactly one next action", async () => {
    const cases: [string, string, string, string | null][] = [
      ["QWEN_UNAVAILABLE", "Внутренний Qwen не вернул корректный ответ.", "Повторите вопрос через минуту.", "Повторить"],
      ["QWEN_INVALID_RESPONSE", "Внутренний Qwen вернул ответ в недопустимом формате.", "Сформулируйте вопрос точнее и отправьте его ещё раз.", "Изменить вопрос"],
      ["SANDBOX_UNAVAILABLE", "Изолированная песочница для расчётов сейчас недоступна, поэтому расчёт не выполнялся.", "Сообщите администратору FinLens, что песочница расчётов недоступна.", null],
      ["SANDBOX_BUSY", "Песочница расчётов занята другими анализами.", "Повторите вопрос через минуту.", "Повторить"],
      ["ANALYSIS_TIMEOUT", "Анализ занял больше допустимого времени и был остановлен.", "Выделите меньший диапазон и отправьте вопрос ещё раз.", "Повторить"],
      ["INTERNAL_ERROR", "Traceback (most recent call last): File \"/srv/app.py\"", "Повторите вопрос.", "Повторить"],
    ];
    for (const [code, message, nextAction, button] of cases) {
      mockApi({ analysis: (body) => json(failure(body.analysisId, code, message, nextAction)) });
      const { unmount } = render(<App pollIntervalMs={1} />);
      await ask(`Вопрос ${code}`);
      const panel = (await screen.findByRole("heading", { name: "Не удалось выполнить анализ" })).closest("section")!;
      expect(panel).not.toHaveTextContent(/Traceback|\/srv\//);
      expect(panel.querySelectorAll("p strong")).toHaveLength(1);
      expect(within(panel).queryAllByRole("button").map((element) => element.textContent)).toEqual(button ? [button] : []);
      unmount();
      vi.unstubAllGlobals();
    }
  });

  it("clears local history when the API session expired by TTL", async () => {
    let expired = false;
    mockApi({ plan: (body) => expired
      ? json(failure(body.analysisId, "SESSION_NOT_FOUND", "Сеанс FinLens завершён или истёк, история диалога очищена.", "Отправьте вопрос ещё раз: начнётся новый сеанс."), 404)
      : json(status(body.analysisId, "planned", { plan: { analysisId: body.analysisId, status: "planned", sheetsInWorkbook: 3, elapsedMs: 1, selections: [{ sheetName: "Debt", address: "B2:C4", reason: "" }] } }), 202) });
    render(<App pollIntervalMs={1} />);
    await chooseWorkbook();
    await ask("Первый вопрос");
    await screen.findByLabelText("Ответ аналитика");

    expired = true;
    await ask("Уточнение");
    await waitFor(() => expect(liveRegion()).toHaveTextContent("история диалога очищена. Следующий шаг: Отправьте вопрос ещё раз"));
    expect(screen.queryByRole("list", { name: "История текущего диалога" })).not.toBeInTheDocument();
  });
});
