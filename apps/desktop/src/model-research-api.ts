import { invoke, isTauri } from "@tauri-apps/api/core";
import { type IndexCode } from "./index-history-api";
import { type QualityPolicy } from "./index-intelligence-api";
import { canonicalModelSha256, verifyIndicatorAverages } from "./model-research-integrity";

export const BUILTIN_MODEL_ID = "builtin.sma_crossover";
export const BUILTIN_MODEL_VERSION = "1.0.0";

export type ModelResearchQuery = {
  code: IndexCode;
  start: string;
  end: string;
  qualityPolicy: QualityPolicy;
  fastWindow: number;
  slowWindow: number;
  expectedSnapshot?: string | null;
};
export type ModelResearchPoint = {
  session_date: string;
  close: string;
  fast_sma: string | null;
  slow_sma: string | null;
  signal_after_close: "long" | "flat" | null;
};
export type ModelResearchResult = {
  execution_contract: "model-research-evaluation-v1";
  lineage: {
    model_id: string;
    model_version: string;
    implementation_revision: string;
    parameters: { fast_window: number; slow_window: number };
    source_identity: {
      instrument_kind: "index"; code: string; provider: "fred";
      adjustment: "price_index"; return_semantics: "price_index";
      frequency: "daily"; units: "index_points";
    };
    source_snapshot_sha256: string;
    analysis_calculation_version: string;
    requested_start: string;
    requested_end: string;
    first_observed_session: string;
    last_observed_session: string;
    quality_policy: QualityPolicy;
    quality_status: string;
    quality_verified: boolean;
    point_in_time_membership: "not_used";
    dividends_included: false;
    corporate_action_adjustment: "none_index_price_level";
  };
  reproducibility_sha256: string;
  output_sha256: string;
  evaluated_at_utc: string;
  status: "completed";
  observations: number;
  points: ModelResearchPoint[];
  quality_warning: string | null;
  read_only: true;
  no_network: true;
  no_synthetic_sessions: true;
  signal_after_close_only: true;
  backtest_validated: false;
  persistent_job: false;
};
const isDate = (value: string): boolean => {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const day = new Date(value + "T00:00:00Z");
  return Number.isFinite(day.valueOf()) && day.toISOString().slice(0, 10) === value;
};
const hash64 = (value: unknown): value is string =>
  typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const record = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Invalid local model research response.");
  }
  return value as Record<string, unknown>;
};
const decimal = (value: unknown): value is string =>
  typeof value === "string" && /^\d+(?:\.\d+)?$/.test(value) &&
  Number.isFinite(Number(value)) && Number(value) > 0;
function compareDecimal(a: string, b: string): number {
  const parts = (v: string) => {
    const [rawWhole, rawFraction = ""] = v.split(".");
    return [rawWhole.replace(/^0+(?=\d)/, ""), rawFraction.replace(/0+$/, "")];
  };
  const left = parts(a), right = parts(b);
  if (left[0].length !== right[0].length) return left[0].length > right[0].length ? 1 : -1;
  if (left[0] !== right[0]) return left[0] > right[0] ? 1 : -1;
  const len = Math.max(left[1].length, right[1].length);
  const la = left[1].padEnd(len, "0"), rb = right[1].padEnd(len, "0");
  return la === rb ? 0 : la > rb ? 1 : -1;
}
export function validateModelResearchQuery(query: ModelResearchQuery): void {
  if (!["SP500", "DJIA", "NASDAQCOM", "NDX"].includes(query.code) ||
    !isDate(query.start) || !isDate(query.end) || query.start > query.end ||
    !["require_complete", "allow_unverified"].includes(query.qualityPolicy) ||
    !Number.isInteger(query.fastWindow) || query.fastWindow < 2 || query.fastWindow > 100 ||
    !Number.isInteger(query.slowWindow) || query.slowWindow < 3 || query.slowWindow > 252 ||
    query.fastWindow >= query.slowWindow ||
    (query.expectedSnapshot != null && !hash64(query.expectedSnapshot))) {
    throw new Error("Invalid local model parameters or source selection.");
  }
}
export async function parseModelResearchResult(payload: unknown, query: ModelResearchQuery): Promise<ModelResearchResult> {
  const value = record(payload), lineage = record(value.lineage);
  const identity = record(lineage.source_identity), params = record(lineage.parameters);
  if (value.execution_contract !== "model-research-evaluation-v1" ||
    value.status !== "completed" || value.read_only !== true ||
    value.no_network !== true || value.no_synthetic_sessions !== true ||
    value.signal_after_close_only !== true || value.backtest_validated !== false ||
    value.persistent_job !== false || !hash64(value.reproducibility_sha256) ||
    !hash64(value.output_sha256) || !hash64(lineage.source_snapshot_sha256) ||
    (query.expectedSnapshot && lineage.source_snapshot_sha256 !== query.expectedSnapshot) ||
    lineage.model_id !== BUILTIN_MODEL_ID || lineage.model_version !== BUILTIN_MODEL_VERSION ||
    typeof lineage.implementation_revision !== "string" || !lineage.implementation_revision ||
    lineage.requested_start !== query.start || lineage.requested_end !== query.end ||
    lineage.quality_policy !== query.qualityPolicy ||
    typeof lineage.quality_verified !== "boolean" ||
    (query.qualityPolicy === "require_complete" && lineage.quality_verified !== true) ||
    typeof lineage.quality_status !== "string" ||
    lineage.point_in_time_membership !== "not_used" ||
    lineage.dividends_included !== false ||
    lineage.corporate_action_adjustment !== "none_index_price_level" ||
    identity.instrument_kind !== "index" || identity.code !== query.code ||
    identity.provider !== "fred" || identity.adjustment !== "price_index" ||
    identity.return_semantics !== "price_index" || identity.units !== "index_points" ||
    identity.frequency !== "daily" ||
    params.fast_window !== query.fastWindow || params.slow_window !== query.slowWindow ||
    !Number.isSafeInteger(value.observations) ||
    (value.observations as number) < query.slowWindow + 1 ||
    (value.observations as number) > 2000 || !Array.isArray(value.points) ||
    value.points.length !== value.observations ||
    typeof value.evaluated_at_utc !== "string" ||
    typeof lineage.analysis_calculation_version !== "string" ||
    !isDate(lineage.first_observed_session as string) ||
    !isDate(lineage.last_observed_session as string) ||
    !((value.quality_warning === null && lineage.quality_verified === true) ||
      (typeof value.quality_warning === "string" && value.quality_warning.length > 0 &&
        lineage.quality_verified === false))) {
    throw new Error("Mismatched model version, input snapshot or execution contract.");
  }
  let previous = "";
  for (let index = 0; index < value.points.length; index++) {
    const point = record(value.points[index]);
    const day = point.session_date;
    if (typeof day !== "string" || !isDate(day) ||
      day < query.start || day > query.end ||
      (previous && day <= previous) ||
      !decimal(point.close) ||
      (point.fast_sma !== null && !decimal(point.fast_sma)) ||
      (point.slow_sma !== null && !decimal(point.slow_sma)) ||
      ![null, "long", "flat"].includes(point.signal_after_close as string | null) ||
      ((index + 1 < query.fastWindow) !== (point.fast_sma === null)) ||
      ((index + 1 < query.slowWindow) !== (point.slow_sma === null)) ||
      ((point.slow_sma === null) !== (point.signal_after_close === null)) ||
      (point.fast_sma !== null && point.slow_sma !== null &&
        point.signal_after_close !== (
          compareDecimal(point.fast_sma as string, point.slow_sma as string) > 0 ? "long" : "flat"
        ))) {
      throw new Error("Invalid chronological model signal evidence.");
    }
    previous = day;
  }
  const rows = value.points as ModelResearchPoint[];
  if (rows[0].session_date !== lineage.first_observed_session ||
    rows[rows.length - 1].session_date !== lineage.last_observed_session) {
    throw new Error("Model coverage does not match source lineage.");
  }
  // The backend-provided hashes are claims, not evidence until recomputed.
  // Independently check every SMA against actual trailing closes *before* display,
  // using Python's decimal.Context(prec=28) rounding semantics.
  verifyIndicatorAverages(rows, query.fastWindow, query.slowWindow);
  const [calculatedLineage, calculatedOutput] = await Promise.all([
    canonicalModelSha256(lineage),
    canonicalModelSha256(rows),
  ]);
  if (value.reproducibility_sha256 !== calculatedLineage ||
      value.output_sha256 !== calculatedOutput) {
    throw new Error("Local model result SHA-256 verification failed.");
  }
  return payload as ModelResearchResult;
}
export async function fetchModelResearch(query: ModelResearchQuery): Promise<ModelResearchResult> {
  validateModelResearchQuery(query);
  if (!isTauri()) {
    throw new Error("Local model evaluation requires the managed FinIntel desktop.");
  }
  const result: unknown = await invoke("model_research_evaluate", {
    code: query.code,
    start: query.start, end: query.end,
    qualityPolicy: query.qualityPolicy,
    fastWindow: query.fastWindow,
    slowWindow: query.slowWindow,
    expectedSnapshot: query.expectedSnapshot ?? null,
  });
  return parseModelResearchResult(result, query);
}
