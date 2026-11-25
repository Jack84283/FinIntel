import {
  comparisonSource, sourceKey, validComparisonWindow,
  type ComparisonMode, type ComparisonSource,
} from "./market-comparison-api";

export const COMPARISON_KEY = "finintel.market-comparison.v1";
export const COMPARISON_LIMIT = 4;
export const COMPARISON_PERIODS = ["1w", "1m", "3m", "6m", "ytd", "1y", "3y", "5y", "10y"] as const;
export type ComparisonPeriod = typeof COMPARISON_PERIODS[number] | "custom";

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
  if (period === "1w") start.setUTCDate(start.getUTCDate() - 7);
  else if (period === "ytd") start.setUTCMonth(0, 1);
  else {
    const day = start.getUTCDate();
    if (period.endsWith("m")) start.setUTCMonth(start.getUTCMonth() - Number.parseInt(period, 10), 1);
    else { start.setUTCDate(1); start.setUTCFullYear(start.getUTCFullYear() - Number.parseInt(period, 10)); }
    const last = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth() + 1, 0)).getUTCDate();
    start.setUTCDate(Math.min(day, last));
  }
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
  const period: ComparisonPeriod = raw.period === "custom" ? "custom" : COMPARISON_PERIODS.find(item => item === raw.period) ?? "1y";
  const hasWindow = typeof raw.start === "string" && typeof raw.end === "string" && validComparisonWindow(raw.start, raw.end);
  const hidden = Array.isArray(raw.hidden) ? raw.hidden : [];
  return {
    sources, hidden: sources.map(sourceKey).filter(key => hidden.includes(key)),
    period: hasWindow ? period : period === "custom" ? "1y" : period,
    ...(hasWindow ? { start: raw.start as string, end: raw.end as string } : presetWindow(period === "custom" ? "1y" : period)),
    mode: raw.mode === "normalized_100" || raw.mode === "close" ? raw.mode : "cumulative_return_pct",
  };
}

export function readComparisonPreferences(initialSource?: ComparisonSource) {
  try {
    const stored = localStorage.getItem(COMPARISON_KEY);
    if (stored !== null) return parseComparisonPreferences(JSON.parse(stored));
  } catch { /* Fall back only when storage is unavailable or invalid JSON. */ }
  return parseComparisonPreferences({ sources: initialSource ? [initialSource] : [] });
}

export function saveComparisonPreferences(settings: ComparisonPreferences) {
  try { localStorage.setItem(COMPARISON_KEY, JSON.stringify(settings)); }
  catch { /* The in-memory session is still usable. */ }
}


export const COMPARISON_TABS_KEY = "finintel.market-comparison-tabs.v1";
export const COMPARISON_TAB_LIMIT = 8;
export interface ComparisonTab {
  id: string;
  settings: ComparisonPreferences;
}
export interface ComparisonWorkspace {
  active: string;
  tabs: ComparisonTab[];
  nextId: number;
}

export function readComparisonWorkspace(initialSource?: ComparisonSource): ComparisonWorkspace {
  try {
    const stored = JSON.parse(localStorage.getItem(COMPARISON_TABS_KEY) ?? "null");
    if (stored && Array.isArray(stored.tabs)) {
      const tabs: ComparisonTab[] = [];
      for (const tab of stored.tabs) {
        if (!tab || typeof tab.id !== "string" || !/^chart-[1-9][0-9]{0,8}$/.test(tab.id)
          || tabs.some(item => item.id === tab.id)) continue;
        tabs.push({ id: tab.id, settings: parseComparisonPreferences(tab.settings) });
        if (tabs.length === COMPARISON_TAB_LIMIT) break;
      }
      if (tabs.length) return {
        tabs, active: tabs.some(tab => tab.id === stored.active) ? stored.active : tabs[0].id,
        nextId: Math.max(1, ...tabs.map(tab => Number(tab.id.slice(6)))) + 1,
      };
    }
  } catch { /* Preserve compatibility with existing single-chart preferences. */ }
  return { active: "chart-1", tabs: [{ id: "chart-1", settings: readComparisonPreferences(initialSource) }], nextId: 2 };
}

export function saveComparisonWorkspace(workspace: ComparisonWorkspace) {
  try { localStorage.setItem(COMPARISON_TABS_KEY, JSON.stringify(workspace)); }
  catch { /* In-memory session remains usable. */ }
  const active = workspace.tabs.find(tab => tab.id === workspace.active);
  if (active) saveComparisonPreferences(active.settings);
}

export function updateComparisonTab(workspace: ComparisonWorkspace, id: string,
  update: ComparisonPreferences | ((current: ComparisonPreferences) => ComparisonPreferences)): ComparisonWorkspace {
  return { ...workspace, tabs: workspace.tabs.map(tab => tab.id !== id ? tab : {
    ...tab, settings: typeof update === "function" ? update(tab.settings) : update,
  }) };
}

export function addComparisonTab(workspace: ComparisonWorkspace): ComparisonWorkspace {
  if (workspace.tabs.length >= COMPARISON_TAB_LIMIT) return workspace;
  const id = "chart-" + workspace.nextId;
  return { active: id, nextId: workspace.nextId + 1,
    tabs: [...workspace.tabs, { id, settings: parseComparisonPreferences(null) }] };
}

export function closeComparisonTab(workspace: ComparisonWorkspace, id: string): ComparisonWorkspace {
  const index = workspace.tabs.findIndex(tab => tab.id === id);
  if (index < 0 || workspace.tabs.length <= 1) return workspace;
  const tabs = workspace.tabs.filter(tab => tab.id !== id);
  return { ...workspace, tabs, active: workspace.active === id ? tabs[Math.min(index, tabs.length - 1)].id : workspace.active };
}
