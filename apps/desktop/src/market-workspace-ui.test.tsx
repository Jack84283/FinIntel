// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import comparisonFixture from "../../../contracts/test-fixtures/market-comparison.json";
import { fetchMarketComparison, type ComparisonResult } from "./market-comparison-api";
import { COMPARISON_KEY } from "./market-comparison-preferences";
import { fetchMarketDaily, fetchMarketInstrumentDirectory } from "./market-api";
import { listIndexHistory, readIndexHistory, type IndexCatalogRecord, type IndexCode } from "./index-history-api";
import type { DailySeries, MarketInstrument } from "./market";
import { MARKET_WORKSPACE_KEY } from "./market-workspace";

vi.mock("./market-comparison-api", async importOriginal => ({
  ...await importOriginal<typeof import("./market-comparison-api")>(),
  fetchMarketComparison: vi.fn(),
}));
vi.mock("./market-api", () => ({ fetchMarketDaily: vi.fn(), fetchMarketInstrumentDirectory: vi.fn() }));
vi.mock("./index-history-api", async importOriginal => ({
  ...await importOriginal<typeof import("./index-history-api")>(),
  listIndexHistory: vi.fn(), readIndexHistory: vi.fn(),
}));
vi.mock("./symbol-catalog-api", () => ({ searchAcquirableSymbols: vi.fn(async () => ({ results: [] })) }));
vi.mock("./AcquisitionCard", () => ({
  AcquisitionCard: ({ onAcquired }: { onAcquired: (symbol: string, provider: string) => void }) =>
    <button onClick={() => onAcquired("AAPL", "tiingo")}>Simulate completed import</button>,
}));
vi.mock("./IndexHistoryCard", () => ({ IndexHistoryCard: () => null }));
vi.mock("./api", () => ({ fetchBackendHealth: vi.fn(async () => { throw new Error("test only"); }) }));

const observations = Array.from({ length: 24 }, (_, i) => ({
  session_date: `2024-03-${String(i + 1).padStart(2, "0")}`, close: String(5000 + i),
}));
const indexRecord = (code: IndexCode): IndexCatalogRecord => ({
  code, name: code === "SP500" ? "S&P 500" : code, fred_series: code, provider: "fred",
  source_url: "https://example.test", series_type: code === "VIX" ? "volatility" : "price",
  frequency: "daily", currency: null, units: "index_points", licensing_note: "", known_history_limit: "",
  sessions: observations.length, local_first_session: observations[0].session_date,
  local_last_session: observations.at(-1)!.session_date, provider_earliest_available: null,
  provider_latest_available: null, first_session: null, last_session: null,
  coverage_updated_at: null, data_kind: "daily_close_only",
});

function stock(symbol: string, provider = "tiingo"): MarketInstrument {
  return { symbol, provider, frequency: "1d", adjustment: "raw_unadjusted", data_kind: "historical_eod",
    sessions: observations.length, first_session: observations[0].session_date, last_session: observations.at(-1)!.session_date };
}

function daily(symbol: string, provider = "tiingo", startDate?: string, endDate?: string, level = 70): DailySeries {
  return {
    symbol, provider, frequency: "1d", price_adjustment: "raw_unadjusted", data_kind: "historical_eod",
    observations: observations.filter(row => (!startDate || row.session_date >= startDate) && (!endDate || row.session_date <= endDate))
      .map((row, i) => ({ session_date: row.session_date, open: String(level), close: String(level + i),
        high: String(level + i + 1), low: String(level - 1), volume: 1000 + i })),
    has_more: false, source_url: null, provider_terms_url: null, retrieved_at: null, provider_last_refreshed: null,
  };
}

let root: Root;
let host: HTMLDivElement;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const preferences = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => preferences.get(key) ?? null,
    setItem: (key: string, value: string) => { preferences.set(key, value); },
  });
  vi.setSystemTime(new Date(2024, 2, 15, 12));
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  vi.mocked(fetchMarketComparison).mockResolvedValue(comparisonFixture as ComparisonResult);
  vi.mocked(fetchMarketInstrumentDirectory).mockResolvedValue([
    stock("AAPL"), stock("AAPL", "alpha_vantage"), stock("NVDA"), stock("QQQ"),
    stock("MSFT"), stock("SPY"), stock("TSLA"), stock("GOOG"), stock("AMZN"),
  ]);
  vi.mocked(listIndexHistory).mockResolvedValue([indexRecord("SP500"), indexRecord("VIX")]);
  vi.mocked(readIndexHistory).mockImplementation(async code => ({
    code, name: indexRecord(code).name, data_kind: "daily_close_only", unit: "index_points", provider: "fred",
    source_url: "https://example.test", has_more: false, observations,
  }));
  vi.mocked(fetchMarketDaily).mockImplementation(async ({ symbol, provider, startDate, endDate }) =>
    daily(symbol, provider, startDate, endDate));
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.clearAllMocks();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function button(label: string, within: ParentNode = host): HTMLButtonElement {
  const match = Array.from(within.querySelectorAll("button")).find(item => item.textContent?.trim() === label || item.getAttribute("aria-label") === label);
  if (!match) throw new Error(`Missing button: ${label}`);
  return match;
}

function tab(key: string): HTMLButtonElement {
  const target = host.querySelector<HTMLButtonElement>(`[data-market-tab="${key}"]`);
  if (!target) throw new Error(`Missing tab ${key}`);
  return target;
}

function visibleRows() { return Array.from(host.querySelectorAll("tbody tr")); }
function active() { return host.querySelector('[role="tab"][aria-selected="true"]')?.getAttribute("data-market-tab"); }
async function click(target: HTMLButtonElement) { await act(async () => { target.click(); }); }
async function change(target: HTMLInputElement | HTMLSelectElement, value: string) {
  await act(async () => {
    const prototype = target instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(target, value);
    target.dispatchEvent(new Event(target instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
}
async function press(target: Element, key: string) {
  await act(async () => { target.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true })); });
}
async function mountMarket(label = "Market") {
  await act(async () => root.render(<App />));
  await click(button(label));
}

async function choose(symbol: string, mode: "new" | "current" = "new", provider = "Tiingo", keyboard = false) {
  if (mode === "new") await click(button("Add instrument tab"));
  else if (!host.querySelector('[role="combobox"]')) await click(button("Show filters"));
  const search = host.querySelector<HTMLInputElement>('[role="combobox"]')!;
  await act(async () => search.focus());
  await change(search, symbol);
  if (keyboard) {
    await press(search, "ArrowDown");
    await press(search, "Enter");
  } else {
    const option = Array.from(host.querySelectorAll<HTMLButtonElement>('[role="option"]'))
      .find(item => item.textContent?.startsWith(symbol) && item.textContent?.endsWith(provider));
    if (!option) throw new Error(`Missing option ${symbol} / ${provider}`);
    await click(option);
  }
  await click(button(mode === "new" ? "Open in New Tab" : "Open in Current Tab"));
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

describe("Market instrument workspace", () => {
  it("opens, deduplicates, disambiguates providers, switches by keyboard and closes tabs", async () => {
    await mountMarket();
    expect(active()).toBe("fred:SP500");
    expect(host.querySelector('svg[aria-label^="Historical index closing-level line chart"]')).toBeTruthy();
    await choose("AAPL", "new", "Tiingo", true);
    expect(active()).toBe("tiingo:AAPL");
    expect(host.textContent).toContain("OHLCV history");
    expect(host.textContent).toContain("Technical indicators");
    await choose("AAPL");
    expect(host.querySelectorAll('[role="tab"]:not([data-market-tab="comparison"])')).toHaveLength(2);
    await choose("AAPL", "new", "Alpha Vantage");
    expect(active()).toBe("alpha_vantage:AAPL");
    expect(tab("tiingo:AAPL").textContent).toContain("Tiingo");
    expect(tab("alpha_vantage:AAPL").textContent).toContain("Alpha Vantage");
    await press(tab("alpha_vantage:AAPL"), "ArrowRight");
    expect(active()).toBe("comparison");
    expect(document.activeElement).toBe(tab("comparison"));
    await press(tab("comparison"), "ArrowRight");
    expect(active()).toBe("fred:SP500");
    expect(document.activeElement).toBe(tab("fred:SP500"));
    await click(button("Close tab · AAPL · Tiingo"));
    expect(active()).toBe("fred:SP500");
    await press(tab("fred:SP500"), "Delete");
    expect(active()).toBe("alpha_vantage:AAPL");
    expect(host.querySelectorAll('[role="tab"]:not([data-market-tab="comparison"])')).toHaveLength(1);
    await choose("QQQ", "current");
    expect(active()).toBe("tiingo:QQQ");
    expect(host.querySelectorAll('[role="tab"]:not([data-market-tab="comparison"])')).toHaveLength(1);
  });

  it("preserves independent dates, sorting, filters and ten-row pages across switches and restart", async () => {
    await mountMarket();
    await change(host.querySelector<HTMLInputElement>('input[aria-label^="Filter table"]')!, "2024-03");
    await click(button("Next"));
    expect(visibleRows()).toHaveLength(10);
    const indexPage = visibleRows()[0].textContent;
    await choose("AAPL");
    await click(button("Show filters"));
    const period = host.querySelector<HTMLSelectElement>('select[aria-label="Historical time range"]')!;
    await change(period, "5y");
    await change(period, "recent");
    const calendars = host.querySelectorAll<HTMLButtonElement>('button[aria-haspopup="dialog"]');
    await click(calendars[0]);
    await click(button("Monday, March 4, 2024"));
    await click(calendars[1]);
    await click(button("Saturday, March 23, 2024"));
    await click(button("Apply range"));
    await change(host.querySelector<HTMLInputElement>('input[aria-label^="Filter table"]')!, "2024-03");
    await click(button("Close"));
    await click(button("Close ↓"));
    await click(button("Next"));
    const equityPage = visibleRows()[0].textContent;
    expect(fetchMarketDaily).toHaveBeenLastCalledWith(expect.objectContaining({
      symbol: "AAPL", startDate: "2024-03-04", endDate: "2024-03-23",
    }));
    await click(tab("fred:SP500"));
    expect(visibleRows()[0].textContent).toBe(indexPage);
    expect(host.querySelector<HTMLInputElement>('input[aria-label^="Filter table"]')?.value).toBe("2024-03");
    await click(tab("tiingo:AAPL"));
    expect(visibleRows()[0].textContent).toBe(equityPage);
    expect(host.querySelector<HTMLSelectElement>('select[aria-label="Historical time range"]')?.value).toBe("custom");
    expect(JSON.parse(localStorage.getItem(MARKET_WORKSPACE_KEY)!).tabs[1].view).toMatchObject({
      period: "custom", sortColumn: "close", descending: false, tableFilter: "2024-03", page: 1,
    });
    await act(async () => root.unmount());
    root = createRoot(host);
    await mountMarket();
    expect(active()).toBe("tiingo:AAPL");
    expect(visibleRows()[0].textContent).toBe(equityPage);
  });

  it("honors acquisition navigation and preserves the eight existing tabs at capacity", async () => {
    const symbols = ["AAPL", "NVDA", "QQQ", "MSFT", "SPY", "TSLA", "GOOG"];
    localStorage.setItem(MARKET_WORKSPACE_KEY, JSON.stringify({
      active: "fred:SP500", tabs: [
        { selection: { kind: "index", code: "SP500" } },
        ...symbols.map(symbol => ({ selection: { kind: "equity", symbol, provider: "tiingo" } })),
      ],
    }));
    await mountMarket();
    await click(button("Add instrument tab"));
    expect(host.textContent).toContain("Eight instrument tabs are open.");
    expect(host.querySelectorAll('[role="tab"]:not([data-market-tab="comparison"])')).toHaveLength(8);
    await click(button("Data Acquisition", host.querySelector("header") ?? host));
    await click(button("Simulate completed import"));
    await click(button("View in Markets"));
    expect(active()).toBe("tiingo:AAPL");
    expect(host.querySelectorAll('[role="tab"]:not([data-market-tab="comparison"])')).toHaveLength(8);
    await click(button("Close tab · QQQ · Tiingo"));
    await choose("AMZN");
    expect(active()).toBe("tiingo:AMZN");
    expect(host.querySelectorAll('[role="tab"]:not([data-market-tab="comparison"])')).toHaveLength(8);
    await click(tab("fred:SP500"));
    expect(active()).toBe("fred:SP500");
  });

  it("ignores delayed responses from closed tabs and superseded date windows", async () => {
    await mountMarket();
    const closed = deferred<DailySeries>();
    vi.mocked(fetchMarketDaily).mockReturnValueOnce(closed.promise);
    await choose("AAPL");
    expect(host.querySelector('[aria-label="Loading historical prices"]')).toBeTruthy();
    await click(button("Close tab · AAPL · Tiingo"));
    await choose("QQQ");
    expect(active()).toBe("tiingo:QQQ");
    const before = host.textContent;
    await act(async () => closed.resolve(daily("AAPL", "tiingo", undefined, undefined, 9876)));
    expect(host.textContent).toBe(before);

    await click(button("Show filters"));
    const period = host.querySelector<HTMLSelectElement>('select[aria-label="Historical time range"]')!;
    const outdated = deferred<DailySeries>();
    vi.mocked(fetchMarketDaily).mockReturnValueOnce(outdated.promise);
    await change(period, "5y");
    expect(host.textContent).not.toContain("OHLCV history");
    await change(period, "recent");
    expect(host.textContent).toContain("OHLCV history");
    const current = host.textContent;
    await act(async () => outdated.resolve(daily("QQQ", "tiingo", undefined, undefined, 9876)));
    expect(host.textContent).toBe(current);
  });

  it("retains a missing saved instrument, localizes its recovery and reads only the selected provider", async () => {
    localStorage.setItem("finintel.workspace-preferences.v1", JSON.stringify({ language: "zh-CN" }));
    localStorage.setItem(MARKET_WORKSPACE_KEY, JSON.stringify({
      tabs: [{ selection: { kind: "equity", symbol: "MISSING", provider: "alpaca_sip" } }],
      active: "alpaca_sip:MISSING",
    }));
    vi.mocked(fetchMarketDaily).mockRejectedValueOnce(new Error("private storage path"));
    await mountMarket("市场");
    expect(active()).toBe("alpaca_sip:MISSING");
    expect(button("添加品种标签页")).toBeTruthy();
    expect(host.textContent).toContain("请确认本地已保存此品种和数据源的历史记录。");
    expect(host.textContent).not.toContain("private storage path");
    expect(fetchMarketDaily).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ symbol: "MISSING", provider: "alpaca_sip" }));
    expect(readIndexHistory).not.toHaveBeenCalled();
  });
});


describe("Market comparison panel", () => {
  const comparison = comparisonFixture as ComparisonResult;

  async function addSource(key: string) {
    await click(button("Select comparison instruments"));
    const picker = host.querySelector("dialog")!;
    const check = Array.from(picker.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')).find(input => input.value === key)!;
    await act(async () => check.click());
    await click(button("Apply selection", picker));
  }

  function saveSources() {
    localStorage.setItem(COMPARISON_KEY, JSON.stringify({
      sources: comparison.request.sources, period: "custom", start: comparison.request.start, end: comparison.request.end,
    }));
  }

  it("combines a current instrument with a local source and keeps modes, gaps and tables in sync", async () => {
    await mountMarket();
    await click(tab("comparison"));
    expect(tab("comparison").previousElementSibling).toBeNull();
    await addSource("tiingo:AAPL");
    expect(host.textContent).toContain("Select at least two local instruments");
    expect(fetchMarketComparison).not.toHaveBeenCalled();
    await click(tab("fred:SP500"));
    await click(button("Add to comparison"));
    expect(active()).toBe("comparison");
    expect(fetchMarketComparison).toHaveBeenLastCalledWith(comparison.request.sources,
      expect.any(String), expect.any(String), expect.any(AbortSignal));
    expect(host.textContent).toContain("Coverage differs from the requested bounds");
    expect(host.querySelector('[aria-label="Comparison summary"]')?.textContent).toContain("5200");
    const plotted = host.querySelector('[data-series="tiingo:AAPL"]');
    expect(plotted?.querySelectorAll("circle")).toHaveLength(2);
    expect(plotted?.querySelector("path")).toBeNull();
    const mode = Array.from(host.querySelectorAll("select")).find(select => select.value === "normalized_100")!;
    await change(mode, "cumulative_return_pct");
    const historical = host.querySelector('section[aria-label="Comparison observation table"]')!;
    expect(historical.textContent).toContain("105 / 5%");
    expect(historical.textContent).toContain("Unavailable");
    expect(host.querySelector("output")?.textContent).toContain("Unavailable");
    const check = host.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    await act(async () => check.click());
    expect(host.querySelectorAll("[data-series]")).toHaveLength(1);
    expect(historical.textContent).toContain("105 / 5%");
    expect(fetchMarketComparison).toHaveBeenCalledTimes(1);
    await press(tab("comparison"), "ArrowRight");
    expect(active()).toBe("fred:SP500");
    expect(host.textContent).toContain("Historical index table");
    await click(tab("comparison"));
    expect(host.querySelectorAll("[data-series]")).toHaveLength(1);
    expect(host.textContent).toContain("105 / 5%");
    expect(fetchMarketComparison).toHaveBeenCalledTimes(2);
  });

  it("limits and removes source selections without changing individual tabs", async () => {
    saveSources();
    await mountMarket();
    await click(tab("comparison"));
    await addSource("tiingo:NVDA");
    await addSource("alpha_vantage:AAPL");
    expect(host.textContent).toContain("Four comparison sources are selected.");
    await click(button("Select comparison instruments"));
    expect(host.querySelector<HTMLInputElement>('dialog input[value="tiingo:MSFT"]')?.disabled).toBe(true);
    await click(button("Cancel", host.querySelector("dialog")!));
    await click(button("Remove from comparison · AAPL · Alpha Vantage"));
    expect(host.textContent).not.toContain("Four comparison sources are selected.");
    await click(button("Clear comparison"));
    expect(host.textContent).toContain("Select at least two local instruments");
    expect(tab("fred:SP500")).toBeTruthy();
  });

  it("selects multiple instruments in one popup and applies a shared range automatically", async () => {
    await mountMarket();
    await click(tab("comparison"));
    await click(button("Select comparison instruments"));
    let picker = host.querySelector("dialog")!;
    for (const key of ["tiingo:AAPL", "tiingo:NVDA"]) {
      const check = picker.querySelector<HTMLInputElement>('input[value="' + key + '"]')!;
      await act(async () => check.click());
    }
    await click(button("Apply selection", picker));
    expect(fetchMarketComparison).toHaveBeenCalledTimes(1);
    const sources = [{ kind: "equity", code: "AAPL", provider: "tiingo" }, { kind: "equity", code: "NVDA", provider: "tiingo" }];
    const period = Array.from(host.querySelectorAll("select")).find(select => select.value === "1y")!;
    await change(period, "5y");
    expect(fetchMarketComparison).toHaveBeenLastCalledWith(sources, "2019-03-15", "2024-03-15", expect.any(AbortSignal));
    await click(button("Select comparison instruments"));
    picker = host.querySelector("dialog")!;
    const check = picker.querySelector<HTMLInputElement>('input[value="tiingo:MSFT"]')!;
    await act(async () => check.click());
    await click(button("Cancel", picker));
    expect(fetchMarketComparison).toHaveBeenCalledTimes(2);
    await change(period, "custom");
    const dates = host.querySelectorAll<HTMLInputElement>('input[type="date"]');
    await change(dates[0], "2020-01-01");
    expect(fetchMarketComparison).toHaveBeenLastCalledWith(sources, "2020-01-01", "2024-03-15", expect.any(AbortSignal));
  });

  it("restores a comparison, rejects late results and retries a safe error", async () => {
    saveSources();
    localStorage.setItem(MARKET_WORKSPACE_KEY, JSON.stringify({ comparing: true }));
    const old = deferred<ComparisonResult>();
    vi.mocked(fetchMarketComparison).mockReturnValueOnce(old.promise).mockRejectedValueOnce(new Error("missing_history"));
    await mountMarket();
    expect(active()).toBe("comparison");
    expect(readIndexHistory).not.toHaveBeenCalled();
    const period = Array.from(host.querySelectorAll("select")).find(select => select.value === "custom")!;
    await change(period, "5y");
    expect(host.textContent).toContain("A selected source has no local history");
    await act(async () => old.resolve(comparison));
    expect(host.querySelector('[aria-label="Comparison summary"]')).toBeNull();
    expect(host.textContent).toContain("A selected source has no local history");
    await click(button("Retry local query"));
    expect(host.querySelector('[aria-label="Comparison summary"]')).toBeTruthy();
    expect(fetchMarketComparison).toHaveBeenCalledTimes(3);
    await click(button("Reload local data"));
    expect(fetchMarketComparison).toHaveBeenCalledTimes(4);
  });

  it("localizes the comparison tab and source guidance", async () => {
    localStorage.setItem("finintel.workspace-preferences.v1", JSON.stringify({ language: "zh-CN" }));
    await mountMarket("市场");
    expect(tab("comparison").textContent).toBe("对比");
    await click(tab("comparison"));
    expect(host.textContent).toContain("请选择至少两个本地品种进行对比。");
  });
});
