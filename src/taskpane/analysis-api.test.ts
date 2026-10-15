import { describe, expect, it, vi } from "vitest";
import { ApiError, cancelAnalysis, closeSession, createSession, getAnalysisStatus, planWorkbook, requestAnalysis, requestWorkbookAnalysis } from "./analysis-api";

const artifact = { sheetName: "P&L", address: "P&L!A1:B2", values: [[1, 2]], formulas: [["", ""]], numberFormats: [["General", "General"]] };
const request = { analysisId: "analysis_12345", scope: "range" as const, question: "What changed?", artifact };
const completed = { analysisId: "analysis_12345", status: "completed", elapsedMs: 4, narrative: { conclusion: "Answer", evidence: ["Cell A1"], risks: [], assumptions: [], nextCheck: "Review" }, sources: [{ sheetName: "P&L", address: "P&L!A1:B2" }] };
const status = (stage: string, extra: Record<string, unknown> = {}) => ({ analysisId: "analysis_12345", stage, computationAttempt: null, maxComputationAttempts: 3, elapsedMs: 4, plan: null, result: null, ...extra });
const done = (result: Record<string, unknown>) => status(String(result.status), { result });
const respond = (body: unknown, code = 200) => vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status: code }));
const invalid = /недопустимом формате/;

describe("analysis API client", () => {
  it("starts an analysis and accepts a typed running status from the same-origin API", async () => {
    const fetcher = respond(status("preparing_answer"), 202);
    const started = await requestAnalysis(request, fetcher);
    expect(started.stage).toBe("preparing_answer");
    expect(fetcher.mock.calls[0][1].body).toContain("P&L!A1:B2");
    // Same-origin relative path: served through the task pane origin, never a cross-origin API URL.
    expect(fetcher.mock.calls[0][0]).toBe("/api/analysis");
  });

  it("validates every lifecycle stage and the payload that belongs to it", async () => {
    for (const stage of ["selecting_artifacts", "preparing_answer", "running_computation", "retrying_computation", "finalizing", "cancelling"]) {
      await expect(getAnalysisStatus("analysis_12345", respond(status(stage, stage.includes("computation") ? { computationAttempt: 2 } : {})))).resolves.toMatchObject({ stage });
    }
    await expect(getAnalysisStatus("analysis_12345", respond(done(completed)))).resolves.toMatchObject({ result: { status: "completed" } });
    const broken = [
      status("thinking"),
      status("completed"),
      status("preparing_answer", { result: completed }),
      status("planned"),
      status("running_computation", { computationAttempt: 4 }),
      done({ ...completed, analysisId: "other_analysis" }),
      status("failed", { result: completed }),
      done({ status: "completed" }),
      done({ ...completed, narrative: { conclusion: "No evidence" } }),
      done({ ...completed, sources: [] }),
      done({ ...completed, narrative: { ...completed.narrative, insufficientData: true, missingData: [] } }),
      done({ ...completed, coverage: { sheetsInWorkbook: 3, sheetsUsed: ["P&L"] } }),
      done({ analysisId: "analysis_12345", status: "cancelled", elapsedMs: 1, message: "m", nextAction: "n", narrative: completed.narrative }),
      done({ analysisId: "analysis_12345", status: "failed", elapsedMs: 0, error: { code: "X" } }),
    ];
    for (const body of broken) {
      await expect(getAnalysisStatus("analysis_12345", respond(body))).rejects.toThrow(invalid);
    }
  });

  it("turns a request rejected by validation into a terminal failed status", async () => {
    const rejected = { analysisId: "unknown", status: "failed", elapsedMs: 0, error: { code: "REQUEST_INVALID", message: "Слишком большой запрос.", nextAction: "Выберите меньший диапазон." } };
    const result = await requestAnalysis(request, respond(rejected, 422));
    expect(result.stage).toBe("failed");
    expect(result.result?.error?.code).toBe("REQUEST_INVALID");
  });

  it("reports an unreachable network or a broken API as typed errors, never raw text", async () => {
    const offline = vi.fn().mockRejectedValue(new TypeError("Failed to fetch https://internal.example/secret"));
    const error = await requestAnalysis(request, offline).catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("NETWORK_UNAVAILABLE");
    expect((error as ApiError).message).not.toContain("internal.example");

    const proxyPage = vi.fn().mockResolvedValue(new Response("<html>502 Bad Gateway nginx /var/www</html>", { status: 502 }));
    const broken = await getAnalysisStatus("analysis_12345", proxyPage).catch((reason: unknown) => reason) as ApiError;
    expect(broken.code).toBe("API_UNAVAILABLE");
    expect(broken.message).not.toMatch(/nginx|var/);
  });

  it("propagates an abort instead of reporting a network error", async () => {
    const aborted = vi.fn().mockRejectedValue(new DOMException("aborted", "AbortError"));
    await expect(getAnalysisStatus("analysis_12345", aborted)).rejects.toMatchObject({ name: "AbortError" });
  });

  it("sends an explicit cancel request and accepts the cancelled result without any answer", async () => {
    const cancelled = { analysisId: "analysis_12345", status: "cancelled", elapsedMs: 7, message: "Анализ отменён.", nextAction: "Задайте новый вопрос." };
    const fetcher = respond(done(cancelled));
    await expect(cancelAnalysis("analysis_12345", fetcher)).resolves.toMatchObject({ stage: "cancelled", result: { status: "cancelled" } });
    expect(fetcher).toHaveBeenCalledWith("/api/analyses/analysis_12345/cancel", { method: "POST" });
    const poll = respond(status("cancelling"));
    await getAnalysisStatus("analysis_12345", poll);
    expect(poll.mock.calls[0][0]).toBe("/api/analyses/analysis_12345");
  });

  it("creates an opaque session and validates its schema", async () => {
    const session = await createSession("workbook", respond({ sessionId: "x".repeat(43), scope: "workbook", ttlSeconds: 1800 }, 201));
    expect(session.ttlSeconds).toBe(1800);
    await expect(createSession("workbook", respond({ sessionId: "Book1.xlsx", scope: "workbook", ttlSeconds: 1800 }, 201))).rejects.toThrow(invalid);
    await expect(createSession("workbook", respond({ status: "failed", error: { code: "SESSION_CAPACITY", message: "Занято.", nextAction: "Повторите позже." } }, 503))).rejects.toMatchObject({ code: "SESSION_CAPACITY", message: "Занято." });
  });

  it("validates the workbook plan carried by the planned stage", async () => {
    const plan = { analysisId: "analysis_12345", status: "planned", sheetsInWorkbook: 4, elapsedMs: 1, selections: [{ sheetName: "P&L", address: "A1:B2", reason: "" }] };
    await expect(planWorkbook("x".repeat(43), { analysisId: "analysis_12345", question: "Q" }, respond(status("planned", { plan })))).resolves.toMatchObject({ stage: "planned", plan: { selections: [{ address: "A1:B2" }] } });
    const tooMany = { ...plan, selections: Array.from({ length: 6 }, () => plan.selections[0]) };
    await expect(planWorkbook("x".repeat(43), { analysisId: "analysis_12345", question: "Q" }, respond(status("planned", { plan: tooMany })))).rejects.toThrow(invalid);
    const expired = done({ analysisId: "analysis_12345", status: "failed", elapsedMs: 0, error: { code: "SESSION_NOT_FOUND", message: "Истёк.", nextAction: "Повторите." } });
    await expect(planWorkbook("x".repeat(43), { analysisId: "analysis_12345", question: "Q" }, respond(expired, 404))).resolves.toMatchObject({ stage: "failed" });
  });

  it("posts phase-two artifacts to the session and closes sessions explicitly", async () => {
    const fetcher = respond(done({ ...completed, coverage: { sheetsInWorkbook: 4, sheetsWithData: 2, sheetsUsed: ["P&L"], complete: false } }));
    await requestWorkbookAnalysis("x".repeat(43), { analysisId: "analysis_12345", artifacts: [artifact] }, fetcher);
    expect(fetcher.mock.calls[0][0]).toMatch(/\/api\/sessions\/x{43}\/workbook\/analysis$/);

    const remover = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    closeSession("x".repeat(43), {}, remover);
    expect(remover).toHaveBeenCalledWith(expect.stringMatching(/\/api\/sessions\/x{43}$/), { method: "DELETE", keepalive: true });
  });

  it("accepts only well-formed execution artifacts and never artifacts on a failed result", async () => {
    const png = "iVBORw0KGgoAAAANSUhEUgAAAAQAAAADCAIAAAA7ljmRAAAAEklEQVR4nGP4";
    const computation = { status: "succeeded", code: "print(1)", method: "m", attempts: 2, inputs: [{ sheetName: "P&L", address: "A1" }], tables: [{ title: "T", columns: ["a"], rows: [[1], [null]], totalRows: 2, truncated: false }], charts: [{ title: "C", pngBase64: png, widthPx: 4, heightPx: 3, description: { metric: "m", period: "p", insight: "i" } }], stdout: "", durationMs: 1 };
    await expect(getAnalysisStatus("analysis_12345", respond(done({ ...completed, computation })))).resolves.toMatchObject({ result: { computation: { attempts: 2 } } });
    const broken = [
      { ...computation, status: "draft" },
      { ...computation, code: "" },
      { ...computation, attempts: 4 },
      { ...computation, tables: [{ ...computation.tables[0], rows: [[1, 2]] }] },
      { ...computation, tables: [{ ...computation.tables[0], rows: [[{ html: "<b>" }]] }] },
      { ...computation, charts: [{ ...computation.charts[0], pngBase64: "PHN2Zz4=" }] },
      { ...computation, charts: [{ ...computation.charts[0], pngBase64: "javascript:alert(1)" }] },
      { ...computation, charts: [{ ...computation.charts[0], description: { metric: "m", period: "", insight: "i" } }] },
    ];
    for (const item of broken) {
      await expect(getAnalysisStatus("analysis_12345", respond(done({ ...completed, computation: item })))).rejects.toThrow(invalid);
    }
    const failed = { analysisId: "analysis_12345", status: "failed", elapsedMs: 0, computationAttempts: 3, error: { code: "COMPUTATION_FAILED", message: "m", nextAction: "n" } };
    await expect(getAnalysisStatus("analysis_12345", respond(done(failed)))).resolves.toMatchObject({ result: { computationAttempts: 3 } });
    await expect(getAnalysisStatus("analysis_12345", respond(done({ ...failed, computation })))).rejects.toThrow(invalid);
    await expect(getAnalysisStatus("analysis_12345", respond(done({ ...failed, narrative: completed.narrative })))).rejects.toThrow(invalid);
  });
});
