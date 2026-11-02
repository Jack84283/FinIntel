import { invoke, isTauri } from "@tauri-apps/api/core";
import { INDEX_CODES, type IndexCode } from "./index-history-api";
import { ANALYTICS_WINDOWS, type AnalyticsWindow, type QualityPolicy } from "./index-intelligence-api";

export type BasketMetric = "normalized_100" | "simple_return" | "drawdown" |
  "rolling_volatility_annualized" | "relative_strength" | "rolling_correlation";
export type BasketQuery = {
  codes: IndexCode[]; start: string; end: string;
  window: AnalyticsWindow; qualityPolicy: QualityPolicy;
};
export type BasketValues = Record<BasketMetric, string | null>;
export type BasketPoint = { session_date: string; values: Record<string, BasketValues> };
export type BasketSource = {
  identity: { instrument_kind: string; code: string; provider: string; adjustment: string;
    return_semantics: string; frequency: string; units: string };
  source: { source_url: string | null; coverage_updated_at: string | null; snapshot_sha256: string };
  stored_sessions: number;
  excluded_nonshared_sessions: number;
};
export type BasketResult = {
  calculation_version: string; codes: string[]; baseline_code: string;
  request: { start: string; end: string; window_sessions: number };
  quality_policy: QualityPolicy;
  quality: { status: string; quality_verified: boolean; active_unresolved_findings?: number | null };
  unverified_data: boolean;
  alignment: "intersection"; common_sessions: number;
  first_common_session: string; last_common_session: string;
  series: BasketSource[]; points: BasketPoint[];
  read_only: true; no_network: true; no_synthetic_sessions: true; notes: string[];
};
const metrics: readonly BasketMetric[] = [
  "normalized_100", "simple_return", "drawdown", "rolling_volatility_annualized",
  "relative_strength", "rolling_correlation",
];
const validDate = (value: string) => {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const dt = new Date(value + "T00:00:00Z");
  return Number.isFinite(dt.valueOf()) && dt.toISOString().slice(0, 10) === value;
};
const asRecord = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw Error("Invalid basket analytics response.");
  return value as Record<string, unknown>;
};
const metricValue = (value: unknown, nullable: boolean) =>
  value === null ? nullable : typeof value === "string" && value.trim() !== "" &&
    Number.isFinite(Number(value));
export function validateBasketQuery(query: BasketQuery): void {
  if (query.codes.length < 2 || query.codes.length > 4 ||
    new Set(query.codes).size !== query.codes.length ||
    query.codes.some(code => code === "VIX" || !INDEX_CODES.includes(code)) ||
    !validDate(query.start) || !validDate(query.end) || query.start > query.end ||
    !ANALYTICS_WINDOWS.includes(query.window) ||
    !["require_complete", "allow_unverified"].includes(query.qualityPolicy)) {
    throw Error("Select 2–4 different price indices and valid dates.");
  }
}
/** Defense-in-depth: reject silent stale, mismatched, fabricated or non-finite observations. */
export function parseBasketResult(payload: unknown, query: BasketQuery): BasketResult {
  const result = asRecord(payload), request = asRecord(result.request), quality = asRecord(result.quality);
  if (!Array.isArray(result.codes) || result.codes.length !== query.codes.length ||
    result.codes.some((code, i) => code !== query.codes[i]) ||
    result.baseline_code !== query.codes[0] ||
    request.start !== query.start || request.end !== query.end || request.window_sessions !== query.window ||
    result.quality_policy !== query.qualityPolicy ||
    typeof quality.quality_verified !== "boolean" || typeof quality.status !== "string" ||
    result.unverified_data !== !quality.quality_verified ||
    result.alignment !== "intersection" ||
    typeof result.calculation_version !== "string" || !/^index-analytics-v\d+$/.test(result.calculation_version) ||
    result.read_only !== true || result.no_network !== true || result.no_synthetic_sessions !== true ||
    !Number.isSafeInteger(result.common_sessions) || (result.common_sessions as number) < 2 ||
    !Array.isArray(result.points) || result.points.length !== result.common_sessions ||
    result.points.length > 10000 || !Array.isArray(result.series) || result.series.length !== query.codes.length ||
    typeof result.first_common_session !== "string" || typeof result.last_common_session !== "string") {
    throw Error("Basket analytics response does not match the request.");
  }
  for (let index = 0; index < query.codes.length; index++) {
    const item = asRecord(result.series[index]), identity = asRecord(item.identity),
      source = asRecord(item.source);
    if (identity.code !== query.codes[index] || identity.provider !== "fred" ||
      identity.instrument_kind !== "index" || identity.units !== "index_points" ||
      identity.return_semantics !== "price_index" ||
      typeof source.snapshot_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(source.snapshot_sha256) ||
      !Number.isSafeInteger(item.stored_sessions) || !Number.isSafeInteger(item.excluded_nonshared_sessions) ||
      (item.stored_sessions as number) < result.common_sessions ||
      (item.excluded_nonshared_sessions as number) !== (item.stored_sessions as number) - (result.common_sessions as number)) {
      throw Error("Invalid multi-index source provenance.");
    }
  }
  let previous = "";
  for (const item of result.points) {
    const point = asRecord(item), values = asRecord(point.values);
    if (typeof point.session_date !== "string" || !validDate(point.session_date) ||
      point.session_date <= previous || point.session_date < query.start || point.session_date > query.end ||
      Object.keys(values).length !== query.codes.length) throw Error("Invalid common session.");
    previous = point.session_date;
    for (let index = 0; index < query.codes.length; index++) {
      const row = asRecord(values[query.codes[index]]);
      if (metrics.some(key => !metricValue(row[key], key !== "normalized_100" &&
        !(key === "relative_strength" && index > 0)))) {
        throw Error("Invalid multi-index calculated value.");
      }
      if (Number(row.normalized_100) <= 0 ||
        (index === 0 && row.relative_strength !== null) ||
        (index > 0 && Number(row.relative_strength) <= 0)) {
        throw Error("Invalid multi-index relative baseline.");
      }
    }
  }
  if (asRecord(result.points[0]).session_date !== result.first_common_session ||
    asRecord(result.points[result.points.length - 1]).session_date !== result.last_common_session ||
    !Array.isArray(result.notes)) throw Error("Invalid comparison coverage.");
  return payload as BasketResult;
}
export async function fetchIndexBasket(query: BasketQuery): Promise<BasketResult> {
  validateBasketQuery(query);
  let payload: unknown;
  if (isTauri()) {
    payload = await invoke("analytics_basket", {
      codes: query.codes, start: query.start, end: query.end,
      window: query.window, qualityPolicy: query.qualityPolicy,
    });
  } else {
    const params = new URLSearchParams({
      codes: query.codes.join(","), start: query.start, end: query.end,
      window: String(query.window), quality_policy: query.qualityPolicy,
    });
    const response = await fetch("/api/v1/analytics/basket?" + params, { cache: "no-store" });
    if (!response.ok) {
      const error: unknown = await response.json().catch(() => null);
      const detail = error && typeof error === "object" && "detail" in error &&
        typeof error.detail === "string" ? error.detail : "HTTP " + response.status;
      throw Error(detail);
    }
    payload = await response.json();
  }
  return parseBasketResult(payload, query);
}
