// @vitest-environment jsdom
import { createHash } from "node:crypto";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { invoke, isTauri } from "@tauri-apps/api/core";
import archived from "../../../contracts/test-fixtures/durable-backtest.json";
import { BacktestResultView } from "./BacktestResultView";
import { BacktestExport } from "./BacktestExport";
import { getBacktestJob } from "./backtest-jobs-api";
import { canonicalModelSha256 } from "./model-research-integrity";
import { NotificationProvider } from "./notifications";
import { saveWorkspacePreferences } from "./workspace-preferences";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(), isTauri: vi.fn(() => true) }));
const ipc = vi.mocked(invoke);
let root: Root;
let host: HTMLDivElement;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const storage = new Map<string, string>();
  vi.stubGlobal("crypto", { subtle: { digest: async (_: string, bytes: Uint8Array) => createHash("sha256").update(bytes).digest() } });
  vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value) });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  ipc.mockReset();
  vi.mocked(isTauri).mockReturnValue(true);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

async function validated(fixture: { id: string } = archived) {
  ipc.mockResolvedValueOnce(fixture);
  const job = await getBacktestJob(fixture.id, true);
  if (!job.result) throw new Error("Expected completed fixture");
  return { job, result: job.result };
}

function button(text: string, within: ParentNode = host) {
  const element = [...within.querySelectorAll("button")].find(node => node.textContent?.trim() === text);
  if (!element) throw new Error("Missing button " + text);
  return element;
}
async function input(element: HTMLInputElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

describe("verified backtest dashboard", () => {
  it("uses exact equity, drawdown and benchmark values and exposes session inspection", async () => {
    const payload = await validated();
    await act(async () => root.render(<BacktestResultView {...payload} />));
    const { rows, metrics, transactions } = payload.result.report;
    const chart = host.querySelector("figure")!;
    const firstRow = host.querySelector('table[aria-label="Equity history"] tbody tr')!;
    expect(firstRow.querySelector(`[title="${rows[0].equity_after_cost}"]`)).toBeTruthy();
    expect(firstRow.querySelector(`[title="${rows[0].frictionless_benchmark_equity}"]`)).toBeTruthy();
    expect(chart.querySelector('[data-series="equity_after_cost"] path')?.getAttribute("stroke-dasharray")).not.toBe(chart.querySelector('[data-series="frictionless_benchmark_equity"] path')?.getAttribute("stroke-dasharray"));
    expect(host.querySelectorAll("[data-transaction-session]")).toHaveLength(transactions.length);
    const inspector = chart.querySelector('input[type="range"]') as HTMLInputElement;
    inspector.focus();
    expect(document.activeElement).toBe(inspector);
    expect(inspector.max).toBe(String(rows.length - 1));
    await input(inspector, "0");
    expect(chart.querySelector("output")?.textContent).toContain(rows[0].equity_after_cost
      .replace(/^(\d)(\d{3})(\.|$)/, "$1,$2$3"));
    const drawdown = host.querySelector(`[title="${metrics.max_drawdown}"]`);
    expect(drawdown?.textContent).toContain("%");
    expect(host.querySelector(`[title="${metrics.cumulative_return}"]`)?.textContent).toContain("%");
    expect(host.querySelector(`[title="${metrics.transaction_cost_total}"]`)?.textContent).toContain("Modeled capital units");
  });

  it("supports the full 2,000 observed sessions, ten-row paging and combined transaction filters", async () => {
    const fixture = structuredClone(archived);
    const report = fixture.result.report;
    const sessions = Array.from({ length: 2001 }, (_, index) => new Date(Date.UTC(2014, 0, 2 + index * 2)).toISOString().slice(0, 10));
    const simulation = fixture.request.simulation;
    simulation.training_end = sessions[0];
    simulation.test_start = sessions[1];
    simulation.test_end = sessions.at(-1)!;
    fixture.request_sha256 = await canonicalModelSha256(fixture.request);
    fixture.result.request_sha256 = fixture.request_sha256;
    report.test_sessions = 2000;
    report.rows = sessions.slice(1).map((session_date, index) => ({
      ...report.rows[0], session_date, executed_from_prior_signal: sessions[index],
    }));
    report.transactions = Array.from({ length: 15 }, (_, index) => ({
      ...report.transactions[0],
      signal_as_of_session: sessions[index],
      hypothetical_fill_session: sessions[index + 1],
      exposure_before: index % 2 ? "1" : "0",
      exposure_after: index % 2 ? "0" : "1",
    }));
    report.metrics.transactions = report.transactions.length;
    report.input_sha256 = await canonicalModelSha256({
      backtest_contract: report.contract,
      engine_revision: report.engine_revision,
      request: simulation,
      model_result_sha256: fixture.request.source_output_sha256,
      model_lineage_sha256: fixture.request.source_lineage_sha256,
    });
    const output = Object.fromEntries(Object.entries(report).filter(([name]) => name !== "output_sha256"));
    fixture.output_sha256 = report.output_sha256 = await canonicalModelSha256(output);
    const payload = await validated(fixture);
    await act(async () => root.render(<BacktestResultView {...payload} />));
    expect(host.querySelector('figure input[type="range"]')?.getAttribute("max")).toBe("1999");
    expect(host.querySelector('[data-series="equity_after_cost"] path')?.getAttribute("d")?.match(/[ML]/g)).toHaveLength(2000);
    const txn = host.querySelector('table[aria-label="Hypothetical transactions"]')!.closest("section")!;
    expect(txn.querySelectorAll("tbody tr")).toHaveLength(10);
    await act(async () => button("Next", txn).click());
    expect(txn.querySelectorAll("tbody tr")).toHaveLength(5);
    const direction = txn.querySelector("select")!;
    await act(async () => { direction.value = "decrease"; direction.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(txn.querySelectorAll("tbody tr")).toHaveLength(7);
    await input(txn.querySelector('input[type="search"]')!, sessions[2]);
    expect(txn.querySelectorAll("tbody tr")).toHaveLength(1);
  });

  it("plots representable long Decimals and identifies out-of-range values without losing table precision", async () => {
    const fixture = structuredClone(archived);
    const report = fixture.result.report;
    const precise = "10000." + "1".repeat(270);
    const tooLarge = "1" + "0".repeat(400);
    report.rows[0].equity_after_cost = precise;
    report.rows[1].equity_after_cost = tooLarge;
    const output = Object.fromEntries(Object.entries(report).filter(([name]) => name !== "output_sha256"));
    fixture.output_sha256 = report.output_sha256 = await canonicalModelSha256(output);
    const payload = await validated(fixture);
    await act(async () => root.render(<BacktestResultView {...payload} />));
    const chart = host.querySelector("figure")!;
    expect(chart.querySelector('[data-series="equity_after_cost"] circle')).toBeTruthy();
    expect(chart.textContent).toContain("Some values are outside the chart’s numerical range.");
    expect(chart.querySelector("svg")?.outerHTML).not.toMatch(/NaN|Infinity/);
    expect(host.querySelector(`table [title="${precise}"]`)).toBeTruthy();
    expect(host.querySelector(`table [title="${tooLarge}"]`)).toBeTruthy();
  });

  it("shows a null Sharpe ratio and a meaningful empty transaction state in Chinese", async () => {
    saveWorkspacePreferences({ language: "zh-CN", theme: "dark", reduceMotion: true });
    const original = structuredClone(archived);
    const report = { ...original.result.report, metrics: { ...original.result.report.metrics, sharpe: null } };
    const fixture = { ...original, result: { ...original.result, report } };
    report.metrics.transactions = 0;
    report.transactions = [];
    const output = Object.fromEntries(Object.entries(report).filter(([name]) => name !== "output_sha256"));
    fixture.output_sha256 = report.output_sha256 = await canonicalModelSha256(output);
    const payload = await validated(fixture);
    await act(async () => root.render(<BacktestResultView {...payload} />));
    expect(host.textContent).toContain("夏普比率");
    expect(host.textContent).toContain("不可用");
    expect(host.textContent).toContain("策略没有模拟敞口变更。");
    expect(host.textContent).toContain("策略与基准权益");
    expect(host.querySelector("animate")).toBeNull();
    saveWorkspacePreferences({ language: "zh-CN", theme: "light", reduceMotion: true });
    expect(document.documentElement.dataset.theme).toBe("light");
  });
});

describe("explicit backtest export action", () => {
  it("requires confirmation for every export, sends pins only and handles cancellation", async () => {
    const payload = await validated();
    ipc.mockClear();
    await act(async () => root.render(<NotificationProvider><BacktestExport {...payload} /></NotificationProvider>));
    expect(host.textContent).toContain("without encryption");
    const exportButton = button("Choose save location");
    expect(exportButton.disabled).toBe(true);
    await act(async () => exportButton.click());
    expect(ipc).not.toHaveBeenCalled();
    const checkbox = host.querySelector('input[type="checkbox"]') as HTMLInputElement;
    await act(async () => checkbox.click());
    ipc.mockResolvedValueOnce("cancelled");
    await act(async () => exportButton.click());
    expect(ipc).toHaveBeenCalledWith("backtest_export", {
      jobId: payload.job.id, requestSha256: payload.result.request_sha256, outputSha256: payload.result.report.output_sha256, format: "json", locale: "en",
    });
    expect(host.textContent).toContain("Backtest export cancelled.");
    expect(exportButton.disabled).toBe(true);
    await act(async () => checkbox.click());
    const format = host.querySelector("select")!;
    await act(async () => { format.value = "equity"; format.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(exportButton.disabled).toBe(true);
    await act(async () => checkbox.click());
    ipc.mockResolvedValueOnce("saved");
    await act(async () => exportButton.click());
    expect(host.textContent).toContain("Backtest report saved locally.");
    expect(exportButton.disabled).toBe(true);
    await act(async () => checkbox.click());
    ipc.mockRejectedValueOnce("invalid_destination");
    await act(async () => exportButton.click());
    expect(host.textContent).toContain("Choose a regular local file with the correct extension.");
    expect(exportButton.disabled).toBe(true);
  });
});
