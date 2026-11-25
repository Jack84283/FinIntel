import { invoke, isTauri } from "@tauri-apps/api/core";
import { canonicalModelSha256 } from "./model-research-integrity";
import type { MarketSelection } from "./market-selection";

const CONTRACT = "market-comparison-v1";
const REVISION = "market-comparison-decimal-v1";
const MAX_BYTES = 4 * 1024 * 1024;
const PRICE_CODES = ["SP500", "DJIA", "NASDAQCOM", "NDX"] as const;
const PROVIDERS = ["alpha_vantage", "alpaca_sip", "tiingo"] as const;
const WARNINGS = ["price_level_only", "mixed_semantics", "quality_unverified", "coverage_gaps"] as const;
const ERRORS = ["invalid_input", "invalid_source", "unsupported_vix", "missing_history",
  "insufficient_overlap", "incompatible_source", "invalid_close", "resource_limit",
  "backend_unavailable", "invalid_response"] as const;
export type ComparisonError = typeof ERRORS[number];
export type ComparisonWarning = typeof WARNINGS[number];
export type ComparisonMode = "normalized_100" | "cumulative_return_pct";
export type ComparisonSource =
  | { kind: "index"; provider: "fred"; code: typeof PRICE_CODES[number] }
  | { kind: "equity"; provider: typeof PROVIDERS[number]; code: string };

export interface ComparisonRequest {
  contract_version: typeof CONTRACT;
  sources: ComparisonSource[];
  start: string;
  end: string;
}

export interface ComparisonCell {
  close: string;
  normalized_100: string;
  cumulative_return_pct: string;
}

export interface ComparisonPoint {
  session_date: string;
  values: (ComparisonCell | null)[];
}

export interface ComparisonEvidence {
  key: string;
  identity: ComparisonSource;
  adjustment: string;
  return_semantics: string;
  frequency: string;
  units: string;
  snapshot_sha256: string;
  first_date: string;
  latest_date: string;
  baseline_close: string;
  common_end_close: string;
  common_end_return_pct: string;
  latest_close: string;
  observations: number;
  missing_sessions: number;
  nonshared_sessions: number;
}

export interface ComparisonResult {
  contract_version: typeof CONTRACT;
  calculation_revision: typeof REVISION;
  request: ComparisonRequest;
  baseline_date: string;
  last_common_date: string;
  shared_sessions: number;
  sources: ComparisonEvidence[];
  points: ComparisonPoint[];
  fingerprint_sha256: string;
  output_sha256: string;
  warnings: ComparisonWarning[];
}

export function sourceKey(source: ComparisonSource): string { return `${source.provider}:${source.code}`; }

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

export function comparisonSource(value: unknown): ComparisonSource | undefined {
  const { kind, provider, code } = record(value);
  if (kind === "index" && provider === "fred" && PRICE_CODES.includes(code as typeof PRICE_CODES[number])) {
    return { kind, provider, code: code as typeof PRICE_CODES[number] };
  }
  if (kind === "equity" && typeof code === "string" && /^[A-Z0-9][A-Z0-9.-]{0,14}$/.test(code)
    && PROVIDERS.includes(provider as typeof PROVIDERS[number])) {
    return { kind, code, provider: provider as typeof PROVIDERS[number] };
  }
}

export function comparisonSourceFromSelection(selection: MarketSelection) {
  return comparisonSource(selection.kind === "index"
    ? { ...selection, provider: "fred" } : { kind: "equity", code: selection.symbol, provider: selection.provider });
}

export function validComparisonDate(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith("0000")) return false;
  const date = new Date(value + "T00:00:00Z");
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
}

export function validComparisonWindow(start: string, end: string): boolean {
  return validComparisonDate(start) && validComparisonDate(end) && start <= end
    && end <= new Date().toISOString().slice(0, 10)
    && Date.parse(end) - Date.parse(start) <= 3660 * 86400000;
}

function check(condition: unknown): asserts condition {
  if (!condition) throw new Error("invalid_response");
}

const digest = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const decimal = (value: unknown): value is string =>
  typeof value === "string" && value.length <= 110 && /^-?\d+(?:\.\d+)?$/.test(value) && Number.isFinite(Number(value));
const sameSource = (value: unknown, requested: ComparisonSource) => {
  const source = comparisonSource(value);
  return source && Object.keys(record(value)).length === 3 && source.kind === requested.kind && sourceKey(source) === sourceKey(requested);
};

export async function parseComparisonResult(value: unknown, request: ComparisonRequest): Promise<ComparisonResult> {
  const result = record(value);
  const echo = record(result.request);
  const { baseline_date: baseline, last_common_date: commonEnd, sources, points, warnings } = result;
  check(result.contract_version === CONTRACT && result.calculation_revision === REVISION
    && result.read_only === true && result.no_network === true && result.no_synthetic_sessions === true
    && echo.contract_version === CONTRACT && echo.start === request.start && echo.end === request.end
    && Array.isArray(echo.sources) && echo.sources.length === request.sources.length
    && request.sources.every((source, i) => sameSource((echo.sources as unknown[])[i], source))
    && validComparisonDate(baseline) && validComparisonDate(commonEnd) && baseline < commonEnd
    && Array.isArray(points) && points.length >= 2 && points.length <= 3661
    && Array.isArray(sources) && sources.length === request.sources.length
    && Array.isArray(warnings) && warnings.every(warning => WARNINGS.includes(warning))
    && warnings.includes("price_level_only") && new Set(warnings).size === warnings.length
    && digest(result.fingerprint_sha256) && digest(result.output_sha256));

  const aligned: ComparisonPoint[] = [];
  let shared = 0;
  let endpoint: ComparisonPoint | undefined;
  for (const raw of points) {
    const { session_date: day, values } = record(raw);
    check(validComparisonDate(day) && day >= request.start && day <= request.end
      && day > (aligned.at(-1)?.session_date ?? "") && Array.isArray(values)
      && values.length === request.sources.length && values.some(cell => cell !== null));
    const cells = values.map((cell): ComparisonCell | null => {
      if (cell === null) return null;
      const { close, normalized_100, cumulative_return_pct } = record(cell);
      check(Object.keys(record(cell)).length === 3 && decimal(close) && close.length <= 48 && Number(close) > 0
        && decimal(normalized_100) && Number(normalized_100) >= 0
        && decimal(cumulative_return_pct) && Number(cumulative_return_pct) >= -100);
      return { close, normalized_100, cumulative_return_pct };
    });
    const point = { session_date: day, values: cells };
    aligned.push(point);
    if (cells.every(cell => cell !== null)) { shared++; endpoint = point; }
  }
  const first = aligned[0];
  check(first.session_date === baseline && first.values.every(cell => cell?.normalized_100 === "100" && cell.cumulative_return_pct === "0")
    && shared >= 2 && shared === result.shared_sessions && endpoint?.session_date === commonEnd);
  const lastCommon = endpoint;
  const evidence = sources.map((raw, i): ComparisonEvidence => {
    const item = record(raw);
    const identity = request.sources[i];
    const isIndex = identity.kind === "index";
    let latest = first;
    let count = 0;
    for (const row of aligned) if (row.values[i] !== null) { latest = row; count++; }
    const { snapshot_sha256, first_date, latest_date, observations } = item;
    check(item.key === sourceKey(identity) && sameSource(item.identity, identity)
      && item.adjustment === (isIndex ? "price_index" : "raw_unadjusted")
      && item.return_semantics === (isIndex ? "price_index" : "raw_unadjusted_equity_price_usd_per_share")
      && item.frequency === (isIndex ? "daily" : "1d") && item.units === (isIndex ? "index_points" : "usd_per_share")
      && digest(snapshot_sha256) && validComparisonDate(first_date) && first_date >= request.start && first_date <= baseline
      && latest_date === latest.session_date && item.latest_close === latest.values[i]!.close
      && item.baseline_close === first.values[i]!.close && item.common_end_close === lastCommon.values[i]!.close
      && item.common_end_return_pct === lastCommon.values[i]!.cumulative_return_pct
      && typeof observations === "number" && Number.isSafeInteger(observations) && observations >= count
      && (first_date === baseline ? observations === count : observations > count
        && observations <= count + (Date.parse(baseline) - Date.parse(first_date)) / 86400000)
      && item.nonshared_sessions === count - shared && item.missing_sessions === aligned.length - count);
    return { ...item, identity } as ComparisonEvidence;
  });
  const coverageGaps = evidence.some(source => source.first_date !== request.start || source.latest_date !== request.end
    || source.missing_sessions > 0 || source.nonshared_sessions > 0);
  check(warnings.includes("coverage_gaps") === coverageGaps
    && warnings.includes("mixed_semantics") === (new Set(evidence.map(source => source.return_semantics)).size > 1));
  const [fingerprint, outputDigest] = await Promise.all([
    canonicalModelSha256({ revision: REVISION, request, sources: evidence.map(source => source.snapshot_sha256) }),
    canonicalModelSha256(aligned),
  ]).catch(() => { throw new Error("invalid_response"); });
  check(fingerprint === result.fingerprint_sha256 && outputDigest === result.output_sha256);
  return { contract_version: CONTRACT, calculation_revision: REVISION, request, baseline_date: baseline,
    last_common_date: commonEnd, shared_sessions: shared, sources: evidence, points: aligned,
    fingerprint_sha256: result.fingerprint_sha256, output_sha256: result.output_sha256, warnings };
}

export function comparisonError(error: unknown): ComparisonError {
  const message = error instanceof Error ? error.message : error;
  return ERRORS.includes(message as ComparisonError) ? message as ComparisonError : "backend_unavailable";
}

async function readBounded(response: Response): Promise<unknown> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("invalid_response");
  let count = 0;
  let body = "";
  const decoder = new TextDecoder();
  try {
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) break;
      count += chunk.value.length;
      if (count > MAX_BYTES) throw new Error("invalid_response");
      body += decoder.decode(chunk.value, { stream: true });
    }
    return JSON.parse(body + decoder.decode());
  } catch {
    void reader.cancel().catch(() => undefined);
    throw new Error("invalid_response");
  } finally {
    reader.releaseLock();
  }
}

export async function fetchMarketComparison(sources: ComparisonSource[], start: string, end: string, signal?: AbortSignal) {
  if (sources.length < 2 || sources.length > 4 || sources.some(source => !comparisonSource(source))
    || new Set(sources.map(sourceKey)).size !== sources.length || !validComparisonWindow(start, end)) throw new Error("invalid_input");
  const request: ComparisonRequest = { contract_version: CONTRACT, sources, start, end };
  if (isTauri()) return parseComparisonResult(await invoke("market_comparison", { request }), request);
  const response = await fetch("/api/v1/market-data/comparison", {
    method: "POST", body: JSON.stringify(request), headers: { "Content-Type": "application/json" }, signal,
  });
  const value = await readBounded(response);
  if (!response.ok) throw new Error(comparisonError(record(value).detail));
  return parseComparisonResult(value, request);
}
