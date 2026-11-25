import { INDEX_CODES, type IndexCode } from "./index-history-api";
import type { HistoryPeriod } from "./history-range";
import { DEFAULT_MARKET_SELECTION, type MarketSelection } from "./market-selection";

export const MARKET_WORKSPACE_KEY = "finintel.market-workspace.v1";
export const MARKET_TAB_LIMIT = 8;
export type MarketSort = "session_date" | "close" | "volume";

export interface MarketView {
  period: HistoryPeriod;
  start: string;
  end: string;
  draftStart: string;
  draftEnd: string;
  sortColumn: MarketSort;
  descending: boolean;
  page: number;
  tableFilter: string;
  showSearch: boolean;
}

export interface MarketTab {
  selection: MarketSelection;
  view: MarketView;
}

export interface MarketWorkspace {
  tabs: MarketTab[];
  active: string;
  comparing?: true;
}

export function marketTabKey(selection: MarketSelection): string {
  return selection.kind === "index" ? `fred:${selection.code}` : `${selection.provider}:${selection.symbol}`;
}

export function marketTabSymbol(selection: MarketSelection): string {
  return selection.kind === "index" ? selection.code : selection.symbol;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

export function parseMarketSelection(value: unknown): MarketSelection | undefined {
  const { kind, code, symbol, provider } = record(value);
  if (kind === "index" && INDEX_CODES.includes(code as IndexCode)) return { kind, code: code as IndexCode };
  if (kind === "equity" && typeof symbol === "string" && /^[a-z0-9][a-z0-9.-]{0,14}$/i.test(symbol)
    && (provider === "alpha_vantage" || provider === "alpaca_sip" || provider === "tiingo")) {
    return { kind, symbol: symbol.toUpperCase(), provider };
  }
}

function cleanDate(value: unknown): string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith("0000")) return "";
  const date = new Date(value + "T00:00:00Z");
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value ? value : "";
}

function parseView(value?: unknown): MarketView {
  const saved = record(value);
  const { period, sortColumn, page, tableFilter } = saved;
  const start = cleanDate(saved.start);
  const end = cleanDate(saved.end);
  const ordered = !(start && end && start > end);
  return {
    period: period === "1y" || period === "5y" || period === "10y" || period === "custom" ? period : "recent",
    start: ordered ? start : "",
    end: ordered ? end : "",
    draftStart: cleanDate(saved.draftStart),
    draftEnd: cleanDate(saved.draftEnd),
    sortColumn: sortColumn === "close" || sortColumn === "volume" ? sortColumn : "session_date",
    descending: saved.descending !== false,
    page: typeof page === "number" && Number.isSafeInteger(page) && page >= 0 ? Math.min(999, page) : 0,
    tableFilter: typeof tableFilter === "string" ? tableFilter.slice(0, 80) : "",
    showSearch: saved.showSearch === true,
  };
}

export function parseMarketWorkspace(value: unknown): MarketWorkspace {
  const saved = record(value);
  const tabs: MarketTab[] = [];
  const keys = new Set<string>();
  if (Array.isArray(saved.tabs)) for (const entry of saved.tabs) {
    const { selection: rawSelection, view } = record(entry);
    const selection = parseMarketSelection(rawSelection);
    if (!selection) continue;
    const key = marketTabKey(selection);
    if (keys.has(key)) continue;
    keys.add(key);
    tabs.push({ selection, view: parseView(view) });
    if (tabs.length === MARKET_TAB_LIMIT) break;
  }
  if (!tabs.length) tabs.push({ selection: DEFAULT_MARKET_SELECTION, view: parseView() });
  return { tabs, active: typeof saved.active === "string" && keys.has(saved.active) ? saved.active : marketTabKey(tabs[0].selection),
    ...(saved.comparing === true ? { comparing: true } : {}) };
}

export function readMarketWorkspace(): MarketWorkspace {
  try {
    return parseMarketWorkspace(JSON.parse(localStorage.getItem(MARKET_WORKSPACE_KEY) ?? "null"));
  } catch {
    return parseMarketWorkspace(null);
  }
}

export function saveMarketWorkspace(value: MarketWorkspace): void {
  try {
    localStorage.setItem(MARKET_WORKSPACE_KEY, JSON.stringify(value));
  } catch {
    // The current workspace remains usable when preferences cannot be saved.
  }
}

export function openMarketTab(workspace: MarketWorkspace, requested: MarketSelection, mode: "current" | "new"): MarketWorkspace {
  const selection = parseMarketSelection(requested);
  if (!selection) return workspace;
  const active = marketTabKey(selection);
  if (workspace.tabs.some(tab => marketTabKey(tab.selection) === active)) return { tabs: workspace.tabs, active };
  if (mode === "new" && workspace.tabs.length >= MARKET_TAB_LIMIT) return workspace;
  const tab: MarketTab = { selection, view: parseView() };
  const tabs: MarketTab[] = mode === "new" ? [...workspace.tabs, tab] : workspace.tabs.map(previous =>
    marketTabKey(previous.selection) === workspace.active
      ? { selection, view: { ...previous.view, page: 0, tableFilter: "", sortColumn: "session_date", descending: true } }
      : previous);
  return { tabs, active };
}

export function closeMarketTab(workspace: MarketWorkspace, key: string): MarketWorkspace {
  const index = workspace.tabs.findIndex(tab => marketTabKey(tab.selection) === key);
  if (index < 0 || workspace.tabs.length < 2) return workspace;
  const tabs = workspace.tabs.filter(tab => marketTabKey(tab.selection) !== key);
  const active = workspace.active === key ? marketTabKey(tabs[Math.min(index, tabs.length - 1)].selection) : workspace.active;
  return { ...workspace, tabs, active };
}
