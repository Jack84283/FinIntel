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
async function mountMarket(label = "Market", details = false) {
  await act(async () => root.render(<App />));
  await click(button(label));
  if (details) await click(button(label === "市场" ? "历史价格详情" : "Historical price details"));
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
    await mountMarket("Market", true);
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
    expect(host.querySelector("[data-market-tab]")).toBeNull();
    await click(button("Historical price details"));
    await click(tab("fred:SP500"));
    expect(active()).toBe("fred:SP500");
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
    await mountMarket("Market", true);
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
    await click(button("Historical price details"));
    await click(tab("tiingo:AAPL"));
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
    await mountMarket("Market", true);
    await click(button("Add instrument tab"));
    expect(host.textContent).toContain("Eight instrument tabs are open.");
    expect(host.querySelectorAll('[role="tab"]:not([data-market-tab="comparison"])')).toHaveLength(8);
    await click(button("Data Acquisition", host.querySelector("header") ?? host));
    await click(button("Simulate completed import"));
    await click(button("View in Markets"));
    expect(fetchMarketComparison).toHaveBeenLastCalledWith([{ kind: "index", code: "SP500", provider: "fred" }, { kind: "equity", code: "AAPL", provider: "tiingo" }], expect.any(String), expect.any(String), expect.any(AbortSignal));
    await change(host.querySelector<HTMLSelectElement>('select[aria-label="Historical detail instrument"]')!, "tiingo:AAPL");
    await click(button("Historical price details"));
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
    await mountMarket("Market", true);
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
    await mountMarket("市场", true);
    expect(active()).toBe("alpaca_sip:MISSING");
    expect(button("添加品种标签页")).toBeTruthy();
    expect(host.textContent).toContain("请确认本地已保存此品种和数据源的历史记录。");
    expect(host.textContent).not.toContain("private storage path");
    expect(fetchMarketDaily).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ symbol: "MISSING", provider: "alpaca_sip" }));
    expect(readIndexHistory).not.toHaveBeenCalled();
  });
});


describe("Chart-first Market workspace", () => {
  it("opens a single default source without tabs or a standalone add button", async () => {
    await mountMarket();
    expect(host.querySelector("[data-market-tab]")).toBeNull();
    expect(fetchMarketComparison).toHaveBeenLastCalledWith([{ kind: "index", code: "SP500", provider: "fred" }],
      expect.any(String), expect.any(String), expect.any(AbortSignal));
    expect(button("Select comparison instruments")).toBeTruthy();
  });

  it("batch-selects sources, cancels edits and shares the date range", async () => {
    localStorage.setItem(MARKET_WORKSPACE_KEY, JSON.stringify({ comparing: true }));
    localStorage.setItem(COMPARISON_KEY, JSON.stringify({ sources: [] }));
    await mountMarket();
    await click(button("Select comparison instruments"));
    let picker = host.querySelector("dialog")!;
    for (const key of ["tiingo:AAPL", "fred:SP500"]) {
      const check = picker.querySelector<HTMLInputElement>('input[value="' + key + '"]')!;
      await act(async () => check.click());
    }
    await click(button("Apply selection", picker));
    const sources = [{ kind: "equity", code: "AAPL", provider: "tiingo" }, { kind: "index", code: "SP500", provider: "fred" }];
    expect(fetchMarketComparison).toHaveBeenCalledTimes(1);
    const period = Array.from(host.querySelectorAll("select")).find(select => select.value === "1y")!;
    await change(period, "5y");
    expect(fetchMarketComparison).toHaveBeenLastCalledWith(sources, "2019-03-15", "2024-03-15", expect.any(AbortSignal));
    await click(button("Select comparison instruments"));
    picker = host.querySelector("dialog")!;
    await act(async () => picker.querySelector<HTMLInputElement>('input[value="tiingo:MSFT"]')!.click());
    await click(button("Cancel", picker));
    expect(fetchMarketComparison).toHaveBeenCalledTimes(2);
    const chart = host.querySelector('svg[data-comparison-chart]') ?? host.querySelector('svg[aria-describedby]');
    const summary = host.querySelector('[aria-label="Comparison summary"]');
    expect(chart && summary && Boolean(chart.compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true);
    expect(host.querySelector("output")?.textContent).toContain("Unavailable");
    const mode = Array.from(host.querySelectorAll("select")).find(select => select.value === "cumulative_return_pct")!;
    await change(mode, "close");
    expect(fetchMarketComparison).toHaveBeenCalledTimes(2);
    expect(host.textContent).toContain("Actual close");
    await change(period, "custom");
    const dates = host.querySelectorAll<HTMLInputElement>('input[type="date"]');
    await change(dates[0], "2020-01-01");
    expect(fetchMarketComparison).toHaveBeenLastCalledWith(sources, "2020-01-01", "2024-03-15", expect.any(AbortSignal));
  });

  it("preserves saved comparison selection through historical navigation and remount", async () => {
    const sources = (comparisonFixture as ComparisonResult).request.sources;
    localStorage.setItem(COMPARISON_KEY, JSON.stringify({ sources }));
    await mountMarket();
    expect(fetchMarketComparison).toHaveBeenLastCalledWith(sources, expect.any(String), expect.any(String), expect.any(AbortSignal));
    await click(button("Historical price details"));
    expect(active()).toBe("tiingo:AAPL");
    await click(button("Overview"));
    await click(button("Market"));
    expect(JSON.parse(localStorage.getItem(COMPARISON_KEY)!).sources).toEqual(sources);
    expect(fetchMarketComparison).toHaveBeenLastCalledWith(sources, expect.any(String), expect.any(String), expect.any(AbortSignal));
  });

  it("limits selection to four and restores the empty selection", async () => {
    const sources = (comparisonFixture as ComparisonResult).request.sources;
    localStorage.setItem(COMPARISON_KEY, JSON.stringify({ sources }));
    await mountMarket();
    await click(button("Select comparison instruments"));
    const picker = host.querySelector("dialog")!;
    for (const key of ["tiingo:NVDA", "alpha_vantage:AAPL"]) {
      await act(async () => picker.querySelector<HTMLInputElement>('input[value="' + key + '"]')!.click());
    }
    expect(picker.querySelector<HTMLInputElement>('input[value="tiingo:MSFT"]')?.disabled).toBe(true);
    await click(button("Apply selection", picker));
    expect(host.textContent).toContain("Four comparison sources are selected.");
    await click(button("Remove from comparison · AAPL · Alpha Vantage"));
    expect(host.textContent).not.toContain("Four comparison sources are selected.");
    await click(button("Clear comparison"));
    expect(host.textContent).toContain("Select one or more local instruments.");
  });

  it("adds a requested acquisition instrument without discarding other sources or changing historical tabs", async () => {
    const sources = [{ kind: "equity", code: "NVDA", provider: "tiingo" }, { kind: "equity", code: "MSFT", provider: "tiingo" }];
    localStorage.setItem(COMPARISON_KEY, JSON.stringify({ sources, hidden: ["tiingo:NVDA"], period: "5y" }));
    await mountMarket();
    const tabs = JSON.parse(localStorage.getItem(MARKET_WORKSPACE_KEY)!).tabs;
    await click(button("Data Acquisition", host.querySelector("header") ?? host));
    await click(button("Simulate completed import"));
    await click(button("View in Markets"));
    const expected = [...sources, { kind: "equity", code: "AAPL", provider: "tiingo" }];
    expect(fetchMarketComparison).toHaveBeenLastCalledWith(expected, expect.any(String), expect.any(String), expect.any(AbortSignal));
    expect(JSON.parse(localStorage.getItem(COMPARISON_KEY)!)).toMatchObject({ sources: expected, hidden: ["tiingo:NVDA"], period: "5y" });
    expect(JSON.parse(localStorage.getItem(MARKET_WORKSPACE_KEY)!).tabs).toEqual(tabs);
  });

  it("requires an explicit replacement when acquisition opens a fifth instrument", async () => {
    const sources = ["NVDA", "MSFT", "QQQ", "SPY"].map(code => ({ kind: "equity", code, provider: "tiingo" }));
    localStorage.setItem(COMPARISON_KEY, JSON.stringify({ sources, hidden: ["tiingo:NVDA", "tiingo:MSFT"] }));
    await mountMarket();
    async function requestFifth() {
      await click(button("Data Acquisition", host.querySelector("header") ?? host));
      await click(button("Simulate completed import"));
      await click(button("View in Markets"));
      return host.querySelector<HTMLDialogElement>('dialog[aria-label="Replace comparison instrument"]')!;
    }
    let dialog = await requestFifth();
    expect(dialog.textContent).toContain("AAPL");
    expect(button("Replace instrument", dialog).disabled).toBe(true);
    expect(JSON.parse(localStorage.getItem(COMPARISON_KEY)!).sources).toEqual(sources);
    await click(button("Cancel", dialog));
    expect(host.querySelector('dialog[aria-label="Replace comparison instrument"]')).toBeNull();
    expect(JSON.parse(localStorage.getItem(COMPARISON_KEY)!).sources).toEqual(sources);
    dialog = await requestFifth();
    await change(dialog.querySelector("select")!, "tiingo:NVDA");
    await click(button("Replace instrument", dialog));
    const expected = [{ kind: "equity", code: "AAPL", provider: "tiingo" }, ...sources.slice(1)];
    expect(fetchMarketComparison).toHaveBeenLastCalledWith(expected, expect.any(String), expect.any(String), expect.any(AbortSignal));
    expect(JSON.parse(localStorage.getItem(COMPARISON_KEY)!)).toMatchObject({ sources: expected, hidden: ["tiingo:MSFT"] });
    expect(host.querySelector('dialog[aria-label="Replace comparison instrument"]')).toBeNull();
  });

  it("does not display unrelated historical details when all eight historical tabs are occupied", async () => {
    const sources = [{ kind: "equity", code: "AMZN", provider: "tiingo" }];
    const tabs = [{ selection: { kind: "index", code: "SP500" } },
      ...["AAPL", "NVDA", "QQQ", "MSFT", "SPY", "TSLA", "GOOG"].map(symbol => ({ selection: { kind: "equity", symbol, provider: "tiingo" } }))];
    localStorage.setItem(COMPARISON_KEY, JSON.stringify({ sources }));
    localStorage.setItem(MARKET_WORKSPACE_KEY, JSON.stringify({ tabs, active: "fred:SP500", comparing: true }));
    await mountMarket();
    await click(button("Historical price details"));
    expect(host.querySelector("[data-market-tab]")).toBeNull();
    expect(host.querySelector("[data-comparison-chart]")).toBeTruthy();
    expect(host.textContent).toContain("Close a historical tab before opening another instrument.");
    expect(readIndexHistory).not.toHaveBeenCalled();
    expect(fetchMarketDaily).not.toHaveBeenCalled();
    await click(button("Manage historical tabs"));
    await click(button("Close tab · QQQ · Tiingo"));
    await click(button("Price comparison"));
    await click(button("Historical price details"));
    expect(active()).toBe("tiingo:AMZN");
  });

  it("moves the crosshair and shows exact observed values and gaps", async () => {
    localStorage.setItem(COMPARISON_KEY, JSON.stringify({ sources: (comparisonFixture as ComparisonResult).request.sources }));
    await mountMarket();
    const svg = host.querySelector<SVGSVGElement>("svg[data-comparison-chart]")!;
    const output = host.querySelector("output")!;
    expect(output.className).toBe("sr-only");
    Object.defineProperty(svg, "getScreenCTM", { value: () => ({ inverse: () => ({}) }) });
    Object.defineProperty(svg, "createSVGPoint", { value: () => ({ x: 0, y: 0, matrixTransform() { return { x: this.x, y: this.y }; } }) });
    await act(async () => svg.dispatchEvent(new MouseEvent("pointermove", { bubbles: true, clientX: 88, clientY: 100 })));
    expect(host.querySelector("[data-comparison-tooltip]")?.textContent).toContain("100.00");
    await act(async () => svg.dispatchEvent(new MouseEvent("pointermove", { bubbles: true, clientX: 819, clientY: 100 })));
    expect(host.querySelector("[data-comparison-tooltip]")?.textContent).toContain("Unavailable");
    expect(host.querySelector("[data-comparison-tooltip]")?.textContent).toContain("5,250");
    expect(output.className).toBe("sr-only");
    await act(async () => svg.dispatchEvent(new MouseEvent("pointerout", { bubbles: true, relatedTarget: document.body })));
    expect(host.querySelector("[data-comparison-tooltip]")).toBeNull();
    expect(output.className).toBe("sr-only");
    const slider = host.querySelector<HTMLInputElement>('input[type="range"]')!;
    await act(async () => slider.focus());
    expect(host.querySelector("[data-comparison-tooltip]")).toBeTruthy();
    expect(output.className).toBe("sr-only");
  });
  it("restores a comparison, rejects late results and retries a safe error", async () => {
    const comparison = comparisonFixture as ComparisonResult;
    localStorage.setItem(COMPARISON_KEY, JSON.stringify({ sources: comparison.request.sources, period: "custom", start: comparison.request.start, end: comparison.request.end }));
    localStorage.setItem(MARKET_WORKSPACE_KEY, JSON.stringify({ comparing: true }));
    const old = deferred<ComparisonResult>();
    vi.mocked(fetchMarketComparison).mockReturnValueOnce(old.promise).mockRejectedValueOnce(new Error("missing_history"));
    await mountMarket();
    expect(host.querySelector("[data-market-tab]")).toBeNull();
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

  it("localizes the unified selection and guidance", async () => {
    localStorage.setItem("finintel.workspace-preferences.v1", JSON.stringify({ language: "zh-CN" }));
    localStorage.setItem(COMPARISON_KEY, JSON.stringify({ sources: [] }));
    await mountMarket("市场");
    expect(button("选择对比品种")).toBeTruthy();
    expect(host.textContent).toContain("请选择一个或多个本地品种。");
  });
});
