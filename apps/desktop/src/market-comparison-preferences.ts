import {
  comparisonSource, sourceKey, validComparisonWindow,
  type ComparisonMode, type ComparisonSource,
} from "./market-comparison-api";

export const COMPARISON_KEY = "finintel.market-comparison.v1";
export const COMPARISON_LIMIT = 4;
export type ComparisonPeriod = "1y" | "5y" | "10y" | "custom";

export interface ComparisonPreferences {
  sources: ComparisonSource[];
  hidden: string[];
  period: ComparisonPeriod;
  start: string;
  end: string;
  mode: ComparisonMode;
}

export function presetWindow(period: Exclude<ComparisonPeriod, "custom">, today = new Date()) {
  const end = today.toISOString().slice(0, 10);
  const start = new Date(end + "T00:00:00Z");
  start.setUTCFullYear(start.getUTCFullYear() - Number.parseInt(period, 10));
  if (start.getUTCDate() !== today.getUTCDate()) start.setUTCDate(0);
  return { start: start.toISOString().slice(0, 10), end };
}

export function parseComparisonPreferences(value: unknown): ComparisonPreferences {
  const raw = value && typeof value === "object" ? value as Record<string, unknown> : {};
  const sources: ComparisonSource[] = [];
  if (Array.isArray(raw.sources)) for (const saved of raw.sources) {
    const source = comparisonSource(saved);
    if (!source || sources.some(other => sourceKey(other) === sourceKey(source))) continue;
    sources.push(source);
    if (sources.length === COMPARISON_LIMIT) break;
  }
  const period = raw.period === "custom" || raw.period === "5y" || raw.period === "10y" ? raw.period : "1y";
  const hasWindow = typeof raw.start === "string" && typeof raw.end === "string" && validComparisonWindow(raw.start, raw.end);
  const hidden = Array.isArray(raw.hidden) ? raw.hidden : [];
  return {
    sources, hidden: sources.map(sourceKey).filter(key => hidden.includes(key)),
    period: hasWindow ? period : period === "custom" ? "1y" : period,
    ...(hasWindow ? { start: raw.start as string, end: raw.end as string } : presetWindow(period === "custom" ? "1y" : period)),
    mode: raw.mode === "normalized_100" || raw.mode === "close" ? raw.mode : "cumulative_return_pct",
  };
}

export function readComparisonPreferences() {
  try { return parseComparisonPreferences(JSON.parse(localStorage.getItem(COMPARISON_KEY) ?? "null")); }
  catch { return parseComparisonPreferences(null); }
}

export function saveComparisonPreferences(settings: ComparisonPreferences) {
  try { localStorage.setItem(COMPARISON_KEY, JSON.stringify(settings)); }
  catch { /* The in-memory session is still usable. */ }
}
