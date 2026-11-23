// @vitest-environment jsdom
import { createHash } from "node:crypto";
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { invoke, isTauri } from "@tauri-apps/api/core";
import archived from "../../../contracts/test-fixtures/durable-backtest.json";
import { ModelJobsWorkspace } from "./ModelJobsWorkspace";
import { BacktestWorkspace } from "./BacktestWorkspace";
import { canonicalModelSha256 } from "./model-research-integrity";
import { observedBacktestWindow, sourceEligibility } from "./backtest-workflow";
import { defaultBacktestAssumptions, validBacktestDecimal, type BacktestJobRequest } from "./backtest-jobs-api";
import type { ModelJobSummary } from "./model-jobs-api";
import type { ModelResearchResult } from "./model-research-api";
import { saveWorkspacePreferences } from "./workspace-preferences";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(), isTauri: vi.fn(() => true) }));
const ipc = vi.mocked(invoke);
const dates = ["2024-01-02", "2024-01-03", "2024-01-05", "2024-01-08", "2024-01-09", "2024-01-11"];
const id = archived.request.source_job_id;
const nextId = "123e4567-e89b-12d3-a456-426614174101";
const { result: archive, ...archivedSummary } = archived;
const cancelled = { ...archivedSummary, state: "cancelled", status: "cancelled", attempts: 1,
  output_sha256: null, source_snapshot_sha256: null, progress: 0, cancel_requested: 1 };
const queued = { ...cancelled, state: "queued", status: "queued", attempts: 0, cancel_requested: 0 };
const running = { ...queued, state: "running", status: "running", attempts: 1, progress: 5 };

async function sma(sourceId = id, options: {
  override?: boolean; verified?: boolean; revision?: string; code?: string; observations?: string[];
} = {}) {
  const { override = false, verified = true, revision = "sma-crossover-observed-close-v1", code = "SP500", observations = dates } = options;
  const lineage: ModelResearchResult["lineage"] = {
    model_id: "builtin.sma_crossover", model_version: "1.0.0", implementation_revision: revision,
    parameters: { fast_window: 2, slow_window: 3 },
    source_identity: { instrument_kind: "index", code, provider: "fred", adjustment: "price_index",
      return_semantics: "price_index", frequency: "daily", units: "index_points" },
    source_snapshot_sha256: "a".repeat(64), analysis_calculation_version: "index-analytics-v2",
    requested_start: dates[0], requested_end: dates.at(-1)!,
    first_observed_session: observations[0], last_observed_session: observations.at(-1)!,
    quality_policy: override ? "allow_unverified" : "require_complete", quality_status: verified ? "verified" : "unknown",
    quality_verified: verified, point_in_time_membership: "not_used", dividends_included: false,
    corporate_action_adjustment: "none_index_price_level",
  };
  const points: ModelResearchResult["points"] = observations.map((session_date, i) => ({
    session_date, close: "100", fast_sma: i >= 1 ? "100" : null, slow_sma: i >= 2 ? "100" : null,
    signal_after_close: i >= 2 ? "flat" : null,
  }));
  const result: ModelResearchResult = {
    execution_contract: "model-research-evaluation-v1", lineage, points, observations: points.length,
    output_sha256: await canonicalModelSha256(points), reproducibility_sha256: await canonicalModelSha256(lineage),
    evaluated_at_utc: archived.finished_at, status: "completed", read_only: true, no_network: true,
    no_synthetic_sessions: true, signal_after_close_only: true, backtest_validated: false, persistent_job: false,
    quality_warning: verified ? null : "Unverified data: research-only results, not verified for backtesting",
  };
  const summary: ModelJobSummary = {
    id: sourceId, model_id: "builtin.sma_crossover", model_version: "1.0.0", state: "completed", status: "completed",
    request_sha256: await canonicalModelSha256({ code, revision, override, observations }), attempts: 1, progress: 100,
    cancel_requested: 0, output_sha256: result.output_sha256, source_snapshot_sha256: lineage.source_snapshot_sha256,
    error_code: null, created_at: archived.created_at, started_at: archived.started_at,
    updated_at: archived.updated_at, finished_at: archived.finished_at,
  };
  return { summary, detail: { ...summary, result } };
}

let host: HTMLDivElement;
let root: Root;
let source: Awaited<ReturnType<typeof sma>>;
let history: { total: number; items: unknown[] };
let detail: unknown;

beforeEach(async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("crypto", { subtle: { digest: async (_: string, bytes: Uint8Array) => createHash("sha256").update(bytes).digest() } });
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value) });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  source = await sma();
  history = { total: 0, items: [] };
  detail = archivedSummary;
  ipc.mockReset();
  vi.mocked(isTauri).mockReturnValue(true);
  ipc.mockImplementation(async (command, args) => {
    switch (command) {
      case "model_jobs_list": return { total: 1, items: [source.summary] };
      case "model_jobs_get": return (args as { includeResult?: boolean }).includeResult ? source.detail : source.summary;
      case "backtest_jobs_list": return history;
      case "backtest_jobs_get": return (args as { includeResult?: boolean }).includeResult ? archived : detail;
      case "backtest_jobs_cancel": return { state: "cancel_requested" };
      case "backtest_jobs_retry": return { ...queued, attempts: 1 };
      case "backtest_jobs_create": {
        const { request } = args as { request: BacktestJobRequest };
        return { ...queued, request, request_sha256: await canonicalModelSha256(request) };
      }
      default: throw new Error("Unexpected invocation");
    }
  });
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const mount = (element: ReactNode) => act(async () => root.render(element));
function button(text: string, within: ParentNode = host) {
  const match = [...within.querySelectorAll("button")].find(node => node.textContent?.trim() === text);
  if (!match) throw new Error("Button not found: " + text);
  return match;
}
const click = (text: string, within: ParentNode = host) => act(async () => button(text, within).click());
function control<T extends HTMLInputElement | HTMLSelectElement>(text: string): T {
  const match = [...host.querySelectorAll("label")].find(node => node.firstChild?.textContent?.trim() === text);
  if (!match?.control) throw new Error("Control not found: " + text);
  return match.control as T;
}
const select = (name: string, value: string) => act(async () => {
  const element = control<HTMLSelectElement>(name);
  element.value = value;
  element.dispatchEvent(new Event("change", { bubbles: true }));
});
const edit = (name: string, value: string) => act(async () => {
  const element = control<HTMLInputElement>(name);
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(element, value);
  element.dispatchEvent(new Event("input", { bubbles: true }));
});
const form = () => host.querySelector("form")!;
const calls = (command: string) => ipc.mock.calls.filter(([name]) => name === command);
const enterWindow = async () => {
  await select("Training end", dates[2]);
  await select("Test end", dates[5]);
};
function deferred<T>() {
  let resolve!: (value: T) => void;
  return { promise: new Promise<T>(finish => { resolve = finish; }), resolve };
}

describe("mounted desktop backtest configuration", () => {
  it("opens from a verified legacy SMA result and queues only after the user confirms exact inputs", async () => {
    await mount(<ModelJobsWorkspace />);
    await click("Inspect");
    await click("Create Backtest");
    expect(button("Backtesting").getAttribute("aria-selected")).toBe("true");
    expect(control("Completed SMA source job").value).toBe(id);
    expect(calls("model_jobs_get").filter(([, args]) => (args as { includeResult: boolean }).includeResult)).toHaveLength(2);
    expect(host.textContent).toContain("Eligible stored SMA source");
    expect(host.textContent).toContain("SP500 · FRED");
    expect(calls("backtest_jobs_create")).toHaveLength(0);

    await enterWindow();
    expect(host.querySelector('output[aria-label="Test start"]')?.textContent).toBe(dates[3]);
    expect(host.textContent).toContain("Training observations · 3 · Test sessions · 3");
    expect(control("Initial capital").value).toBe("10000");
    expect(control("Position fraction").value).toBe("1");
    expect(control("Slippage (basis points)").value).toBe("0");
    expect(control("Annual risk-free rate").value).toBe("0");
    await edit("Commission (basis points)", "-1");
    expect(control("Commission (basis points)").getAttribute("aria-invalid")).toBe("true");
    expect(button("Queue backtest").disabled).toBe(true);
    await edit("Commission (basis points)", "12.340000");
    await click("Queue backtest");
    const params = (calls("backtest_jobs_create")[0][1] as { request: BacktestJobRequest }).request;
    expect(params).toEqual({
      contract_version: "backtest-job-request-v1", source_job_id: id,
      source_request_sha256: source.summary.request_sha256, source_output_sha256: source.detail.result.output_sha256,
      source_lineage_sha256: source.detail.result.reproducibility_sha256,
      source_implementation_revision: "sma-crossover-observed-close-v1", engine_revision: "sma-next-observed-close-v1",
      simulation: { model_id: "builtin.sma_crossover", model_version: "1.0.0", index_code: "SP500",
        expected_snapshot_sha256: source.summary.source_snapshot_sha256, source_semantics: "fred_price_index_level",
        point_in_time_membership: "not_used", training_end: dates[2], test_start: dates[3], test_end: dates[5],
        assumptions: { ...defaultBacktestAssumptions, commission_bps: "12.340000" } },
    });
    expect(host.textContent).toContain("Backtest queued");
    expect(host.textContent).not.toContain("Verified backtest result");

    await click("New Backtest");
    expect(calls("model_jobs_get").filter(([, args]) => (args as { includeResult: boolean }).includeResult)).toHaveLength(3);
    expect(control("Commission (basis points)").value).toBe("12.340000");
    expect(control("Test end").value).toBe(dates[5]);
    await click("Queue backtest");
    expect(calls("backtest_jobs_create")).toHaveLength(2);
  });

  it("does not silently move an invalidated test end or double-submit", async () => {
    await mount(<BacktestWorkspace initialSource={source.summary} />);
    await select("Training end", dates[2]);
    await select("Test end", dates[4]);
    await select("Training end", dates[3]);
    expect(control("Test end").value).toBe(dates[4]);
    expect(button("Queue backtest").disabled).toBe(true);
    await select("Test end", dates[5]);

    const pending = deferred<unknown>();
    const previous = ipc.getMockImplementation()!;
    ipc.mockImplementation((name, args) => name === "backtest_jobs_create" ? pending.promise : previous(name, args));
    await act(async () => {
      form().dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      form().dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    expect(calls("backtest_jobs_create")).toHaveLength(1);
    await act(async () => pending.resolve(null));
    expect(host.textContent).toContain("failed integrity verification");
  });

  it.each([true, false])("rejects an original override with verified evidence=%s", async verified => {
    source = await sma(id, { override: true, verified });
    await mount(<BacktestWorkspace initialSource={source.summary} />);
    expect(host.textContent).toContain("Backtesting requires an original verified result created under Require complete.");
    expect(host.querySelector("form")).toBeNull();
    expect(calls("backtest_jobs_create")).toHaveLength(0);
  });

  it.each(["revision", "index", "corruption", "history", "failed"] as const)("blocks an invalid SMA source (%s)", async kind => {
    source = await sma(id, kind === "revision" ? { revision: "other" } : kind === "index" ? { code: "VIX" } :
      kind === "history" ? { observations: dates.slice(0, 4) } : {});
    if (kind === "corruption") source.detail.result.output_sha256 = "f".repeat(64);
    if (kind === "failed") source.detail.state = "failed";
    await mount(<BacktestWorkspace initialSource={source.summary} />);
    expect(host.querySelector("form")).toBeNull();
    expect(host.querySelector('[role="alert"]')).toBeTruthy();
  });

  it("drops a delayed source result after selecting a different source job", async () => {
    const first = source;
    const second = await sma(nextId, { code: "DJIA" });
    const delayed = deferred<unknown>();
    const previous = ipc.getMockImplementation()!;
    ipc.mockImplementation((name, args) => {
      if (name === "model_jobs_list") return Promise.resolve({ total: 2, items: [first.summary, second.summary] });
      if (name === "model_jobs_get") return (args as { jobId: string }).jobId === id ? delayed.promise : Promise.resolve(second.detail);
      return previous(name, args);
    });
    await mount(<BacktestWorkspace initialSource={first.summary} />);
    await select("Completed SMA source job", nextId);
    expect(host.textContent).toContain("DJIA · FRED");
    await act(async () => delayed.resolve(first.detail));
    expect(host.textContent).not.toContain("SP500 · FRED");
    expect(host.textContent).toContain("DJIA · FRED");
  });

  it("preserves the form on sanitized source revision and capacity failures", async () => {
    const previous = ipc.getMockImplementation()!;
    let failure = "source_changed";
    ipc.mockImplementation((name, args) => name === "backtest_jobs_create" ? Promise.reject(failure) : previous(name, args));
    await mount(<BacktestWorkspace initialSource={source.summary} />);
    await enterWindow();
    await click("Queue backtest");
    expect(host.textContent).toContain("The original source snapshot no longer matches the local data.");
    expect(control("Training end").value).toBe(dates[2]);
    failure = "queue_full";
    await click("Queue backtest");
    expect(host.textContent).toContain("The local job queue is full.");
    expect(calls("backtest_jobs_create")).toHaveLength(2);
  });

  it("localizes the workflow and keeps browser mode read-only", async () => {
    saveWorkspacePreferences({ language: "zh-CN", theme: "light", reduceMotion: true });
    await mount(<BacktestWorkspace initialSource={source.summary} />);
    expect(host.textContent).toContain("已保存的 SMA 来源符合条件");
    expect(button("提交回测任务").disabled).toBe(true);
    vi.mocked(isTauri).mockReturnValue(false);
    ipc.mockClear();
    await mount(<BacktestWorkspace />);
    expect(host.textContent).toContain("回测需要受管理的 FinIntel 桌面版。");
    expect(ipc).not.toHaveBeenCalled();
  });
});

describe("mounted backtest lifecycle", () => {
  it("shows the durable cancellation acknowledgement even when the immediate refresh fails", async () => {
    history = { total: 1, items: [running] };
    detail = running;
    await mount(<BacktestWorkspace />);
    await click("Inspect backtest");
    const previous = ipc.getMockImplementation()!;
    ipc.mockImplementation((name, args) => name === "backtest_jobs_get" ? Promise.reject("backend_unavailable") : previous(name, args));
    await click("Cancel job");
    expect(calls("backtest_jobs_cancel")).toHaveLength(1);
    expect(host.querySelector('[aria-label="Backtest details"]')?.textContent).toContain("cancel_requested");
    expect(host.textContent).not.toContain("Cancel job");
    expect(host.textContent).not.toContain("Verified backtest result");
  });

  it.each(["completed", "failed"] as const)("waits for a full summary when cancellation races with %s", async status => {
    history = { total: 1, items: [running] };
    detail = running;
    await mount(<BacktestWorkspace />);
    await click("Inspect backtest");
    const previous = ipc.getMockImplementation()!;
    let refreshFails = true;
    ipc.mockImplementation((name, args) => {
      if (name === "backtest_jobs_cancel") return Promise.resolve({ state: status });
      if (name === "backtest_jobs_get" && refreshFails) return Promise.reject("backend_unavailable");
      return previous(name, args);
    });
    await click("Cancel job");
    const section = host.querySelector('[aria-label="Backtest details"]')!;
    expect(section.querySelector('[role="status"] strong')?.textContent).toBe("running");
    expect(section.querySelector('[role="alert"]')?.textContent).toContain("Refresh details to see its final status and results.");
    expect(section.textContent).not.toContain("View result");
    expect(section.textContent).not.toContain("Retry original request");
    expect(section.textContent).not.toContain("Cancel job");

    refreshFails = false;
    detail = status === "completed" ? archivedSummary : { ...running, state: "failed", status: "failed", error_code: "worker_failure" };
    await click("Refresh details", section);
    expect(section.querySelector('[role="status"] strong')?.textContent).toBe(status);
    expect(section.querySelector('[role="alert"]')).toBeNull();
    expect(button(status === "completed" ? "View result" : "Retry original request", section)).toBeTruthy();
  });

  it("polls active jobs, cancels queued work and enforces retry eligibility and attempt limits", async () => {
    vi.useFakeTimers();
    history = { total: 1, items: [queued] };
    detail = queued;
    const previous = ipc.getMockImplementation()!;
    ipc.mockImplementation((name, args) => {
      if (name === "backtest_jobs_cancel") {
        detail = cancelled;
        return Promise.resolve({ state: "cancelled" });
      }
      return previous(name, args);
    });
    await mount(<BacktestWorkspace />);
    await click("Inspect backtest");
    await click("Cancel job");
    expect(host.querySelector('[aria-label="Backtest details"]')?.textContent).toContain("cancelled");
    await click("Retry original request");
    expect(calls("backtest_jobs_retry")).toHaveLength(1);
    const reads = calls("backtest_jobs_get").length;
    detail = { ...cancelled, state: "failed", status: "failed", attempts: 3, cancel_requested: 0, error_code: "source_changed" };
    await act(async () => vi.advanceTimersByTime(5000));
    expect(calls("backtest_jobs_get").length).toBeGreaterThan(reads);
    expect(host.textContent).toContain("3/3");
    expect(host.textContent).not.toContain("Retry original request");
    expect(host.textContent).toContain("The original source snapshot no longer matches the local data.");
    await mount(<div />);
    const unmounted = ipc.mock.calls.length;
    await act(async () => vi.advanceTimersByTime(5000));
    expect(ipc.mock.calls.length).toBe(unmounted);
  });

  it("retrieves a verified archived result on demand and clears it after integrity failure", async () => {
    history = { total: 1, items: [archivedSummary] };
    await mount(<BacktestWorkspace />);
    await click("Inspect backtest");
    expect(calls("backtest_jobs_get").some(([, args]) => (args as { includeResult?: boolean }).includeResult)).toBe(false);
    await click("View result");
    expect(host.textContent).toContain("Verified backtest result");
    expect(host.textContent).toContain(archive.report.input_sha256);
    expect(host.querySelector(`[title="${archive.report.metrics.cumulative_return}"]`)).toBeTruthy();
    const previous = ipc.getMockImplementation()!;
    const corrupted = structuredClone(archived);
    corrupted.result.report.rows[0].equity_after_cost = "1";
    ipc.mockImplementation((name, args) => name === "backtest_jobs_get" ? Promise.resolve(corrupted) : previous(name, args));
    await click("View result");
    expect(host.textContent).not.toContain("Verified backtest result");
    expect(host.textContent).toContain("The saved backtest failed integrity verification.");
  });

  it.each(["job", "status", "page", "tab"] as const)("fences a delayed verified result after %s navigation", async change => {
    const items = [archivedSummary, ...Array.from({ length: 9 }, (_, i) => ({
      ...archivedSummary, id: `123e4567-e89b-12d3-a456-4266141742${String(i).padStart(2, "0")}`,
    }))];
    const pending = deferred<unknown>();
    const previous = ipc.getMockImplementation()!;
    ipc.mockImplementation((name, args) => {
      if (name === "backtest_jobs_list") {
        const { offset } = args as { offset: number };
        return Promise.resolve({ total: 11, items: offset ? [items[1]] : items });
      }
      if (name === "backtest_jobs_get") {
        const { jobId, includeResult } = args as { jobId: string; includeResult: boolean };
        return includeResult ? pending.promise : Promise.resolve({ ...archivedSummary, id: jobId });
      }
      return previous(name, args);
    });
    await mount(<ModelJobsWorkspace />);
    await click("Backtesting");
    expect(host.querySelectorAll('table[aria-label="Backtest History"] tbody tr')).toHaveLength(10);
    await click("Inspect backtest");
    await click("View result");
    if (change === "job") await act(async () => {
      [...host.querySelectorAll("button")].filter(node => node.textContent === "Inspect backtest")[1].click();
    });
    if (change === "status") {
      await select("Filter job status", "completed");
      expect(ipc).toHaveBeenLastCalledWith("backtest_jobs_list", { limit: 10, offset: 0, status: "completed" });
    }
    if (change === "page") {
      await click("Next", host.querySelector('[aria-label="Backtest history pages"]')!);
      expect(ipc).toHaveBeenLastCalledWith("backtest_jobs_list", { limit: 10, offset: 10 });
    }
    if (change === "tab") await click("Legacy SMA Jobs");
    await act(async () => pending.resolve(archived));
    expect(host.textContent).not.toContain("Verified backtest result");
  });
});

describe("backtest eligibility and exact input boundaries", () => {
  it("uses stored session order and rejects unsupported models, duplicate sessions and bad date ranges", () => {
    const { summary, detail: { result } } = source;
    expect(sourceEligibility(summary, result)).toBeUndefined();
    expect(sourceEligibility({ ...summary, model_id: "unknown" } as unknown as ModelJobSummary, result)).toContain("ineligible");
    const duplicate = { ...result, points: [result.points[0], ...result.points.slice(0, -1), result.points.at(-1)!] };
    expect(sourceEligibility(summary, duplicate)).toContain("integrity");
    expect(observedBacktestWindow(result, dates[1], dates[5])).toBeUndefined();
    expect(observedBacktestWindow(result, dates[2], dates[3])).toBeUndefined();
    expect(observedBacktestWindow(result, dates[2], "2024-01-12")).toBeUndefined();
    expect(observedBacktestWindow(result, dates[2], dates[4])).toMatchObject({ test_start: dates[3], testCount: 2, trainingCount: 3 });
  });

  it("validates fixed-point strings at the inclusive and exclusive bounds without rounding", () => {
    expect(validBacktestDecimal("initial_capital", "1000000000000")).toBe(true);
    expect(validBacktestDecimal("initial_capital", "1000000000000.000001")).toBe(false);
    expect(validBacktestDecimal("position_fraction", "0")).toBe(false);
    expect(validBacktestDecimal("position_fraction", "0.000001")).toBe(true);
    expect(validBacktestDecimal("position_fraction", "0.0000001")).toBe(false);
    expect(validBacktestDecimal("commission_bps", "-0.1")).toBe(false);
    expect(validBacktestDecimal("slippage_bps", "1000.000000")).toBe(true);
    expect(validBacktestDecimal("risk_free_rate_annual", "-0.99")).toBe(true);
    expect(validBacktestDecimal("risk_free_rate_annual", "-0.990001")).toBe(false);
  });
});
