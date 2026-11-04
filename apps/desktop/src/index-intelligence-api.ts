import { invoke, isTauri } from "@tauri-apps/api/core";
import { INDEX_CODES, type IndexCode } from "./index-history-api";

/** The v1 analytics contract is index-level and read-only; no chart invents prices. */
export type QualityPolicy = "require_complete" | "allow_unverified";
export type AnalyticsWindow = 5 | 10 | 14 | 20 | 21 | 50 | 63 | 126 | 200 | 252;
export const ANALYTICS_WINDOWS: readonly AnalyticsWindow[] = [5, 10, 14, 20, 21, 50, 63, 126, 200, 252];
export type AnalyticsQuery = {
  code: IndexCode;
  comparisonCode: IndexCode | null;
  start: string;
  end: string;
  window: AnalyticsWindow;
  qualityPolicy: QualityPolicy;
};
export type AnalyticsPoint = {
  session_date: string;
  close: string;
  normalized_100: string | null;
  simple_return: string | null;
  log_return: string | null;
  cumulative_return: string | null;
  drawdown: string | null;
  rolling_volatility_annualized: string | null;
  moving_average: string | null;
  momentum: string | null;
  rsi: string | null;
};
export type DrawdownEpisode = {
  status: "recovered" | "ongoing";
  peak_session_date: string;
  trough_session_date: string;
  recovery_session_date: string | null;
  trough_drawdown: string;
  underwater_observed_sessions: number;
  peak_to_recovery_observed_sessions: number | null;
  trough_to_recovery_observed_sessions: number | null;
};
export type ComparisonPoint = {
  session_date: string;
  left_normalized_100: string;
  right_normalized_100: string;
  left_return: string | null;
  right_return: string | null;
  relative_strength: string;
  rolling_correlation: string | null;
  rolling_covariance: string | null;
  rolling_beta: string | null;
  rolling_tracking_error_annualized: string | null;
};
export type AnalyticsIdentity = {
  instrument_kind: "index" | "equity";
  code: string;
  provider: string;
  adjustment: string;
  return_semantics: string;
  frequency: string;
  units: string;
};
export type AnalyticsResult = {
  calculation_version: string;
  identity: AnalyticsIdentity;
  settings: { window_sessions: number; annualization_sessions: number; missing_data_policy: string; numeric_boundary: string };
  observations: number;
  first_session: string | null;
  last_session: string | null;
  metrics_enabled: boolean;
  max_drawdown: string | null;
  longest_underwater_observed_sessions: number | null;
  longest_completed_recovery_observed_sessions: number | null;
  ongoing_drawdown_observed_sessions: number | null;
  drawdown_episodes: DrawdownEpisode[];
  period_returns: Record<string, { session_date: string; return: string; period: string }[]>;
  points: AnalyticsPoint[];
  request: { start: string; end: string };
  source: { source_url: string | null; coverage_updated_at: string | null; snapshot_sha256: string };
  quality: { status: string; quality_verified: boolean; active_unresolved_findings?: number | null; scan_complete_current?: boolean };
  quality_policy: QualityPolicy;
  unverified_data: boolean;
  read_only: true;
  no_network: true;
  no_synthetic_sessions: true;
  comparison?: {
    left: AnalyticsIdentity;
    right: AnalyticsIdentity;
    missing_data_policy: "strict" | "intersection";
    overlap_sessions: number;
    left_only_sessions: number;
    right_only_sessions: number;
    window_sessions: number;
    dividends_excluded: boolean;
    source: { source_url: string | null; coverage_updated_at: string | null; snapshot_sha256: string };
    points: ComparisonPoint[];
  } | null;
};
const validDate = (value: string): boolean => {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = new Date(value + "T00:00:00Z");
  return !Number.isNaN(date.valueOf()) && date.toISOString().slice(0, 10) === value;
};
export function validateAnalyticsQuery(query: AnalyticsQuery): void {
  if (!INDEX_CODES.includes(query.code) || (query.comparisonCode !== null && !INDEX_CODES.includes(query.comparisonCode)) ||
    query.code === query.comparisonCode || (query.comparisonCode !== null && (query.code === "VIX" || query.comparisonCode === "VIX")) ||
    !validDate(query.start) || !validDate(query.end) || query.start > query.end ||
    !ANALYTICS_WINDOWS.includes(query.window) ||
    !["require_complete", "allow_unverified"].includes(query.qualityPolicy)) {
    throw new Error("Invalid index analytics selection.");
  }
}
function record(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid local analytics response.");
  return value as Record<string, unknown>;
}
const scalar = (x: unknown): x is string | null => x === null || typeof x === "string";
const numeric = (x: unknown): boolean => x === null || (typeof x === "string" && x.trim() !== "" && Number.isFinite(Number(x)));
function validPoints(points: unknown, code: string, comparison = false): boolean {
  if (!Array.isArray(points) || points.length > 10000) return false;
  let last = "";
  for (const value of points) {
    const p = record(value);
    if (typeof p.session_date !== "string" || !validDate(p.session_date) || p.session_date <= last) return false;
    last = p.session_date;
    const fields = comparison ? ["left_normalized_100", "right_normalized_100", "relative_strength", "left_return",
      "right_return", "rolling_correlation", "rolling_covariance", "rolling_beta", "rolling_tracking_error_annualized"]
      : ["close", "normalized_100", "simple_return", "log_return", "cumulative_return", "drawdown",
        "rolling_volatility_annualized", "moving_average", "momentum", "rsi"];
    if (fields.some(key => !numeric(p[key]))) return false;
    if (comparison ? p.left_normalized_100 === null || p.right_normalized_100 === null
      : p.close === null || Number(p.close) <= 0 || (p.normalized_100 !== null && Number(p.normalized_100) <= 0)) return false;
  }
  return !!code;
}
/** Compare exact, positive Decimal close strings before any binary64 conversion.
 * Backend prices are formatted as plain decimals; using Number comparisons here
 * would collapse distinct high-precision peaks/troughs into the same float.
 */
function decimalClose(value: unknown): [string, string] | null {
  if (typeof value !== "string" || !/^\d+(?:\.\d+)?$/.test(value) ||
      !Number.isFinite(Number(value)) || Number(value) <= 0) return null;
  const [whole, fraction = ""] = value.split(".");
  return [whole.replace(/^0+(?=\d)/, ""), fraction.replace(/0+$/, "")];
}
function compareDecimalCloses(left: [string, string], right: [string, string]): number {
  if (left[0].length !== right[0].length) return left[0].length > right[0].length ? 1 : -1;
  if (left[0] !== right[0]) return left[0] > right[0] ? 1 : -1;
  const digits = Math.max(left[1].length, right[1].length);
  const a = left[1].padEnd(digits, "0"), b = right[1].padEnd(digits, "0");
  return a === b ? 0 : a > b ? 1 : -1;
}

/** Independently reconstruct every episode, including omitted or ongoing drawdowns. */
export function validRecoveryEpisodes(
  payload: unknown, rawPoints: unknown, metricsEnabled: boolean,
): boolean {
  if (!payload || typeof payload !== "object" || Array.isArray(payload) ||
      !Array.isArray(rawPoints)) return false;
  const data = payload as Record<string, unknown>;
  if (!Array.isArray(data.drawdown_episodes) ||
      data.drawdown_episodes.length > rawPoints.length) return false;
  if (!metricsEnabled) {
    return data.drawdown_episodes.length === 0 &&
      data.longest_completed_recovery_observed_sessions === null &&
      data.ongoing_drawdown_observed_sessions === null;
  }
  const points = rawPoints.map(record);
  const closes = points.map(p => decimalClose(p.close));
  if (closes.some(close => close === null)) return false;
  const windows: { peak: number; trough: number; recovery: number | null }[] = [];
  let peak = 0, trough = 0;
  for (let index = 1; index < closes.length; index++) {
    if (compareDecimalCloses(closes[index]!, closes[peak]!) >= 0) {
      if (trough > peak) windows.push({ peak, trough, recovery: index });
      peak = trough = index;
    } else if (trough === peak || compareDecimalCloses(closes[index]!, closes[trough]!) < 0) {
      trough = index;
    }
  }
  if (trough > peak) windows.push({ peak, trough, recovery: null });
  if (windows.length !== data.drawdown_episodes.length) return false;

  let longestCompleted: number | null = null;
  let ongoing: number | null = null;
  let longestUnderwater = 0;
  for (let i = 0; i < windows.length; i++) {
    const expected = windows[i];
    const episode = record(data.drawdown_episodes[i]);
    const { peak: high, trough: low, recovery } = expected;
    if (episode.status !== (recovery === null ? "ongoing" : "recovered") ||
        episode.peak_session_date !== points[high].session_date ||
        episode.trough_session_date !== points[low].session_date ||
        episode.recovery_session_date !== (recovery === null ? null : points[recovery].session_date) ||
        typeof episode.trough_drawdown !== "string" || !numeric(episode.trough_drawdown) ||
        Number(episode.trough_drawdown) >= 0 ||
        Math.abs(Number(episode.trough_drawdown) -
          (Number(points[low].close) / Number(points[high].close) - 1)) > 1e-10) return false;
    const underwater = (recovery ?? points.length) - high - 1;
    if (episode.underwater_observed_sessions !== underwater) return false;
    longestUnderwater = Math.max(longestUnderwater, underwater);
    if (recovery === null) {
      if (episode.peak_to_recovery_observed_sessions !== null ||
          episode.trough_to_recovery_observed_sessions !== null) return false;
      ongoing = underwater;
    } else {
      const duration = recovery - high;
      if (episode.peak_to_recovery_observed_sessions !== duration ||
          episode.trough_to_recovery_observed_sessions !== recovery - low) return false;
      longestCompleted = Math.max(longestCompleted ?? 0, duration);
    }
  }
  return data.longest_completed_recovery_observed_sessions === longestCompleted &&
    data.ongoing_drawdown_observed_sessions === ongoing &&
    data.longest_underwater_observed_sessions === (points.length ? longestUnderwater : null);
}

/** Validate provenance, point chronology and semantic identity before any visualization. */
export function parseAnalyticsResult(value: unknown, query: AnalyticsQuery): AnalyticsResult {
  const v = record(value), identity = record(v.identity), source = record(v.source);
  const quality = record(v.quality), settings = record(v.settings);
  if (identity.code !== query.code || identity.instrument_kind !== "index" || identity.provider !== "fred" ||
    identity.units !== "index_points" || !["price_index", "volatility_index"].includes(String(identity.return_semantics)) ||
    typeof v.calculation_version !== "string" || !/^index-analytics-v\d+$/.test(v.calculation_version) ||
    !Number.isSafeInteger(v.observations) || (v.observations as number) > 10000 ||
    !Array.isArray(v.points) || v.observations !== v.points.length || !validPoints(v.points, query.code) ||
    typeof v.metrics_enabled !== "boolean" ||
    !validRecoveryEpisodes(v, v.points, v.metrics_enabled as boolean) ||
    !scalar(v.first_session) || !scalar(v.last_session) ||
    typeof source.snapshot_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(source.snapshot_sha256) ||
    typeof quality.quality_verified !== "boolean" || typeof quality.status !== "string" ||
    v.quality_policy !== query.qualityPolicy || typeof v.unverified_data !== "boolean" ||
    v.unverified_data === quality.quality_verified ||
    v.read_only !== true || v.no_network !== true || v.no_synthetic_sessions !== true ||
    settings.window_sessions !== query.window || v.request === null) {
    throw new Error("Invalid or mismatched local index analytics response.");
  }
  const request = record(v.request);
  if (request.start !== query.start || request.end !== query.end ||
    (v.points.length && (v.first_session !== record(v.points[0]).session_date ||
      v.last_session !== record(v.points[v.points.length - 1]).session_date))) {
    throw new Error("Analytics range does not match the selection.");
  }
  if (query.comparisonCode !== null) {
    const c = record(v.comparison), left = record(c.left), right = record(c.right), cSource = record(c.source);
    if (left.code !== query.code || right.code !== query.comparisonCode ||
      left.provider !== "fred" || right.provider !== "fred" ||
      !Array.isArray(c.points) || !validPoints(c.points, query.comparisonCode, true) ||
      c.points.length !== c.overlap_sessions ||
      typeof cSource.snapshot_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(cSource.snapshot_sha256)) {
      throw new Error("Analytics benchmark does not match the selection.");
    }
  } else if (v.comparison !== null && v.comparison !== undefined) {
    throw new Error("Unexpected analytics benchmark.");
  }
  return value as AnalyticsResult;
}
export async function fetchIndexAnalytics(query: AnalyticsQuery): Promise<AnalyticsResult> {
  validateAnalyticsQuery(query);
  let payload: unknown;
  if (isTauri()) {
    payload = await invoke("analytics_series", {
      code: query.code,
      comparisonCode: query.comparisonCode,
      start: query.start,
      end: query.end,
      window: query.window,
      qualityPolicy: query.qualityPolicy,
    });
  } else {
    const params = new URLSearchParams({
      kind: "index", code: query.code, provider: "fred", start: query.start, end: query.end,
      window: String(query.window), quality_policy: query.qualityPolicy,
    });
    if (query.comparisonCode) {
      params.set("comparison_code", query.comparisonCode);
      params.set("comparison_kind", "index");
      params.set("comparison_provider", "fred");
    }
    const response = await fetch("/api/v1/analytics/series?" + params.toString(), { cache: "no-store" });
    if (!response.ok) {
      const error: unknown = await response.json().catch(() => null);
      const detail = error && typeof error === "object" && "detail" in error && typeof error.detail === "string"
        ? error.detail : "HTTP " + response.status;
      throw new Error(detail);
    }
    payload = await response.json();
  }
  return parseAnalyticsResult(payload, query);
}
