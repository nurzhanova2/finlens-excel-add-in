import { ContextScope, MAX_SELECTED_ARTIFACTS, RangeSelection, TabularArtifact, WorkbookMap } from "./contracts";
import { MAX_EXECUTION_ATTEMPTS, SERVER_STAGES, ServerStage, TERMINAL_STAGES } from "./lifecycle";

export { MAX_EXECUTION_ATTEMPTS } from "./lifecycle";

export interface AnalysisRequest {
  analysisId: string;
  sessionId?: string;
  scope: "range" | "sheet";
  question: string;
  artifact: TabularArtifact;
}

export interface AnalysisError {
  code: string;
  message: string;
  nextAction: string;
}

export interface Source {
  sheetName: string;
  address: string;
}

export type TableCell = string | number | boolean | null;

export interface ResultTable {
  title: string;
  columns: string[];
  rows: TableCell[][];
  totalRows: number;
  truncated: boolean;
}

/** Text alternative of a chart produced by the executed code. */
export interface ChartDescription {
  metric: string;
  period: string;
  insight: string;
}

export interface ResultChart {
  title: string;
  pngBase64: string;
  widthPx: number;
  heightPx: number;
  description: ChartDescription;
}

/** Execution artifact: present only after a successful sandbox job and holds exactly the code that ran. */
export interface Computation {
  status: "succeeded";
  code: string;
  method: string;
  attempts: number;
  inputs: Source[];
  tables: ResultTable[];
  charts: ResultChart[];
  stdout: string;
  durationMs: number;
}

export interface Narrative {
  conclusion: string;
  evidence: string[];
  risks: string[];
  assumptions: string[];
  nextCheck: string;
  /** Story 3.2: the model flagged that the data does not support a well-founded conclusion. */
  insufficientData?: boolean;
  missingData?: string[];
}

export interface Coverage {
  sheetsInWorkbook: number;
  sheetsWithData: number;
  sheetsUsed: string[];
  /** True only when every sheet with data was read in full. */
  complete: boolean;
}

export interface AnalysisResult {
  analysisId: string;
  status: "completed" | "failed" | "cancelled";
  elapsedMs: number;
  narrative?: Narrative;
  sources?: Source[];
  coverage?: Coverage | null;
  computation?: Computation | null;
  computationAttempts?: number | null;
  error?: AnalysisError;
  /** Present on a cancelled result. */
  message?: string;
  nextAction?: string;
}

export interface Session {
  sessionId: string;
  scope: ContextScope;
  ttlSeconds: number;
}

export interface WorkbookPlanRequest {
  analysisId: string;
  question: string;
  /** Sent on the first question of a session; follow-ups reuse the map held by the API session. */
  workbookMap?: WorkbookMap;
}

export interface WorkbookPlan {
  analysisId: string;
  status: "planned";
  selections: RangeSelection[];
  sheetsInWorkbook: number;
  elapsedMs: number;
}

export interface WorkbookAnalysisRequest {
  analysisId: string;
  artifacts: TabularArtifact[];
}

/** Typed progress snapshot returned by every start, status and cancel request (Story 3.1). */
export interface AnalysisStatus {
  analysisId: string;
  stage: ServerStage;
  computationAttempt: number | null;
  maxComputationAttempts: number;
  elapsedMs: number;
  plan: WorkbookPlan | null;
  result: AnalysisResult | null;
}

/** Network or protocol failure: the task pane could not get a trustworthy answer from the API. */
export class ApiError extends Error {
  constructor(
    public readonly code: "NETWORK_UNAVAILABLE" | "API_UNAVAILABLE" | "STATUS_UNAVAILABLE" | "CANCEL_UNCONFIRMED" | "SESSION_CAPACITY",
    message: string,
    public readonly nextAction: string,
  ) {
    super(message);
  }
}

// Same-origin path: in dev the task pane origin proxies /api to the loopback API, so no CORS or mixed content.
export const API_BASE_PATH = "/api";
const RETRY_ACTION = "Проверьте подключение к корпоративной сети и нажмите «Повторить».";
const invalidFormat = () => new ApiError("API_UNAVAILABLE", "FinLens API вернул ответ в недопустимом формате.", "Нажмите «Повторить»; если ошибка повторяется, сообщите администратору FinLens.");

const isObject = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const isString = (value: unknown): value is string => typeof value === "string";
const isStringArray = (value: unknown): value is string[] => Array.isArray(value) && value.every(isString);
const isSource = (value: unknown): value is Source => isObject(value) && isString(value.sheetName) && isString(value.address);

function isError(value: unknown): value is AnalysisError {
  return isObject(value) && isString(value.code) && isString(value.message) && isString(value.nextAction);
}

function isNarrative(value: unknown): value is Narrative {
  if (!isObject(value) || !isString(value.conclusion) || !isStringArray(value.evidence) || !isStringArray(value.risks) || !isStringArray(value.assumptions) || !isString(value.nextCheck)) return false;
  if (value.insufficientData != null && typeof value.insufficientData !== "boolean") return false;
  if (value.missingData != null && !isStringArray(value.missingData)) return false;
  // Insufficient data must name what is missing; otherwise the answer is not shown as a conclusion.
  return !value.insufficientData || (isStringArray(value.missingData) && value.missingData.some((item) => item.trim()));
}

const isNonEmptyString = (value: unknown): value is string => isString(value) && value.trim().length > 0;
const isCount = (value: unknown, min: number, max = Number.MAX_SAFE_INTEGER): value is number => Number.isInteger(value) && (value as number) >= min && (value as number) <= max;
const isCell = (value: unknown) => value === null || isString(value) || typeof value === "boolean" || (typeof value === "number" && Number.isFinite(value));
const PNG_BASE64 = /^iVBORw0KGgo[A-Za-z0-9+/]+={0,2}$/;

function isTable(value: unknown): value is ResultTable {
  if (!isObject(value) || !isNonEmptyString(value.title) || !isStringArray(value.columns) || value.columns.length === 0 || !Array.isArray(value.rows)) return false;
  const width = value.columns.length;
  return value.rows.every((row) => Array.isArray(row) && row.length === width && row.every(isCell))
    && isCount(value.totalRows, value.rows.length) && typeof value.truncated === "boolean";
}

function isChart(value: unknown): value is ResultChart {
  if (!isObject(value) || !isNonEmptyString(value.title) || !isString(value.pngBase64) || !PNG_BASE64.test(value.pngBase64)) return false;
  const description = value.description;
  return isCount(value.widthPx, 1) && isCount(value.heightPx, 1) && isObject(description)
    && isNonEmptyString(description.metric) && isNonEmptyString(description.period) && isNonEmptyString(description.insight);
}

function isComputation(value: unknown): value is Computation {
  return isObject(value) && value.status === "succeeded" && isNonEmptyString(value.code) && isNonEmptyString(value.method)
    && isCount(value.attempts, 1, MAX_EXECUTION_ATTEMPTS) && Array.isArray(value.inputs) && value.inputs.length > 0 && value.inputs.every(isSource)
    && Array.isArray(value.tables) && value.tables.every(isTable) && Array.isArray(value.charts) && value.charts.every(isChart)
    && isString(value.stdout) && typeof value.durationMs === "number";
}

function isCoverage(value: unknown): value is Coverage {
  return isObject(value) && isCount(value.sheetsInWorkbook, 1) && isCount(value.sheetsWithData, 0) && isStringArray(value.sheetsUsed)
    && value.sheetsUsed.length > 0 && typeof value.complete === "boolean";
}

function isResult(value: unknown): value is AnalysisResult {
  if (!isObject(value) || !isString(value.analysisId) || typeof value.elapsedMs !== "number") return false;
  const noArtifacts = value.computation == null && value.narrative == null && value.sources == null;
  if (value.status === "failed") {
    // A failed analysis never carries execution artifacts or a narrative.
    return isError(value.error) && noArtifacts
      && (value.computationAttempts == null || isCount(value.computationAttempts, 1, MAX_EXECUTION_ATTEMPTS));
  }
  if (value.status === "cancelled") {
    // A cancelled analysis never returns anything it may have produced before the cancel.
    return isString(value.message) && isString(value.nextAction) && noArtifacts && value.error == null;
  }
  if (value.status !== "completed") return false;
  const coverageValid = value.coverage == null || isCoverage(value.coverage);
  const computationValid = value.computation == null || isComputation(value.computation);
  return isNarrative(value.narrative) && Array.isArray(value.sources) && value.sources.length > 0 && value.sources.every(isSource) && coverageValid && computationValid;
}

function isPlan(value: unknown): value is WorkbookPlan {
  return isObject(value) && value.status === "planned" && isString(value.analysisId) && typeof value.sheetsInWorkbook === "number"
    && Array.isArray(value.selections) && value.selections.length > 0 && value.selections.length <= MAX_SELECTED_ARTIFACTS
    && value.selections.every((selection) => isSource(selection) && isString((selection as RangeSelection).reason));
}

function isStatus(value: unknown): value is AnalysisStatus {
  if (!isObject(value) || !isString(value.analysisId) || !SERVER_STAGES.includes(value.stage as ServerStage) || typeof value.elapsedMs !== "number") return false;
  if (value.computationAttempt != null && !isCount(value.computationAttempt, 1, MAX_EXECUTION_ATTEMPTS)) return false;
  const stage = value.stage as ServerStage;
  if ((stage === "planned") !== (value.plan != null) || (value.plan != null && !isPlan(value.plan))) return false;
  if (TERMINAL_STAGES.has(stage)) {
    return isResult(value.result) && value.result.status === stage && value.result.analysisId === value.analysisId;
  }
  return value.result == null;
}

function isSession(value: unknown): value is Session {
  return isObject(value) && isString(value.sessionId) && /^[A-Za-z0-9_-]{32,128}$/.test(value.sessionId) && typeof value.ttlSeconds === "number"
    && (value.scope === "range" || value.scope === "sheet" || value.scope === "workbook");
}

async function send(path: string, init: RequestInit, fetcher: typeof fetch): Promise<{ response: Response; body: unknown }> {
  let response: Response;
  try {
    response = await fetcher(`${API_BASE_PATH}${path}`, init);
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError("NETWORK_UNAVAILABLE", "Нет связи с FinLens API.", RETRY_ACTION);
  }
  try {
    return { response, body: await response.json() };
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    // A proxy error page or an unhandled server failure: never show its raw text.
    throw response.ok ? invalidFormat() : new ApiError("API_UNAVAILABLE", "FinLens API временно недоступен.", RETRY_ACTION);
  }
}

const postJson = (path: string, payload: unknown, fetcher: typeof fetch, signal?: AbortSignal) =>
  send(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), signal }, fetcher);

/** Every analysis endpoint answers with a typed status; a request rejected by validation is a terminal failed status. */
function asStatus(response: Response, body: unknown): AnalysisStatus {
  if (isStatus(body)) return body;
  if (isResult(body) && body.status === "failed") {
    return { analysisId: body.analysisId, stage: "failed", computationAttempt: null, maxComputationAttempts: MAX_EXECUTION_ATTEMPTS, elapsedMs: body.elapsedMs, plan: null, result: body };
  }
  throw response.ok ? invalidFormat() : new ApiError("API_UNAVAILABLE", "FinLens API временно недоступен.", RETRY_ACTION);
}

export async function requestAnalysis(request: AnalysisRequest, fetcher: typeof fetch = fetch, signal?: AbortSignal): Promise<AnalysisStatus> {
  const { response, body } = await postJson("/analysis", request, fetcher, signal);
  return asStatus(response, body);
}

export async function planWorkbook(sessionId: string, request: WorkbookPlanRequest, fetcher: typeof fetch = fetch, signal?: AbortSignal): Promise<AnalysisStatus> {
  const { response, body } = await postJson(`/sessions/${encodeURIComponent(sessionId)}/workbook/plan`, request, fetcher, signal);
  return asStatus(response, body);
}

export async function requestWorkbookAnalysis(sessionId: string, request: WorkbookAnalysisRequest, fetcher: typeof fetch = fetch, signal?: AbortSignal): Promise<AnalysisStatus> {
  const { response, body } = await postJson(`/sessions/${encodeURIComponent(sessionId)}/workbook/analysis`, request, fetcher, signal);
  return asStatus(response, body);
}

export async function getAnalysisStatus(analysisId: string, fetcher: typeof fetch = fetch, signal?: AbortSignal): Promise<AnalysisStatus> {
  const { response, body } = await send(`/analyses/${encodeURIComponent(analysisId)}`, { method: "GET", signal }, fetcher);
  return asStatus(response, body);
}

/** Explicit server-side cancel: the API stops Qwen, terminates the sandbox job and reports `cancelled` after cleanup. */
export async function cancelAnalysis(analysisId: string, fetcher: typeof fetch = fetch): Promise<AnalysisStatus> {
  const { response, body } = await send(`/analyses/${encodeURIComponent(analysisId)}/cancel`, { method: "POST" }, fetcher);
  return asStatus(response, body);
}

export async function createSession(scope: ContextScope, fetcher: typeof fetch = fetch, signal?: AbortSignal): Promise<Session> {
  const { response, body } = await postJson("/sessions", { scope }, fetcher, signal);
  if (!response.ok) {
    const error = isObject(body) && isError(body.error) ? body.error : null;
    if (error?.code === "SESSION_CAPACITY") throw new ApiError("SESSION_CAPACITY", error.message, error.nextAction);
    throw new ApiError("API_UNAVAILABLE", "Не удалось начать сеанс FinLens.", RETRY_ACTION);
  }
  if (!isSession(body)) {
    throw invalidFormat();
  }
  return body;
}

/** Best-effort cleanup: on unload a beacon is used because pending fetches may be aborted. */
export function closeSession(sessionId: string, options: { onUnload?: boolean } = {}, fetcher: typeof fetch = fetch): void {
  const path = `${API_BASE_PATH}/sessions/${encodeURIComponent(sessionId)}`;
  if (options.onUnload && typeof navigator.sendBeacon === "function" && navigator.sendBeacon(`${path}/close`)) {
    return;
  }
  void fetcher(path, { method: "DELETE", keepalive: true }).catch(() => undefined);
}
