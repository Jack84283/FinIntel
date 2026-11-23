// @vitest-environment jsdom
import { webcrypto } from "node:crypto";
import { act } from "react";
import { invoke } from "@tauri-apps/api/core";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import contracts from "../../../contracts/analysis-jobs.json";
import archived from "../../../contracts/test-fixtures/analysis-relative-strength.json";
import { AnalysisResultVisualization } from "./AnalysisResultVisualization";
import {
  getAnalysisJob, parseAnalysisResult, type AnalysisJobSummary, type AnalysisModelId, type AnalysisPoint, type AnalysisSourceCode,
} from "./analysis-jobs-api";
import { chartNumber, continuousSegments, datePosition, formatCell } from "./analysis-result-display";
import { canonicalModelSha256 } from "./model-research-integrity";
import { saveWorkspacePreferences } from "./workspace-preferences";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(), isTauri: () => true }));

const date = (index: number) => new Date(Date.UTC(2014, 0, 2 + index * 2)).toISOString().slice(0, 10);
const close = (index: number) => ({ session_date: date(index), close: String(100 + index) });
const evaluated_at_utc = "2025-01-01T00:00:00.000000+00:00";

async function verified<M extends AnalysisModelId>(modelId: M, points: readonly AnalysisPoint<M>[], {
  parameters = {}, codes = ["SP500"] as AnalysisSourceCode[], unverified = false, allowUnverified = false,
} = {}) {
  const model = contracts[modelId];
  const observed_sessions = points.map(point => point.session_date);
  const requested = { start: observed_sessions[0], end: observed_sessions.at(-1)! };
  const sources = codes.map((code, index) => ({
    identity: { instrument_kind: "index", code, provider: "fred", adjustment: "price_index", return_semantics: "price_index", frequency: "daily", units: "index_points" },
    snapshot_sha256: String(index + 1).repeat(64),
  }));
  const identity = { model_id: modelId, model_version: model.version, implementation_revision: model.revision };
  const resolved = { ...Object.fromEntries(Object.entries(model.parameters).map(([key, rule]) => [key, rule.default])), ...parameters };
  const request = {
    contract_version: "analysis-model-request-v1", model_id: modelId, model_version: model.version,
    window: requested, sources, quality_policy: (allowUnverified || unverified) ? "allow_unverified" : "require_complete", parameters: resolved,
  };
  const evidence = {
    contract_version: "analysis-model-evidence-v1", model: identity, calculation_version: model.calculation,
    sources: sources.map((source, index) => ({
      ...source, observed: { ...requested, count: points.length + (index ? 1 : 0) },
      aligned_count: points.length, excluded_count: index ? 1 : 0,
      quality: unverified ? { status: "not_run", warnings: ["quality_scan_not_run"] } : { status: "verified", warnings: [] },
    })),
    requested, observed_sessions, parameters: resolved, output_fields: model.output_fields,
    quality_policy: request.quality_policy, warnings: (allowUnverified || unverified) ? ["quality_override"] : [], research_only: true,
  };
  const output_sha256 = await canonicalModelSha256(points);
  const result = {
    contract_version: "analysis-model-result-v1", points, evidence, output_sha256,
    reproducibility_sha256: await canonicalModelSha256(evidence), evaluated_at_utc,
  };
  vi.mocked(invoke).mockResolvedValueOnce({
    ...identity, request, output_sha256, result,
    id: archived.id, state: "completed", status: "completed", progress: 100, attempts: 1, cancel_requested: 0,
    source_snapshot_sha256: sources.length === 1 ? sources[0].snapshot_sha256 : null,
    request_sha256: await canonicalModelSha256(request), error_code: null,
    created_at: evaluated_at_utc, updated_at: evaluated_at_utc, started_at: evaluated_at_utc, finished_at: evaluated_at_utc,
  });
  const detail = await getAnalysisJob(archived.id, true);
  if (!detail.result) throw new Error("Expected a verified fixture result");
  return detail.result;
}

let host: HTMLDivElement;
let root: Root;
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("crypto", webcrypto);
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
  });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});
const render = async (result: ReturnType<typeof verified>) => {
  const payload = await result;
  await act(async () => root.render(<AnalysisResultVisualization result={payload} />));
};
const tableValue = (field: string, index = 0) =>
  host.querySelector(`[data-session="${date(index)}"] [data-field="${field}"]`)?.textContent;
const series = () => [...host.querySelectorAll("[data-series]")].map(node => node.getAttribute("data-series"));
const chartMode = async (mode: string) => act(async () => {
  const select = host.querySelector("select")!;
  select.value = mode;
  select.dispatchEvent(new Event("change", { bubbles: true }));
});

describe("mounted verified result visualization", () => {
  it("renders exact EMA values and crossover markers while retaining null warm-up cells", async () => {
    const points: AnalysisPoint<"builtin.ema_trend">[] = Array.from({ length: 4 }, (_, i) => ({
      ...close(i), fast_ema: i ? "102.125" : "100", slow_ema: i ? "101.125" : null,
      ema_difference: i ? "1.000" : null, trend_after_close: i ? "above" : null,
      crossover_after_close: i === 2 ? "above" : i ? "none" : null, warmup_complete: i > 0,
    }));
    await render(verified("builtin.ema_trend", points, { parameters: { fast_window: 1, slow_window: 2 } }));
    expect(series()).toEqual(["close", "fast_ema", "slow_ema"]);
    expect(host.querySelectorAll('[data-crossover="above"]')).toHaveLength(1);
    expect(tableValue("fast_ema", 2)).toBe("102.125");
    expect(tableValue("slow_ema")).toBe("Unavailable");
    expect(tableValue("warmup_complete")).toBe("No");
    expect(host.querySelector("output")?.textContent).toContain("102.125");
  });

  it("uses observed momentum lookback, comparison sessions and exact signed percentages", async () => {
    const points: AnalysisPoint<"builtin.momentum">[] = Array.from({ length: 22 }, (_, i) => ({
      ...close(i), comparison_session_date: i === 21 ? date(0) : null, comparison_close: i === 21 ? "100" : null,
      momentum: i === 21 ? "-0.021234567890123456789" : null, lookback_sessions: 21, warmup_complete: i === 21,
    }));
    await render(verified("builtin.momentum", points, { parameters: { lookback_sessions: 21 } }));
    expect(series()).toEqual(["momentum"]);
    expect(tableValue("momentum")).toBe("Unavailable");
    expect(host.querySelector("output")?.textContent).toContain("-2.1234567890123456789%");
    expect(host.textContent).toContain("01/02/2014");
    expect(host.textContent).toContain("21");
    const go = [...host.querySelectorAll("button")].find(button => button.textContent === "Next")!;
    await act(async () => go.click());
    await act(async () => go.click());
    expect(tableValue("momentum", 21)).toBe("-2.1234567890123456789%");
    expect(tableValue("comparison_session_date", 21)).toBe("01/02/2014");
  });

  it.each([["DJIA", "SP500"], ["NDX", "DJIA", "SP500", "NASDAQCOM"]] as AnalysisSourceCode[][])(
    "matches relative chart and table fields in ordered sources %#",
    async (...codes) => {
      const points = [0, 1].map(i => Object.fromEntries(contracts["builtin.relative_strength"].output_fields.map(({ name }) => {
        const [sourceCode] = name.split("_");
        if (name === "session_date") return [name, date(i)];
        if (name === "benchmark") return [name, codes[0].toLowerCase()];
        const available = codes.some(code => code.toLowerCase() === sourceCode);
        return [name, !available ? null : name.endsWith("normalized_100") ? "100.25" : name.endsWith("return_difference") ? "0.12345" : "1.25"];
      })) as AnalysisPoint<"builtin.relative_strength">);
      await render(verified("builtin.relative_strength", points, { codes }));
      expect(host.querySelector("figure li")?.textContent).toContain(codes[0] + " · Benchmark");
      for (const [mode, expected] of [
        ["normalized_100", "100.25"], ["relative_strength", "1.25"], ["return_difference", "12.345%"],
      ]) {
        await chartMode(mode);
        const fields = codes.map(code => code.toLowerCase() + "_" + mode);
        expect(series()).toEqual(fields);
        expect([...host.querySelectorAll(`[data-session="${date(0)}"] td`)].map(td => td.getAttribute("data-field"))).toEqual(fields);
        expect(tableValue(fields[0])).toBe(expected);
        expect(host.querySelector("output")?.textContent).toContain(expected);
      }
    },
  );

  it("renders RSI on a 0–100 axis with supplied thresholds and classifications", async () => {
    const states = [null, "low", "neutral", "high"] as const;
    const points: AnalysisPoint<"builtin.rsi_state">[] = states.map((state, index) => ({
      ...close(index), rsi: [null, "20", "55", "80"][index], rsi_state: state,
      lower_threshold: 35, upper_threshold: 65, warmup_complete: state !== null,
    }));
    await render(verified("builtin.rsi_state", points, { parameters: { window: 1, lower_threshold: 35, upper_threshold: 65 } }));
    expect(series()).toEqual(["rsi", "lower_threshold", "upper_threshold"]);
    expect([...host.querySelectorAll('svg[role="img"] text')].map(node => node.textContent)).toContain("100");
    expect(states.map((_, index) => tableValue("rsi_state", index))).toEqual(["Unavailable", "Low", "Neutral", "High"]);
    expect(tableValue("lower_threshold")).toBe("35");
    expect(tableValue("upper_threshold")).toBe("65");
  });

  it.each([false, true])("shows risk metrics and separate recovery evidence with comparison=%s", async comparison => {
    const points: AnalysisPoint<"builtin.risk_profile">[] = Array.from({ length: 62 }, (_, index) => ({
      ...close(index), simple_return: index ? "-0.025" : null, comparison_return: comparison && index ? "0.01" : null,
      annualized_volatility: index >= 2 ? "0.2125" : null, downside_deviation: index >= 2 ? "0.0125" : null,
      var_95: index >= 60 ? "0.04125" : null, expected_shortfall_95: index >= 60 ? "0.0525" : null,
      correlation: comparison && index >= 2 ? "-0.125" : null,
      current_drawdown: index ? "-0.085" : "0", maximum_drawdown: index ? "-0.125" : "0",
      open_peak_date: index ? date(0) : null, open_trough_date: index ? date(1) : null,
      maximum_peak_date: index ? date(0) : null, maximum_trough_date: index ? date(1) : null,
      last_recovered_peak_date: index > 3 ? date(0) : null, last_recovered_trough_date: index > 3 ? date(1) : null,
      last_recovery_date: index > 3 ? date(3) : null, open_elapsed_intervals: index || null,
      last_peak_to_recovery_intervals: index > 3 ? 3 : null, last_trough_to_recovery_intervals: index > 3 ? 2 : null,
      historical_sample_count: Math.min(index, 60), historical_tail_count: index >= 60 ? 3 : null,
      historical_tail_status: index >= 60 ? "available" : "insufficient_history",
    }));
    await render(verified("builtin.risk_profile", points, {
      codes: comparison ? ["SP500", "DJIA"] : ["SP500"], parameters: { risk_window: 2, tail_window: 60 },
    }));
    const summary = host.querySelector('dl[aria-label="Historical risk measurements"]')?.textContent;
    for (const value of ["21.25%", "1.25%", "4.125%", "5.25%", "-8.5%", "-12.5%"]) expect(summary).toContain(value);
    if (comparison) expect(summary).toContain("-0.125");
    else expect(summary).not.toContain("Correlation");
    expect(tableValue("var_95")).toBe("Unavailable");
    expect(tableValue("historical_sample_count", 2)).toBe("2");
    expect(tableValue("historical_tail_status")).toBe("Insufficient history");
    await chartMode("drawdown");
    expect(series()).toEqual(["current_drawdown", "maximum_drawdown"]);
    expect(host.querySelector('dl[aria-label="Ongoing drawdown"]')?.textContent).toContain("Open elapsed intervals");
    expect(host.querySelector('dl[aria-label="Last completed recovery"]')?.textContent).toContain("Last recovery date");
    expect(tableValue("maximum_drawdown", 1)).toBe("-12.5%");
  });

  it("keeps regime tracks independent, retains exact reasons, and labels unverified unknowns", async () => {
    const trends = ["unknown", "uptrend", "downtrend", "sideways"] as const;
    const volatility = ["unknown", "high", "low", "normal"] as const;
    const trendReasons = ["trend_insufficient_history", "trend_above_band", "trend_below_band", "trend_mixed"] as const;
    const volReasons = ["vol_calibration_insufficient", "vol_above_p75", "vol_below_p25", "vol_normal_range"] as const;
    const points: AnalysisPoint<"builtin.market_regime">[] = trends.map((trend, index) => ({
      ...close(index), sma50: "101", sma200: index ? "102" : null, close_distance: index ? "0.15" : null,
      sma_distance: index ? "0.10" : null, trend_state: trend,
      realized_volatility: index ? "0.18" : null, volatility_p25: index ? "0.11" : null,
      volatility_p75: index ? "0.21" : null, volatility_calibration_count: index,
      volatility_state: volatility[index], reason_codes: [trendReasons[index], volReasons[index]],
    }));
    await render(verified("builtin.market_regime", points));
    expect([...host.querySelectorAll("[data-state]")].map(node => node.getAttribute("data-state"))).toEqual([
      "uptrend", "downtrend", "sideways", "unknown", "low", "normal", "high", "unknown",
    ]);
    expect(tableValue("trend_state", 1)).toBe("Uptrend");
    expect(tableValue("volatility_state", 1)).toBe("High");
    expect(tableValue("reason_codes")).toBe("Insufficient trend history · Insufficient volatility calibration");
    await chartMode("volatility");
    expect(tableValue("volatility_calibration_count", 2)).toBe("2");
    expect(series()).toEqual(["realized_volatility", "volatility_p25", "volatility_p75"]);

    const unverified = points.map(point => ({
      ...point, trend_state: "unknown" as const, volatility_state: "unknown" as const,
      reason_codes: ["quality_unverified"] as const,
    }));
    await render(verified("builtin.market_regime", unverified, { unverified: true }));
    expect(host.textContent).toContain("Result uses unverified source evidence.");
    expect(tableValue("trend_state")).toBe("Unknown");
    expect(tableValue("reason_codes")).toBe("Quality unverified");
  });

  it("renders the full 2,000-observation result as bounded SVG paths and paginates to the last row", async () => {
    const points: AnalysisPoint<"builtin.momentum">[] = Array.from({ length: 2000 }, (_, index) => ({
      ...close(index), comparison_session_date: index < 21 ? null : date(index - 21),
      comparison_close: index < 21 ? null : "100", momentum: index < 21 || index === 500 ? null : "0.0125",
      lookback_sessions: 21, warmup_complete: index >= 21 && index !== 500,
    }));
    await render(verified("builtin.momentum", points, { parameters: { lookback_sessions: 21 } }));
    const path = host.querySelector('[data-series="momentum"] path')?.getAttribute("d");
    expect(path?.match(/M/g)).toHaveLength(2);
    expect(path?.match(/[ML]/g)).toHaveLength(1978);
    expect(host.querySelectorAll("tbody tr")).toHaveLength(10);
    const input = host.querySelector<HTMLInputElement>('input[aria-label="Go to page"]')!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "200");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => [...host.querySelectorAll("button")].find(button => button.textContent === "Go")!.click());
    expect(tableValue("momentum", 1999)).toBe("1.25%");
  });

  it.each([
    ["en", "Unverified evidence is allowed for this historical research.", "Result uses unverified source evidence."],
    ["zh-CN", "本次历史研究允许使用未经核验的证据。", "此结果使用了未经核验的数据来源证据。"],
  ] as const)("explains the selected override with verified sources in %s", async (language, policyMessage, sourceAlert) => {
    saveWorkspacePreferences({ language, theme: "dark", reduceMotion: false });
    const points: AnalysisPoint<"builtin.rsi_state">[] = [{
      ...close(0), rsi: null, rsi_state: null, lower_threshold: 30, upper_threshold: 70, warmup_complete: false,
    }];
    await render(verified("builtin.rsi_state", points, { allowUnverified: true }));
    expect(host.textContent).toContain(policyMessage);
    expect(host.textContent).not.toContain(sourceAlert);
    expect(host.textContent).not.toContain("quality_override");
  });

  it("renders a localized read-only chart with focusable native controls and no animation in either theme", async () => {
    const result = await parseAnalysisResult(archived.result, archived as AnalysisJobSummary);
    for (const theme of ["light", "dark"] as const) {
      saveWorkspacePreferences({ language: "zh-CN", theme, reduceMotion: true });
      await act(async () => root.render(<AnalysisResultVisualization result={result} />));
      expect(host.textContent).toContain("归一化指数水平");
      expect(host.textContent).toContain("仅供研究");
      const select = host.querySelector("select")!;
      select.focus();
      expect(document.activeElement).toBe(select);
      expect(host.querySelector('input[type="range"]')?.getAttribute("aria-valuetext")).toBeTruthy();
      expect(host.querySelector("animate, animateTransform")).toBeNull();
    }
  });
});

describe("presentation-only chart conversion", () => {
  it("retains 2,000 ordered sessions, breaks gaps and scales only decimal returns", () => {
    const rows = Array.from({ length: 2000 }, (_, index) => ({
      session_date: date(index), momentum: index === 100 ? null : "0.0125",
    }));
    const field = contracts["builtin.momentum"].output_fields.find(item => item.name === "momentum")!;
    const segments = continuousSegments(rows, { field: { ...field, kind: "decimal" }, label: "Momentum" });
    expect(segments.map(part => part.length)).toEqual([100, 1899]);
    expect(segments[0][0]).toEqual({ date: date(0), value: 1.25 });
    expect(segments[1].at(-1)?.date).toBe(date(1999));
    expect(datePosition(date(1), date(0), date(3))).toBeCloseTo(1 / 3);
    expect(chartNumber("0.0125", "ratio")).toBe(0.0125);
    expect(chartNumber("1".repeat(400), "decimal_return")).toBeNull();
    expect(chartNumber(null, "decimal_return")).toBeNull();
    expect(formatCell("-0.000000012300", { ...field, kind: "decimal" }, "en", key => key)).toBe("-0.0000012300%");
  });
});
