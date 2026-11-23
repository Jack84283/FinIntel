// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AnalysisLibraryPanel } from "./AnalysisLibraryPanel";
import { AnalysisJobsHistory } from "./AnalysisJobsHistory";
import { ModelJobsWorkspace } from "./ModelJobsWorkspace";
import { AnalysisJobError, type AnalysisJobSummary, type AnalysisResult } from "./analysis-jobs-api";
import { type AnalyticsResult } from "./index-intelligence-api";
import { type IndexCatalogRecord } from "./index-history-api";
import { saveWorkspacePreferences } from "./workspace-preferences";
import fixture from "../../../contracts/test-fixtures/analysis-relative-strength.json";

const api = vi.hoisted(() => ({
  native: true, analytics: vi.fn(), queue: vi.fn(), list: vi.fn(), get: vi.fn(), cancel: vi.fn(), retry: vi.fn(),
}));
vi.mock("@tauri-apps/api/core", () => ({ isTauri: () => api.native, invoke: vi.fn() }));
vi.mock("./index-intelligence-api", async importOriginal => ({
  ...await importOriginal<typeof import("./index-intelligence-api")>(), fetchIndexAnalytics: api.analytics,
}));
vi.mock("./analysis-jobs-api", async importOriginal => ({
  ...await importOriginal<typeof import("./analysis-jobs-api")>(), queueAnalysisJob: api.queue,
  listAnalysisJobs: api.list, getAnalysisJob: api.get, cancelAnalysisJob: api.cancel, retryAnalysisJob: api.retry,
}));

const requestWindow = { start: "2024-01-01", end: "2024-03-01" };
const sourceCodes = ["SP500", "DJIA", "NDX", "NASDAQCOM", "VIX"] as const;
const catalog = sourceCodes.map(code => ({
  code, provider: "fred", series_type: code === "VIX" ? "volatility" : "price", sessions: 500,
  local_first_session: "2023-01-01", local_last_session: "2024-03-01",
})) as IndexCatalogRecord[];
const sha = (code: string) => (code === "SP500" ? "a" : code === "DJIA" ? "b" : "c").repeat(64);
const source = (code: string, options: { verified?: boolean; count?: number; start?: string; end?: string } = {}) => {
  const { verified = true, count = 30, start = requestWindow.start, end = requestWindow.end } = options;
  const days = Array.from({ length: count }, (_, index) => new Date(Date.UTC(2024, 0, index + 1)).toISOString().slice(0, 10));
  return {
    identity: { instrument_kind: "index", code, provider: "fred", adjustment: "price_index", return_semantics: "price_index", frequency: "daily", units: "index_points" },
    request: { start, end }, source: { snapshot_sha256: sha(code) }, quality: { quality_verified: verified },
    observations: count, first_session: days[0], last_session: days.at(-1), points: days.map(session_date => ({ session_date })),
  } as unknown as AnalyticsResult;
};

const summary = fixture as unknown as AnalysisJobSummary;
const result = fixture.result as unknown as AnalysisResult;
const job = (id: string, state: AnalysisJobSummary["state"] = "queued", attempts = 1) => ({
  ...summary, id, state, status: state, attempts,
}) as AnalysisJobSummary;
const panel = <AnalysisLibraryPanel primary="SP500" {...requestWindow} catalog={catalog} catalogLoaded />;

function promise<T>() {
  let resolve!: (value: T) => void;
  return { value: new Promise<T>(finish => { resolve = finish; }), resolve };
}

let host: HTMLDivElement;
let root: Root;
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
  });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  vi.resetAllMocks();
  api.native = true;
  api.analytics.mockImplementation(({ code, start, end }) => Promise.resolve(source(code, { start, end })));
  api.get.mockImplementation((id: string) => Promise.resolve(job(id)));
  api.list.mockResolvedValue({ items: [], total: 0 });
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const mount = (element: React.ReactNode) => act(async () => root.render(element));
function button(text: string) {
  const found = [...host.querySelectorAll("button")].find(node => node.textContent?.trim() === text);
  if (!found) throw new Error("Button not found: " + text);
  return found;
}
function control<T extends HTMLInputElement | HTMLSelectElement>(name: string): T {
  const label = [...host.querySelectorAll("label")].find(node => node.firstChild?.textContent?.trim() === name);
  if (!label?.control) throw new Error("Control not found: " + name);
  return label.control as T;
}
const click = (text: string) => act(async () => button(text).click());
const select = (name: string, value: string) => act(async () => {
  const node = control<HTMLSelectElement>(name);
  node.value = value;
  node.dispatchEvent(new Event("change", { bubbles: true }));
});
const type = (name: string, value: string) => act(async () => {
  const node = control<HTMLInputElement>(name);
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(node, value);
  node.dispatchEvent(new Event("input", { bubbles: true }));
});

describe("mounted Analysis Library workflow", () => {
  it("shows all model families and approved defaults, rejects invalid fields, and never executes on selection", async () => {
    await mount(panel);
    expect([...control<HTMLSelectElement>("Analysis model").options].map(option => option.value)).toEqual([
      "builtin.ema_trend", "builtin.momentum", "builtin.relative_strength", "builtin.rsi_state",
    ]);
    expect(control("Fast EMA window").value).toBe("12");
    expect(control("Slow EMA window").value).toBe("26");
    expect(control<HTMLSelectElement>("Primary source").querySelector('[value="VIX"]')).toBeNull();
    await type("Fast EMA window", "100");
    expect(control("Slow EMA window").getAttribute("aria-invalid")).toBe("true");
    expect(button("Validate local sources").disabled).toBe(true);
    await select("Analysis model", "builtin.momentum");
    expect(control("Observed-session lookback").value).toBe("63");
    await select("Analysis model", "builtin.rsi_state");
    expect(["RSI calculation window", "Lower RSI threshold", "Upper RSI threshold"].map(label => control(label).value)).toEqual(["14", "30", "70"]);
    await click("Risk");
    expect(control("Analysis model").value).toBe("builtin.risk_profile");
    expect(control("Risk calculation window").value).toBe("63");
    expect(control("VaR and ES history window").value).toBe("252");
    await click("Market Regime");
    expect(control("Analysis model").value).toBe("builtin.market_regime");
    expect(control("Short SMA window").value).toBe("50");
    expect(control("Long SMA window").value).toBe("200");
    expect(control("Neutrality band (fraction, 0.02 = 2%)").value).toBe("0.02");
    expect(host.textContent).toContain("820");
    expect(api.analytics).not.toHaveBeenCalled();
    expect(api.queue).not.toHaveBeenCalled();
  });

  it("queues exact independently read pins in benchmark order and treats acknowledgement as queued", async () => {
    api.queue.mockResolvedValue(job("queued-id"));
    const open = vi.fn();
    await mount(<AnalysisLibraryPanel primary="SP500" {...requestWindow} catalog={catalog} catalogLoaded onOpenJob={open} />);
    await select("Analysis model", "builtin.relative_strength");
    await select("Additional index 1", "DJIA");
    await click("Move earlier");
    expect(control("Benchmark").value).toBe("DJIA");
    await click("Validate local sources");
    expect(api.analytics.mock.calls.map(([query]) => query.code)).toEqual(["DJIA", "SP500"]);
    expect(api.analytics.mock.calls.every(([query]) => query.comparisonCode === null)).toBe(true);
    await click("Queue analysis job");
    expect(api.queue).toHaveBeenCalledWith({
      contract_version: "analysis-model-request-v1", model_id: "builtin.relative_strength",
      model_version: "1.0.0", implementation_revision: "relative-strength-common-baseline-v1",
      parameters: {}, quality_policy: "require_complete", window: requestWindow,
      sources: ["DJIA", "SP500"].map(code => ({ identity: source(code).identity, snapshot_sha256: sha(code) })),
    });
    expect(host.textContent).toContain("queued-id");
    expect(host.textContent).not.toContain("Verified research result");
    expect(api.get).not.toHaveBeenCalledWith("queued-id", true);
    await click("Open job details");
    expect(open).toHaveBeenCalledWith("queued-id");
  });

  it("queues a second job with unchanged inputs only after obtaining fresh source pins", async () => {
    const first = source("SP500");
    const freshPin = "d".repeat(64);
    api.analytics.mockResolvedValueOnce(first)
      .mockResolvedValueOnce({ ...first, source: { ...first.source, snapshot_sha256: freshPin } });
    api.queue.mockResolvedValueOnce(job("first-job")).mockResolvedValueOnce(job("second-job"));
    const open = vi.fn();
    await mount(<AnalysisLibraryPanel primary="SP500" {...requestWindow} catalog={catalog} catalogLoaded onOpenJob={open} />);
    await type("Fast EMA window", "8");
    await click("Validate local sources");
    await click("Queue analysis job");
    await click("Open job details");
    expect(open).toHaveBeenCalledWith("first-job");

    await click("Run again");
    expect(host.textContent).not.toContain("first-job");
    expect(host.textContent).not.toContain("Queue analysis job");
    expect(control("Fast EMA window").value).toBe("8");
    expect(api.analytics).toHaveBeenCalledOnce();
    expect(api.queue).toHaveBeenCalledOnce();

    await click("Validate local sources");
    expect(api.analytics.mock.calls[1]).toEqual(api.analytics.mock.calls[0]);
    await click("Queue analysis job");
    const [firstRequest] = api.queue.mock.calls[0];
    expect(api.queue).toHaveBeenCalledTimes(2);
    expect(api.queue).toHaveBeenLastCalledWith({
      ...firstRequest, sources: [{ identity: first.identity, snapshot_sha256: freshPin }],
    });
    expect(host.textContent).toContain("second-job");
  });

  it("submits contract parameters and blocks strict quality until an explicit override", async () => {
    api.analytics.mockResolvedValue(source("SP500", { verified: false }));
    api.queue.mockResolvedValue(job("queued-id"));
    await mount(panel);
    await type("Fast EMA window", "8");
    await click("Validate local sources");
    expect(button("Queue analysis job").disabled).toBe(true);
    await select("Data quality policy", "allow_unverified");
    expect(host.textContent).toContain("Unverified evidence is allowed");
    await click("Validate local sources");
    await click("Queue analysis job");
    expect(api.queue).toHaveBeenCalledWith(expect.objectContaining({
      model_id: "builtin.ema_trend", parameters: { fast_window: 8, slow_window: 26 }, quality_policy: "allow_unverified",
    }));
  });

  it.each(["model", "source", "date", "quality", "parameters", "unmount"])("drops a delayed source response after %s changes", async change => {
    const old = promise<AnalyticsResult>();
    api.analytics.mockReturnValueOnce(old.value);
    await mount(panel);
    await click("Validate local sources");
    if (change === "model") await select("Analysis model", "builtin.momentum");
    if (change === "source") await select("Primary source", "DJIA");
    if (change === "date") await click("1 year");
    if (change === "quality") await select("Data quality policy", "allow_unverified");
    if (change === "parameters") await type("Fast EMA window", "8");
    if (change === "unmount") await mount(<div>Other view</div>);
    if (change !== "unmount") await click("Validate local sources");
    const before = host.textContent;
    await act(async () => old.resolve(source("SP500")));
    expect(host.textContent).toBe(before);
    expect(api.queue).not.toHaveBeenCalled();
  });

  it("blocks windows above 2,000 observations and never truncates the all-data preset", async () => {
    const wideCatalog = catalog.map(entry => entry.code === "SP500" ? {
      ...entry, sessions: 2501, local_first_session: "2010-01-01",
    } : entry);
    await mount(<AnalysisLibraryPanel primary="SP500" {...requestWindow} catalog={wideCatalog} catalogLoaded />);
    await click("All available data");
    expect(button("Validate local sources").disabled).toBe(true);
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("2,000");
    expect(api.analytics).not.toHaveBeenCalled();
    expect(api.queue).not.toHaveBeenCalled();
  });

  it("requires an explicit new preview after the backend rejects a changed source", async () => {
    api.queue.mockRejectedValueOnce(new AnalysisJobError("source_changed"));
    await mount(panel);
    await click("Validate local sources");
    await click("Queue analysis job");
    expect(host.textContent).toContain("The original source snapshot has changed.");
    expect(api.analytics).toHaveBeenCalledOnce();
    expect(api.queue).toHaveBeenCalledOnce();
    expect(host.textContent).not.toContain("Review analysis inputs");
  });

  it("links to Historical Data when a selected index has no observations", async () => {
    const open = vi.fn();
    await mount(<AnalysisLibraryPanel primary="SP500" {...requestWindow} catalog={[]} catalogLoaded onOpenAcquisition={open} />);
    expect(button("Validate local sources").disabled).toBe(true);
    await click("Open Historical Data");
    expect(open).toHaveBeenCalledOnce();
  });

  it("supports keyboard focus movement between model family tabs", async () => {
    await mount(panel);
    const trend = button("Trend & Momentum");
    await act(async () => {
      trend.focus();
      trend.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    });
    expect(document.activeElement).toBe(button("Risk"));
    expect(button("Risk").getAttribute("role")).toBe("tab");
  });

  it("localizes mounted controls and keeps browser mode disabled", async () => {
    api.native = false;
    saveWorkspacePreferences({ language: "zh-CN", theme: "light", reduceMotion: true });
    await mount(panel);
    expect(host.textContent).toContain("分析模型库");
    expect(host.textContent).toContain("分析模型库任务需要在受管理的 FinIntel 桌面版中执行。");
    expect(button("校验本地来源").disabled).toBe(true);
    expect(document.documentElement.dataset.reduceMotion).toBe("true");
  });
});

describe("mounted Analysis Library job history", () => {
  it("preserves the SMA workspace, then switches to server-filtered history with ten-row pages", async () => {
    const records = Array.from({ length: 10 }, (_, index) => job("job-" + index));
    api.list.mockResolvedValue({ total: 11, items: records });
    await mount(<ModelJobsWorkspace />);
    expect(host.textContent).toContain("Queue built-in model");
    await click("Analysis Library Jobs");
    expect(host.querySelectorAll("tbody tr")).toHaveLength(10);
    await click("Next");
    expect(api.list).toHaveBeenLastCalledWith(2, "all");
    await select("Filter job status", "running");
    expect(api.list).toHaveBeenLastCalledWith(1, "running");
  });

  it("refreshes, cancels and retries using durable status and enforces the attempt cap", async () => {
    const active = job("active");
    api.list.mockResolvedValue({ total: 1, items: [active] });
    api.get.mockResolvedValue(active);
    await mount(<AnalysisJobsHistory />);
    await click("Open job details");
    await click("Refresh details");
    expect(api.get).toHaveBeenLastCalledWith("active");
    api.get.mockResolvedValue({ ...active, state: "running", status: "cancel_requested" });
    await click("Cancel job");
    expect(api.cancel).toHaveBeenCalledWith("active");
    expect(host.textContent).toContain("cancel_requested");
    api.get.mockResolvedValue(job("active", "cancelled", 2));
    await click("Refresh details");
    api.get.mockResolvedValue(job("active", "failed", 3));
    await click("Retry original request");
    expect(api.retry).toHaveBeenCalledWith("active");
    expect(host.textContent).toContain("3/3");
    expect(host.textContent).not.toContain("Retry original request");
  });

  it("retains the selected page after an earlier history request resolves late", async () => {
    vi.useFakeTimers();
    const earlier = promise<{ total: number; items: AnalysisJobSummary[] }>();
    api.list.mockResolvedValueOnce({ total: 11, items: [job("page-one")] })
      .mockReturnValueOnce(earlier.value)
      .mockResolvedValueOnce({ total: 11, items: [job("page-two")] });
    await mount(<AnalysisJobsHistory />);
    await act(async () => vi.advanceTimersByTime(5000));
    await click("Next");
    await act(async () => earlier.resolve({ total: 11, items: [job("outdated")] }));
    await click("Open job details");
    expect(api.get).toHaveBeenLastCalledWith("page-two");
  });

  it("renders archived validated observations and evidence and rejects an integrity failure", async () => {
    api.get.mockImplementation((id, include) => Promise.resolve({ ...job(id, "completed"), ...(include && { result }) }));
    await mount(<AnalysisJobsHistory initialJobId="archived" />);
    await click("View result");
    expect(api.get).toHaveBeenLastCalledWith("archived", true);
    expect(host.textContent).toContain("Verified research result");
    expect(host.textContent).toContain(result.reproducibility_sha256);
    expect(host.textContent).toContain(result.points[0].session_date);
    api.get.mockRejectedValueOnce(new AnalysisJobError("invalid_response"));
    await click("View result");
    expect(host.textContent).not.toContain("Verified research result");
    expect(host.textContent).toContain("failed integrity validation");
  });

  it.each(["job", "filter", "page", "unmount"])("discards a delayed completed result after %s navigation", async change => {
    const original = job("old", "completed");
    const newer = job("new", "completed");
    const pending = promise<AnalysisJobSummary & { result: AnalysisResult }>();
    api.list.mockResolvedValue({ total: 11, items: [original, newer] });
    api.get.mockImplementation((id, include) => include ? pending.value : Promise.resolve(id === "old" ? original : newer));
    await mount(<AnalysisJobsHistory initialJobId="old" />);
    await click("View result");
    if (change === "job") await act(async () => {
      [...host.querySelectorAll("button")].filter(node => node.textContent === "Open job details")[1].click();
    });
    if (change === "filter") await select("Filter job status", "completed");
    if (change === "page") await click("Next");
    if (change === "unmount") await mount(<div>Other view</div>);
    await act(async () => pending.resolve({ ...original, result }));
    expect(host.textContent).not.toContain("Verified research result");
  });
});
