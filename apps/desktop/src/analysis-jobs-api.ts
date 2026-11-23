/** Typed, native-only transport for the six managed Analysis Library models. */
import { invoke, isTauri } from "@tauri-apps/api/core";
import contracts from "../../../contracts/analysis-jobs.json";
import { canonicalModelSha256 } from "./model-research-integrity";

type ModelContracts = {
  "builtin.ema_trend": {
    revision: "ema-sma-seed-decimal-v1";
    parameters: { fast_window: number; slow_window: number };
    point: ClosePoint & {
      fast_ema: Decimal | null; slow_ema: Decimal | null; ema_difference: Decimal | null;
      trend_after_close: "above" | "flat" | "below" | null;
      crossover_after_close: "above" | "none" | "below" | null;
      warmup_complete: boolean;
    };
  };
  "builtin.momentum": {
    revision: "momentum-trailing-observed-v1";
    parameters: { lookback_sessions: 21 | 63 | 126 | 252 };
    point: ClosePoint & {
      comparison_session_date: string | null; comparison_close: Decimal | null;
      momentum: Decimal | null; lookback_sessions: 21 | 63 | 126 | 252;
      warmup_complete: boolean;
    };
  };
  "builtin.relative_strength": {
    revision: "relative-strength-common-baseline-v1";
    parameters: Record<string, never>;
    point: { session_date: string; benchmark: Lowercase<AnalysisSourceCode> } &
      Record<`${Lowercase<AnalysisSourceCode>}_${"normalized_100" | "relative_strength" | "return_difference"}`,
        Decimal | null>;
  };
  "builtin.rsi_state": {
    revision: "rsi-simple-rolling-states-v1";
    parameters: { window: number; lower_threshold: number; upper_threshold: number };
    point: ClosePoint & {
      rsi: Decimal | null; rsi_state: "low" | "neutral" | "high" | null;
      lower_threshold: number; upper_threshold: number; warmup_complete: boolean;
    };
  };
  "builtin.risk_profile": {
    revision: "historical-risk-linear95-decimal-v1";
    parameters: { risk_window: number; tail_window: number };
    point: ClosePoint & {
      simple_return: Decimal | null; comparison_return: Decimal | null;
      annualized_volatility: Decimal | null; downside_deviation: Decimal | null;
      var_95: Decimal | null; expected_shortfall_95: Decimal | null;
      correlation: Decimal | null; current_drawdown: Decimal; maximum_drawdown: Decimal;
      open_peak_date: string | null; open_trough_date: string | null;
      maximum_peak_date: string | null; maximum_trough_date: string | null;
      last_recovered_peak_date: string | null; last_recovered_trough_date: string | null;
      last_recovery_date: string | null; open_elapsed_intervals: number | null;
      last_peak_to_recovery_intervals: number | null; last_trough_to_recovery_intervals: number | null;
      historical_sample_count: number; historical_tail_count: number | null;
      historical_tail_status: "insufficient_history" | "empty_tail" | "available";
    };
  };
  "builtin.market_regime": {
    revision: "market-regime-two-axis-v1";
    parameters: {
      short_window: number; long_window: number; neutrality_band: Decimal;
      volatility_window: number; calibration_window: number;
    };
    point: ClosePoint & {
      sma50: Decimal | null; sma200: Decimal | null; close_distance: Decimal | null;
      sma_distance: Decimal | null; trend_state: "uptrend" | "downtrend" | "sideways" | "unknown";
      realized_volatility: Decimal | null; volatility_p25: Decimal | null;
      volatility_p75: Decimal | null; volatility_calibration_count: number;
      volatility_state: "low" | "normal" | "high" | "unknown";
      reason_codes: readonly RegimeReasonCode[];
    };
  };
};
type Decimal = string;
type ClosePoint = { session_date: string; close: Decimal };
type RegimeReasonCode = "trend_above_band" | "trend_below_band" | "trend_mixed" |
  "trend_insufficient_history" | "vol_below_p25" | "vol_above_p75" | "vol_normal_range" |
  "vol_calibration_insufficient" | "quality_unverified";

export type AnalysisModelId = keyof ModelContracts;
export type AnalysisParameters<M extends AnalysisModelId> = ModelContracts[M]["parameters"];
export type AnalysisPoint<M extends AnalysisModelId> = ModelContracts[M]["point"];
export type AnalysisSourceCode = "SP500" | "DJIA" | "NASDAQCOM" | "NDX";
export type AnalysisSourceIdentity = {
  instrument_kind: "index"; code: AnalysisSourceCode; provider: "fred";
  adjustment: "price_index"; return_semantics: "price_index"; frequency: "daily"; units: "index_points";
};
export type AnalysisSourcePin = { identity: AnalysisSourceIdentity; snapshot_sha256: string };
export type AnalysisWindow = { start: string; end: string };
export type AnalysisQualityPolicy = "require_complete" | "allow_unverified";
export type AnalysisQualityStatus = "verified" | "not_run" | "partial" | "stale" | "blocked" | "failed";

type ModelIdentity<M extends AnalysisModelId> = {
  model_id: M;
  model_version: "1.0.0";
  implementation_revision: ModelContracts[M]["revision"];
};
type RequestData = {
  contract_version: "analysis-model-request-v1";
  sources: readonly AnalysisSourcePin[];
  window: AnalysisWindow;
  quality_policy: AnalysisQualityPolicy;
};
type StoredRequest<M extends AnalysisModelId> = RequestData &
  Omit<ModelIdentity<M>, "implementation_revision"> & { parameters: AnalysisParameters<M> };
export type AnalysisJobRequest = {
  [M in AnalysisModelId]: RequestData & ModelIdentity<M> & { parameters: Partial<AnalysisParameters<M>> };
}[AnalysisModelId];

export type AnalysisSourceEvidence = AnalysisSourcePin & {
  observed: AnalysisWindow & { count: number };
  aligned_count: number;
  excluded_count: number;
  quality: { status: AnalysisQualityStatus; warnings: readonly string[] };
};
export type AnalysisOutputField = {
  name: string;
  kind: "date" | "decimal" | "integer" | "boolean" | "enum" | "reason_codes";
  units: string;
  nullable: boolean;
  strictly_positive: boolean;
  allowed_values: readonly string[];
};
export type AnalysisEvidence<M extends AnalysisModelId> = {
  contract_version: "analysis-model-evidence-v1";
  model: ModelIdentity<M>;
  calculation_version: string;
  sources: readonly AnalysisSourceEvidence[];
  requested: AnalysisWindow;
  observed_sessions: readonly string[];
  parameters: AnalysisParameters<M>;
  output_fields: readonly AnalysisOutputField[];
  quality_policy: AnalysisQualityPolicy;
  warnings: readonly string[];
  research_only: true;
};
export type AnalysisResult<M extends AnalysisModelId = AnalysisModelId> = {
  [K in M]: {
    contract_version: "analysis-model-result-v1";
    evidence: AnalysisEvidence<K>;
    points: readonly AnalysisPoint<K>[];
    reproducibility_sha256: string;
    output_sha256: string;
    evaluated_at_utc: string;
  };
}[M];

export const states = ["queued", "running", "completed", "failed", "cancelled"] as const;
export const statuses = [...states, "cancel_requested"] as const;
export const workerCodes = [
  "interrupted", "invalid_input", "quality_blocked", "source_changed",
  "storage_unavailable", "resource_limit", "worker_failure",
] as const;
export const transportCodes = [
  "managed_required", "invalid_input", "queue_full", "job_conflict", "source_changed",
  "job_not_found", "quality_blocked", "resource_limit", "storage_unavailable",
  "invalid_response", "backend_unavailable",
] as const;

export type AnalysisJobState = typeof states[number];
export type AnalysisJobStatus = typeof statuses[number];
export type AnalysisJobErrorCode = typeof transportCodes[number];
export type AnalysisWorkerErrorCode = typeof workerCodes[number];
export type AnalysisJobSummary<M extends AnalysisModelId = AnalysisModelId> = {
  [K in M]: ModelIdentity<K> & {
    id: string;
    state: AnalysisJobState;
    status: AnalysisJobStatus;
    request: StoredRequest<K>;
    request_sha256: string;
    attempts: number;
    progress: number;
    cancel_requested: 0 | 1;
    output_sha256: string | null;
    source_snapshot_sha256: string | null;
    error_code: AnalysisWorkerErrorCode | null;
    created_at: string;
    updated_at: string;
    started_at: string | null;
    finished_at: string | null;
  };
}[M];
export type AnalysisJobDetail = {
  [M in AnalysisModelId]: AnalysisJobSummary<M> & { result: AnalysisResult<M> | null };
}[AnalysisModelId];
export type AnalysisJobPage = { total: number; items: AnalysisJobSummary[] };
export type AnalysisJobFilter = "all" | AnalysisJobStatus;

export class AnalysisJobError extends Error {
  constructor(readonly code: AnalysisJobErrorCode) {
    super(code);
    this.name = "AnalysisJobError";
  }
}

type JsonObject = Record<string, unknown>;
type ParameterRule = {
  default: number | string;
  minimum?: number;
  maximum?: number;
  exclusiveMaximum?: number;
  enum?: number[];
  pattern?: string;
};
type RuntimeModel = {
  version: string;
  revision: string;
  calculation: string;
  min_sources: number;
  max_sources: number;
  allows_unverified: boolean;
  parameters: Record<string, ParameterRule>;
  output_fields: readonly AnalysisOutputField[];
};

const modelContracts = contracts as Record<AnalysisModelId, RuntimeModel>;
const requestKeys = [
  "contract_version", "model_id", "model_version", "sources", "window", "parameters", "quality_policy",
];
const summaryKeys = [
  "id", "state", "status", "model_id", "model_version", "implementation_revision", "request",
  "request_sha256", "attempts", "progress", "cancel_requested", "output_sha256", "source_snapshot_sha256",
  "error_code", "created_at", "updated_at", "started_at", "finished_at",
];

export function requireValid(value: unknown, code: AnalysisJobErrorCode = "invalid_response"): asserts value {
  if (!value) throw new AnalysisJobError(code);
}
export function object(value: unknown, keys?: readonly string[]): JsonObject {
  requireValid(value !== null && typeof value === "object" && !Array.isArray(value));
  const record = value as JsonObject;
  requireValid(!keys || (Object.keys(record).length === keys.length &&
    keys.every(key => Object.hasOwn(record, key))));
  return record;
}
export function contains<const T>(choices: readonly T[], value: unknown): value is T {
  return choices.some(choice => choice === value);
}
export function integer(value: unknown, min = Number.MIN_SAFE_INTEGER, max = Number.MAX_SAFE_INTEGER): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= min && value <= max;
}
export function hash(value: unknown): value is string {
  return typeof value === "string" && value.length === 64 && /^[0-9a-f]{64}$/.test(value);
}
export function identifier(value: unknown): value is string {
  return typeof value === "string" && value.length === 36 && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value);
}
export function day(value: unknown): value is string {
  if (typeof value !== "string" || !/^(?!0000)[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(value)) return false;
  const parsed = new Date(value + "T00:00:00Z");
  return Number.isFinite(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === value;
}
export function timestamp(value: unknown): boolean {
  if (typeof value !== "string") return false;
  const match = /^(.{10})T((?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9])(?:\.[0-9]{1,9})?(?:Z|\+00:00)$/.exec(value);
  return Boolean(match && match[0] === value && day(match[1]));
}

function sourceCode(value: unknown): AnalysisSourceCode {
  const source = object(value, [
    "instrument_kind", "code", "provider", "adjustment", "return_semantics", "frequency", "units",
  ]);
  requireValid(contains(["SP500", "DJIA", "NASDAQCOM", "NDX"], source.code) &&
    source.instrument_kind === "index" && source.provider === "fred" &&
    source.adjustment === "price_index" && source.return_semantics === "price_index" &&
    source.frequency === "daily" && source.units === "index_points");
  return source.code;
}

function normalizeParameters(id: AnalysisModelId, rules: RuntimeModel["parameters"], value: unknown): JsonObject {
  const input = object(value);
  requireValid(Object.keys(input).every(name => Object.hasOwn(rules, name)));
  const normalized: JsonObject = {};
  for (const [name, rule] of Object.entries(rules)) {
    const parameter = Object.hasOwn(input, name) ? input[name] : rule.default;
    requireValid(rule.pattern
      ? typeof parameter === "string" && parameter.trim() === parameter && new RegExp(rule.pattern).test(parameter)
      : integer(parameter, rule.minimum, rule.maximum) &&
        (rule.exclusiveMaximum === undefined || parameter < rule.exclusiveMaximum) &&
        (!rule.enum || rule.enum.includes(parameter)));
    normalized[name] = parameter;
  }
  if (id === "builtin.ema_trend") {
    requireValid(Number(normalized.fast_window) < Number(normalized.slow_window));
  }
  if (id === "builtin.rsi_state") {
    requireValid(Number(normalized.lower_threshold) < Number(normalized.upper_threshold));
  }
  if (id === "builtin.market_regime") {
    requireValid(Number(normalized.short_window) < Number(normalized.long_window) &&
      Number(normalized.volatility_window) + Number(normalized.calibration_window) < 2000);
  }
  return normalized;
}

function normalizeRequest(value: unknown, revision: unknown): StoredRequest<AnalysisModelId> {
  const request = object(value, requestKeys);
  requireValid(typeof request.model_id === "string" && Object.hasOwn(modelContracts, request.model_id));
  const id = request.model_id as AnalysisModelId;
  const model = modelContracts[id];
  requireValid(request.contract_version === "analysis-model-request-v1" &&
    request.model_version === model.version && revision === model.revision &&
    (request.quality_policy === "require_complete" ||
      (request.quality_policy === "allow_unverified" && model.allows_unverified)));
  const window = object(request.window, ["start", "end"]);
  requireValid(day(window.start) && day(window.end) && window.start <= window.end &&
    window.end <= new Date().toISOString().slice(0, 10));
  requireValid(Array.isArray(request.sources) && request.sources.length >= model.min_sources &&
    request.sources.length <= model.max_sources);
  const codes = new Set<AnalysisSourceCode>();
  for (const rawPin of request.sources) {
    const pin = object(rawPin, ["identity", "snapshot_sha256"]);
    const code = sourceCode(pin.identity);
    requireValid(!codes.has(code) && hash(pin.snapshot_sha256));
    codes.add(code);
  }
  const parameters = normalizeParameters(id, model.parameters, request.parameters);
  return { ...request, parameters } as StoredRequest<AnalysisModelId>;
}

async function parseSummary(value: unknown, id?: string, includeResult = false): Promise<AnalysisJobSummary> {
  const job = object(value, includeResult ? [...summaryKeys, "result"] : summaryKeys);
  requireValid(identifier(job.id) && (id === undefined || job.id === id) && contains(states, job.state) &&
    contains([0, 1], job.cancel_requested) &&
    job.status === (job.state === "running" && job.cancel_requested === 1 ? "cancel_requested" : job.state) &&
    integer(job.attempts, 0, 3) && integer(job.progress, 0, 100) &&
    (job.error_code === null || contains(workerCodes, job.error_code)) &&
    (job.state === "failed") === (job.error_code !== null) &&
    timestamp(job.created_at) && timestamp(job.updated_at) &&
    (job.started_at === null || timestamp(job.started_at)) &&
    (job.finished_at === null || timestamp(job.finished_at)));
  const original = normalizeRequest(job.request, job.implementation_revision);
  requireValid(job.model_id === original.model_id && job.model_version === original.model_version &&
    hash(job.request_sha256));
  const [originalHash, storedHash] = await Promise.all([
    canonicalModelSha256(original), canonicalModelSha256(job.request),
  ]);
  requireValid(originalHash === job.request_sha256 && storedHash === originalHash);
  if (job.state === "completed") {
    const snapshot = original.sources.length === 1 ? original.sources[0].snapshot_sha256 : null;
    requireValid(job.progress === 100 && hash(job.output_sha256) && job.source_snapshot_sha256 === snapshot);
  } else {
    requireValid(job.output_sha256 === null && job.source_snapshot_sha256 === null &&
      (!includeResult || job.result === null));
  }
  return job as unknown as AnalysisJobSummary;
}

function warnings(value: unknown): string[] {
  requireValid(Array.isArray(value) && value.every(code =>
    typeof code === "string" && code.trim() === code && /^[a-z0-9][a-z0-9._-]{0,127}$/.test(code)));
  return value;
}
function accepts(field: AnalysisOutputField, value: unknown): boolean {
  if (value === null) return field.nullable;
  switch (field.kind) {
    case "date": return day(value);
    case "decimal":
      return typeof value === "string" && value.trim() === value && /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(value) &&
        (!field.strictly_positive || (!value.startsWith("-") && /[1-9]/.test(value)));
    case "integer": return integer(value);
    case "boolean": return typeof value === "boolean";
    case "enum": return contains(field.allowed_values, value);
    case "reason_codes":
      return Array.isArray(value) && new Set(value).size === value.length &&
        value.every(code => contains(field.allowed_values, code));
  }
}

function minimumObservations(request: StoredRequest<AnalysisModelId>): number {
  const parameters = request.parameters as JsonObject;
  switch (request.model_id) {
    case "builtin.ema_trend": return Number(parameters.slow_window);
    case "builtin.momentum": return Number(parameters.lookback_sessions) + 1;
    case "builtin.relative_strength": return 2;
    default: return 1;
  }
}

function validateSource(
  value: unknown, pin: AnalysisSourcePin, request: StoredRequest<AnalysisModelId>,
  sessions: readonly string[], minimum: number,
): void {
  const source = object(value, ["identity", "snapshot_sha256", "observed", "aligned_count", "excluded_count", "quality"]);
  const observed = object(source.observed, ["start", "end", "count"]);
  const quality = object(source.quality, ["status", "warnings"]);
  const codes = warnings(quality.warnings);
  const first = sessions[0];
  const last = sessions[sessions.length - 1];
  requireValid(sourceCode(source.identity) === pin.identity.code && source.snapshot_sha256 === pin.snapshot_sha256 &&
    day(observed.start) && day(observed.end) && observed.start >= request.window.start &&
    observed.end <= request.window.end && observed.start <= first && observed.end >= last &&
    integer(observed.count, minimum, 2000) &&
    observed.count <= (Date.parse(observed.end) - Date.parse(observed.start)) / 86_400_000 + 1 &&
    source.aligned_count === sessions.length && integer(source.excluded_count, 0, 1999) &&
    sessions.length + source.excluded_count === observed.count &&
    sessions.length + Number(observed.start < first) + Number(last < observed.end) <= observed.count &&
    contains(["verified", "not_run", "partial", "stale", "blocked", "failed"], quality.status) &&
    (quality.status === "verified" || (request.quality_policy === "allow_unverified" && codes.length > 0)));
}

export async function parseAnalysisResult(value: unknown, job: AnalysisJobSummary): Promise<AnalysisResult> {
  const result = object(value, [
    "contract_version", "evidence", "points", "reproducibility_sha256", "output_sha256", "evaluated_at_utc",
  ]);
  const evidence = object(result.evidence, [
    "contract_version", "model", "calculation_version", "sources", "requested", "observed_sessions",
    "parameters", "output_fields", "quality_policy", "warnings", "research_only",
  ]);
  const identity = object(evidence.model, ["model_id", "model_version", "implementation_revision"]);
  const window = object(evidence.requested, ["start", "end"]);
  const request = job.request;
  const model = modelContracts[job.model_id];
  const codes = warnings(evidence.warnings);
  requireValid(result.contract_version === "analysis-model-result-v1" && timestamp(result.evaluated_at_utc) &&
    hash(result.reproducibility_sha256) && hash(result.output_sha256) && result.output_sha256 === job.output_sha256 &&
    evidence.contract_version === "analysis-model-evidence-v1" &&
    identity.model_id === job.model_id && identity.model_version === job.model_version &&
    identity.implementation_revision === job.implementation_revision &&
    evidence.calculation_version === model.calculation && evidence.research_only === true &&
    evidence.quality_policy === request.quality_policy && window.start === request.window.start &&
    window.end === request.window.end && (request.quality_policy !== "allow_unverified" || codes.includes("quality_override")) &&
    Array.isArray(evidence.sources) && evidence.sources.length === request.sources.length);
  const minimum = minimumObservations(request);
  const sessions = evidence.observed_sessions;
  const points = result.points;
  requireValid(Array.isArray(sessions) && sessions.length >= minimum && sessions.length <= 2000 &&
    sessions.every((session, index) => day(session) && (index === 0 || session > sessions[index - 1])) &&
    Array.isArray(points) && points.length === sessions.length);
  for (const [index, source] of evidence.sources.entries()) {
    validateSource(source, request.sources[index], request, sessions, minimum);
  }
  const fieldNames = model.output_fields.map(field => field.name);
  const parameters = request.parameters as JsonObject;
  const sourceCodes = request.sources.map(pin => pin.identity.code.toLowerCase());
  const relativeFields = job.model_id === "builtin.relative_strength"
    ? model.output_fields.filter(field => field.kind === "decimal")
      .map(field => ({ name: field.name, selected: sourceCodes.includes(field.name.split("_", 1)[0]) }))
    : [];
  const singleSourceRisk = job.model_id === "builtin.risk_profile" && sourceCodes.length === 1;
  for (const [index, rawPoint] of points.entries()) {
    const point = object(rawPoint, fieldNames);
    requireValid(point.session_date === sessions[index] &&
      model.output_fields.every(field => accepts(field, point[field.name])) &&
      (job.model_id !== "builtin.relative_strength" || point.benchmark === sourceCodes[0]) &&
      relativeFields.every(({ name, selected }) => (point[name] !== null) === selected) &&
      (!singleSourceRisk || (point.comparison_return === null && point.correlation === null)) &&
      (job.model_id !== "builtin.momentum" || point.lookback_sessions === parameters.lookback_sessions) &&
      (job.model_id !== "builtin.rsi_state" || (point.lower_threshold === parameters.lower_threshold &&
        point.upper_threshold === parameters.upper_threshold)));
  }
  const [fieldHash, expectedFieldHash, parameterHash, expectedParameterHash, evidenceHash, outputHash] =
    await Promise.all([
      canonicalModelSha256(evidence.output_fields), canonicalModelSha256(model.output_fields),
      canonicalModelSha256(evidence.parameters), canonicalModelSha256(parameters),
      canonicalModelSha256(evidence), canonicalModelSha256(points),
    ]);
  requireValid(fieldHash === expectedFieldHash && parameterHash === expectedParameterHash &&
    evidenceHash === result.reproducibility_sha256 && outputHash === result.output_sha256);
  return result as unknown as AnalysisResult;
}

type Command = "analysis_jobs_create" | "analysis_jobs_list" | "analysis_jobs_get" |
  "analysis_jobs_cancel" | "analysis_jobs_retry";

async function native(command: Command, args: JsonObject): Promise<unknown> {
  requireValid(isTauri(), "managed_required");
  try {
    return await invoke(command, args);
  } catch (error) {
    throw new AnalysisJobError(contains(transportCodes, error) ? error : "backend_unavailable");
  }
}

export async function queueAnalysisJob(request: AnalysisJobRequest): Promise<AnalysisJobSummary> {
  let snapshot: AnalysisJobRequest;
  let expected: StoredRequest<AnalysisModelId>;
  try {
    snapshot = structuredClone(request);
    const { implementation_revision: revision, ...original } = object(snapshot, [...requestKeys, "implementation_revision"]);
    expected = normalizeRequest(original, revision);
  } catch {
    throw new AnalysisJobError("invalid_input");
  }
  requireValid(isTauri(), "managed_required");
  const expectedHash = await canonicalModelSha256(expected);
  const job = await parseSummary(await native("analysis_jobs_create", { request: snapshot }));
  requireValid(job.request_sha256 === expectedHash &&
    job.implementation_revision === snapshot.implementation_revision && job.state === "queued" && job.attempts === 0);
  return job;
}

export async function listAnalysisJobs(page = 1, filter: AnalysisJobFilter = "all"): Promise<AnalysisJobPage> {
  requireValid(integer(page, 1, 1001) && (filter === "all" || contains(statuses, filter)), "invalid_input");
  const offset = (page - 1) * 10;
  const args = { limit: 10, offset, ...(filter === "all" ? {} : { status: filter }) };
  const response = object(await native("analysis_jobs_list", args), ["total", "items"]);
  requireValid(integer(response.total, 0) && Array.isArray(response.items) &&
    response.items.length === Math.min(10, Math.max(0, response.total - offset)));
  const items = await Promise.all(response.items.map(item => parseSummary(item)));
  requireValid(new Set(items.map(job => job.id)).size === items.length &&
    (filter === "all" || items.every(job => job.status === filter)));
  return { total: response.total, items };
}

export function getAnalysisJob(id: string, includeResult?: false): Promise<AnalysisJobSummary>;
export function getAnalysisJob(id: string, includeResult: true): Promise<AnalysisJobDetail>;
export async function getAnalysisJob(id: string, includeResult = false): Promise<AnalysisJobSummary | AnalysisJobDetail> {
  requireValid(identifier(id) && typeof includeResult === "boolean", "invalid_input");
  const response = await native("analysis_jobs_get", { jobId: id, includeResult });
  const job = await parseSummary(response, id, includeResult);
  if (!includeResult) return job;
  const result = job.state === "completed" ? await parseAnalysisResult(object(response).result, job) : null;
  // parseAnalysisResult verifies the model-specific binding to this summary.
  return { ...job, result } as AnalysisJobDetail;
}

export async function cancelAnalysisJob(id: string): Promise<{ id: string; status: AnalysisJobStatus }> {
  requireValid(identifier(id), "invalid_input");
  const response = object(await native("analysis_jobs_cancel", { jobId: id }), ["state"]);
  requireValid(contains(statuses, response.state));
  return { id, status: response.state };
}

export async function retryAnalysisJob(id: string): Promise<AnalysisJobSummary> {
  requireValid(identifier(id), "invalid_input");
  return parseSummary(await native("analysis_jobs_retry", { jobId: id }), id);
}
