import { describe, expect, it, vi } from "vitest";
import { createFakeExcel } from "../test/fake-excel";
import { AnalysisRun, RunSession } from "./analysis-run";
import { Progress } from "./lifecycle";

const ID = "a".repeat(32);
const SESSION = "s".repeat(43);
const narrative = { conclusion: "Вывод", evidence: ["Факт"], risks: [], assumptions: [], nextCheck: "Проверьте." };
const completed = { analysisId: ID, status: "completed", elapsedMs: 9, narrative, sources: [{ sheetName: "P&L", address: "'P&L'!A1:C3" }] };
const cancelled = { analysisId: ID, status: "cancelled", elapsedMs: 9, message: "Анализ отменён. Временные данные запроса удалены.", nextAction: "Задайте новый вопрос." };
const status = (stage: string, extra: Record<string, unknown> = {}) => ({ analysisId: ID, stage, computationAttempt: null, maxComputationAttempts: 3, elapsedMs: 5, plan: null, result: null, ...extra });
const done = (result: Record<string, unknown>) => status(String(result.status), { result });
const json = (body: unknown, code = 200) => new Response(JSON.stringify(body), { status: code });
const plan = { analysisId: ID, status: "planned", sheetsInWorkbook: 2, elapsedMs: 1, selections: [{ sheetName: "P&L", address: "A1:C3", reason: "" }] };

type Handler = (url: string, init: RequestInit) => Response | Promise<Response>;

/** A scripted API: start, status polls (in order) and cancel; records every request. */
function api(handlers: { start?: Handler; polls?: (Handler | Record<string, unknown>)[]; cancel?: Handler }) {
  const requests: { url: string; method: string }[] = [];
  const polls = [...(handlers.polls ?? [])];
  const fetcher = vi.fn(async (url: string, init: RequestInit = {}) => {
    requests.push({ url, method: init.method ?? "GET" });
    if (url.endsWith("/cancel")) return (handlers.cancel ?? (() => json(done(cancelled))))(url, init);
    if (init.method === "GET") {
      const next = polls.length > 1 ? polls.shift()! : polls[0];
      if (!next) throw new Error("unexpected poll");
      return typeof next === "function" ? next(url, init) : json(next);
    }
    return (handlers.start ?? (() => json(status("preparing_answer"), 202)))(url, init);
  });
  return { fetcher: fetcher as unknown as typeof fetch, requests };
}

function setup(scope: "range" | "sheet" | "workbook", fetcher: typeof fetch, options: { blank?: boolean; session?: RunSession } = {}) {
  const progress: Progress[] = [];
  const fake = createFakeExcel([{ name: "P&L", usedRange: "A1:C3" }, { name: "Notes", usedRange: null }], { blank: options.blank });
  const session = options.session ?? { id: SESSION, scope, hasWorkbookMap: false };
  const run = new AnalysisRun({ analysisId: ID, scope, question: "Что изменилось?", ensureSession: async () => session, onProgress: (next) => progress.push(next), fetcher, excel: fake.excel, pollIntervalMs: 1 });
  return { run, progress, fake, session };
}

const stages = (progress: Progress[]) => progress.map((item) => item.stage);
const gate = () => {
  let release!: () => void;
  const promise = new Promise<void>((resolve) => { release = resolve; });
  return { promise, release };
};

describe("analysis run lifecycle", () => {
  it("reports every client and server stage of a workbook analysis with a computation retry", async () => {
    let started = 0;
    const { fetcher } = api({
      start: () => (started++ === 0 ? json(status("selecting_artifacts"), 202) : json(status("preparing_answer"), 202)),
      polls: [
        status("planned", { plan }),
        status("running_computation", { computationAttempt: 1 }),
        status("retrying_computation", { computationAttempt: 2 }),
        status("finalizing"),
        done({ ...completed, coverage: { sheetsInWorkbook: 2, sheetsWithData: 1, sheetsUsed: ["P&L"], complete: true } }),
      ],
    });
    const { run, progress, fake, session } = setup("workbook", fetcher);
    const outcome = await run.run();
    expect(outcome.kind).toBe("completed");
    expect(stages(progress)).toEqual(["mapping_workbook", "selecting_artifacts", "selecting_artifacts", "reading_context", "preparing_answer", "preparing_answer", "running_computation", "retrying_computation", "finalizing"]);
    expect(progress.find((item) => item.stage === "reading_context")?.detail).toBe("1 из карты книги — P&L!A1:C3");
    expect(progress.find((item) => item.stage === "retrying_computation")?.computationAttempt).toBe(2);
    expect(fake.requestedRanges).toEqual(["P&L!A1:C3"]);
    expect(session.hasWorkbookMap).toBe(true);
    expect(fake.writes).toEqual([]);
  });

  it("reads the selection, then follows the server until completed", async () => {
    const { fetcher, requests } = api({ polls: [done(completed)] });
    const { run, progress } = setup("range", fetcher);
    await expect(run.run()).resolves.toMatchObject({ kind: "completed", cancelTooLate: false });
    expect(stages(progress)).toEqual(["reading_context", "preparing_answer", "preparing_answer"]);
    expect(requests.map((request) => `${request.method} ${request.url}`)).toEqual(["POST /api/analysis", `GET /api/analyses/${ID}`]);
  });

  for (const stage of ["preparing_answer", "running_computation", "retrying_computation"]) {
    it(`cancels for real during ${stage}: explicit cancel request, confirmed cleanup, cancelled outcome`, async () => {
      const { fetcher, requests } = api({
        start: () => json(status(stage, stage === "preparing_answer" ? {} : { computationAttempt: stage === "running_computation" ? 1 : 2 }), 202),
        polls: [status(stage, stage === "preparing_answer" ? {} : { computationAttempt: stage === "running_computation" ? 1 : 2 })],
        cancel: () => json(done(cancelled)),
      });
      const { run, progress } = setup("range", fetcher);
      const running = run.run();
      await vi.waitFor(() => expect(stages(progress)).toContain(stage));
      const confirmation = run.cancel();
      expect(progress.at(-1)?.stage).toBe("cancelling");
      await expect(confirmation).resolves.toMatchObject({ kind: "cancelled", nextAction: "Задайте новый вопрос." });
      await expect(running).resolves.toMatchObject({ kind: "cancelled" });
      expect(requests.filter((request) => request.url.endsWith("/cancel"))).toEqual([{ url: `/api/analyses/${ID}/cancel`, method: "POST" }]);
      const polls = requests.filter((request) => request.method === "GET").length;
      await new Promise((resolve) => setTimeout(resolve, 20));
      expect(requests.filter((request) => request.method === "GET").length).toBe(polls);
      expect(progress.at(-1)?.stage).toBe("cancelling");
    });
  }

  it("follows the API until cleanup finished when the cancel answer is still `cancelling`", async () => {
    const { fetcher } = api({ polls: [status("running_computation", { computationAttempt: 1 }), status("cancelling"), done(cancelled)], cancel: () => json(status("cancelling")) });
    const { run, progress } = setup("range", fetcher);
    const running = run.run();
    await vi.waitFor(() => expect(stages(progress)).toContain("preparing_answer"));
    void run.cancel();
    await expect(running).resolves.toMatchObject({ kind: "cancelled" });
  });

  it("never shows a ready answer as success once the API confirmed the cancel", async () => {
    const answer = gate();
    const { fetcher } = api({ polls: [async () => { await answer.promise; return json(done(completed)); }], cancel: () => json(done(cancelled)) });
    const { run } = setup("range", fetcher);
    const running = run.run();
    await new Promise((resolve) => setTimeout(resolve, 20));
    const confirmation = run.cancel();
    answer.release(); // the completed status arrives just after the cancel click
    await expect(running).resolves.toMatchObject({ kind: "cancelled" });
    await expect(confirmation).resolves.toMatchObject({ kind: "cancelled" });
  });

  it("shows the answer honestly when the API had already committed it before the cancel", async () => {
    const { fetcher } = api({ polls: [status("finalizing")], cancel: () => json(done(completed)) });
    const { run, progress } = setup("range", fetcher);
    const running = run.run();
    await vi.waitFor(() => expect(stages(progress)).toContain("preparing_answer"));
    void run.cancel();
    await expect(running).resolves.toMatchObject({ kind: "completed", cancelTooLate: true });
  });

  it("cancels before anything reached the API and prevents a late start", async () => {
    const { fetcher, requests } = api({});
    const { run } = setup("range", fetcher);
    const running = run.run();
    const confirmation = run.cancel(); // still reading Excel
    await expect(running).resolves.toMatchObject({ kind: "cancelled" });
    await confirmation;
    expect(requests.map((request) => request.url)).toEqual([`/api/analyses/${ID}/cancel`]);
  });

  it("replaces lost progress with a retry error and stops the server analysis", async () => {
    const offline = () => { throw new TypeError("Failed to fetch"); };
    const { fetcher, requests } = api({ polls: [offline], cancel: () => json(done(cancelled)) });
    const { run } = setup("range", fetcher);
    const outcome = await run.run();
    expect(outcome).toMatchObject({ kind: "failed", issue: { code: "STATUS_UNAVAILABLE", action: "retry" } });
    expect(requests.filter((request) => request.method === "GET")).toHaveLength(3);
    await vi.waitFor(() => expect(requests.some((request) => request.url.endsWith("/cancel"))).toBe(true));
  });

  it("tolerates a single lost status response", async () => {
    let first = true;
    const flaky = () => { if (first) { first = false; throw new TypeError("Failed to fetch"); } return json(done(completed)); };
    const { fetcher } = api({ polls: [flaky] });
    await expect(setup("range", fetcher).run.run()).resolves.toMatchObject({ kind: "completed" });
  });

  it("reports an unconfirmed cancel when the API cannot be reached", async () => {
    const { fetcher } = api({ polls: [status("preparing_answer")], cancel: () => { throw new TypeError("Failed to fetch"); } });
    const { run, progress } = setup("range", fetcher);
    const running = run.run();
    await vi.waitFor(() => expect(stages(progress)).toContain("preparing_answer"));
    void run.cancel();
    await expect(running).resolves.toMatchObject({ kind: "failed", issue: { code: "CANCEL_UNCONFIRMED", action: "retry" } });
  });

  it("does not send an all-blank context to the API", async () => {
    const { fetcher, requests } = api({});
    const outcome = await setup("range", fetcher, { blank: true }).run.run();
    expect(outcome).toMatchObject({ kind: "failed", issue: { code: "EMPTY_CONTEXT", action: "choose_context" } });
    expect(requests).toEqual([]);
  });

  it("maps typed API failures to one action and flags session resets", async () => {
    const { fetcher } = api({ start: () => json(done({ analysisId: ID, status: "failed", elapsedMs: 0, error: { code: "SESSION_NOT_FOUND", message: "Сеанс истёк.", nextAction: "Отправьте вопрос ещё раз." } }), 404) });
    await expect(setup("range", fetcher).run.run()).resolves.toMatchObject({ kind: "failed", resetSession: true, issue: { code: "SESSION_NOT_FOUND", action: "retry" } });
  });
});
