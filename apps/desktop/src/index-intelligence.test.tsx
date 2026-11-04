import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { IndexIntelligencePage, latestCommonDate } from "./IndexIntelligencePage";
import {
  fetchIndexAnalytics, parseAnalyticsResult, validateAnalyticsQuery,
  type AnalyticsQuery, type AnalyticsResult,
} from "./index-intelligence-api";
import { chartLines, metricCell, metricRows, shownChartLines } from "./IndexIntelligenceChart";
import type { IndexCatalogRecord } from "./index-history-api";
import { translate } from "./localization";

const native = vi.hoisted(() => ({ enabled: false, invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => native.enabled,
  invoke: native.invoke,
}));
const query: AnalyticsQuery = {
  code: "SP500", comparisonCode: null, start: "2026-10-20",
  end: "2026-10-22", window: 20, qualityPolicy: "allow_unverified",
};
const sha = "a".repeat(64);
function sample(comparison = false): AnalyticsResult {
  const identity = {
    instrument_kind: "index" as const, code: "SP500", provider: "fred",
    adjustment: "price_index", return_semantics: "price_index", frequency: "daily", units: "index_points",
  };
  const result: AnalyticsResult = {
    calculation_version: "index-analytics-v2",
    identity, settings: { window_sessions: 20, annualization_sessions: 252,
      missing_data_policy: "observed_sessions_only", numeric_boundary: "decimal" },
    observations: 3, first_session: "2026-10-20", last_session: "2026-10-22",
    metrics_enabled: true, max_drawdown: "-0.1", longest_underwater_observed_sessions: 1,
    longest_completed_recovery_observed_sessions: null,
    ongoing_drawdown_observed_sessions: 1,
    drawdown_episodes: [{
      status: "ongoing", peak_session_date: "2026-10-21",
      trough_session_date: "2026-10-22", recovery_session_date: null,
      trough_drawdown: "-0.1", underwater_observed_sessions: 1,
      peak_to_recovery_observed_sessions: null,
      trough_to_recovery_observed_sessions: null,
    }],
    period_returns: { week: [], month: [], year: [] },
    points: [
      { session_date: "2026-10-20", close: "100", normalized_100: "100", simple_return: null,
        log_return: null, cumulative_return: "0", drawdown: "0",
        rolling_volatility_annualized: null, moving_average: null, momentum: null, rsi: null },
      { session_date: "2026-10-21", close: "110", normalized_100: "110", simple_return: "0.1",
        log_return: "0.09531", cumulative_return: "0.1", drawdown: "0",
        rolling_volatility_annualized: null, moving_average: null, momentum: null, rsi: null },
      { session_date: "2026-10-22", close: "99", normalized_100: "99", simple_return: "-0.1",
        log_return: "-0.1053", cumulative_return: "-0.01", drawdown: "-0.1",
        rolling_volatility_annualized: null, moving_average: null, momentum: null, rsi: null },
    ],
    request: { start: query.start, end: query.end },
    source: { source_url: "https://fred.stlouisfed.org/series/SP500", coverage_updated_at: null,
      snapshot_sha256: sha },
    quality: { status: "stale_or_partial", quality_verified: false },
    quality_policy: "allow_unverified", unverified_data: true,
    read_only: true, no_network: true, no_synthetic_sessions: true,
  };
  if (comparison) {
    result.comparison = {
      left: identity, right: { ...identity, code: "DJIA" },
      missing_data_policy: "intersection", overlap_sessions: 2, left_only_sessions: 1,
      right_only_sessions: 0, window_sessions: 20, dividends_excluded: false,
      source: { source_url: "https://fred.stlouisfed.org/series/DJIA",
        coverage_updated_at: null, snapshot_sha256: "b".repeat(64) },
      points: [
        { session_date: "2026-10-21", left_normalized_100: "100", right_normalized_100: "100",
          left_return: null, right_return: null, relative_strength: "1",
          rolling_correlation: null, rolling_covariance: null, rolling_beta: null,
          rolling_tracking_error_annualized: null },
        { session_date: "2026-10-22", left_normalized_100: "90", right_normalized_100: "104",
          left_return: "-0.1", right_return: "0.04", relative_strength: "0.865384615384615",
          rolling_correlation: null, rolling_covariance: null, rolling_beta: null,
          rolling_tracking_error_annualized: null },
      ],
    };
  }
  return result;
}
beforeEach(() => {
  native.enabled = false;
  native.invoke.mockReset();
});
afterEach(() => vi.unstubAllGlobals());

describe("Index Intelligence local analytics API", () => {
  it("rejects invalid/incompatible selections before any external or native access", async () => {
    expect(() => validateAnalyticsQuery({ ...query, code: "VIX", comparisonCode: "DJIA" })).toThrow();
    expect(() => validateAnalyticsQuery({ ...query, code: "SP500", comparisonCode: "SP500" })).toThrow();
    expect(() => validateAnalyticsQuery({ ...query, start: "2026-02-30" })).toThrow();
    expect(() => validateAnalyticsQuery({ ...query, start: "2026-10-23" })).toThrow();
    expect(() => validateAnalyticsQuery({ ...query, window: 7 as 20 })).toThrow();
    await expect(fetchIndexAnalytics({ ...query, comparisonCode: "VIX" })).rejects.toThrow();
  });

  it("reads only the bounded local API using a fixed FRED provider and no caching", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => sample() });
    vi.stubGlobal("fetch", fetcher);
    const result = await fetchIndexAnalytics(query);
    expect(result.points).toHaveLength(3);
    expect(fetcher).toHaveBeenCalledTimes(1);
    const [url, options] = fetcher.mock.calls[0];
    expect(url).toContain("/api/v1/analytics/series?");
    expect(url).toContain("code=SP500");
    expect(url).toContain("provider=fred");
    expect(url).toContain("quality_policy=allow_unverified");
    expect(options).toEqual({ cache: "no-store" });
  });

  it("uses managed Tauri IPC without direct HTTP requests", async () => {
    native.enabled = true;
    native.invoke.mockResolvedValue(sample(true));
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const data = await fetchIndexAnalytics({ ...query, comparisonCode: "DJIA" });
    expect(data.comparison?.right.code).toBe("DJIA");
    expect(native.invoke).toHaveBeenCalledWith("analytics_series", {
      code: "SP500", comparisonCode: "DJIA", start: query.start, end: query.end,
      window: 20, qualityPolicy: "allow_unverified",
    });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("does not turn missing network, authorization or quality results into invented values", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("Backend offline")));
    await expect(fetchIndexAnalytics(query)).rejects.toThrow("Backend offline");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false, status: 422,
      json: async () => ({ detail: "Current complete quality scan required" }),
    }));
    await expect(fetchIndexAnalytics({ ...query, qualityPolicy: "require_complete" }))
      .rejects.toThrow("Current complete quality scan required");
  });

  it("rejects fabricated drawdown recoveries, impossible dates and missing censoring", () => {
    const invalidRecovery = sample();
    invalidRecovery.drawdown_episodes[0].status = "recovered";
    invalidRecovery.drawdown_episodes[0].recovery_session_date = "2026-10-23";
    invalidRecovery.drawdown_episodes[0].peak_to_recovery_observed_sessions = 2;
    expect(() => parseAnalyticsResult(invalidRecovery, query)).toThrow();
    const invalidDuration = sample();
    invalidDuration.drawdown_episodes[0].underwater_observed_sessions = 0;
    expect(() => parseAnalyticsResult(invalidDuration, query)).toThrow();
    const invalidTrough = sample();
    invalidTrough.drawdown_episodes[0].trough_drawdown = "-0.5";
    expect(() => parseAnalyticsResult(invalidTrough, query)).toThrow();
    const invalidOverall = sample();
    invalidOverall.longest_completed_recovery_observed_sessions = 3;
    expect(() => parseAnalyticsResult(invalidOverall, query)).toThrow();
    expect(parseAnalyticsResult(sample(), query).ongoing_drawdown_observed_sessions).toBe(1);
  });

  it("rejects wrong provider, mismatched dates and unsourced/malformed numbers", () => {
    expect(() => parseAnalyticsResult({ ...sample(), identity: { ...sample().identity, provider: "other" } }, query)).toThrow();
    expect(() => parseAnalyticsResult({ ...sample(), request: { start: "2020-01-01", end: query.end } }, query)).toThrow();
    const invalid = sample();
    invalid.points[1].close = "NaN";
    expect(() => parseAnalyticsResult(invalid, query)).toThrow();
    const badComparison = sample(true);
    expect(() => parseAnalyticsResult(badComparison, query)).toThrow("Unexpected analytics benchmark");
  });
});

describe("Index Intelligence UI and exact chart/grid values", () => {
  it("starts index-first with SP500, an explicit quality policy and no equity ticker", () => {
    const markup = renderToStaticMarkup(<IndexIntelligencePage />);
    expect(markup).toContain("Index Intelligence");
    expect(markup).toContain("SP500");
    expect(markup).toContain("Verified only");
    expect(markup).toContain("Compare with");
    expect(markup).not.toContain("Stock or ETF symbol");
    expect(translate("zh-CN", "Index Intelligence")).not.toBe("Index Intelligence");
    expect(translate("zh-CN", "Quality verified")).not.toBe("Quality verified");
    expect(translate("zh-CN", "Longest completed recovery")).not.toBe("Longest completed recovery");
    expect(translate("zh-CN", "Open drawdown duration")).not.toBe("Open drawdown duration");
  });

  it("uses identical backend cells in the comparison chart and historical grid", () => {
    const result = sample(true);
    const lines = chartLines(result, "normalized");
    const rows = metricRows(result, "normalized");
    expect(rows).toHaveLength(2);
    expect(lines[0].values.at(-1)?.value).toBe(Number(metricCell(result, "normalized", rows[1])));
    expect(lines[1].values.at(-1)?.value)
      .toBe(Number(metricCell(result, "normalized", rows[1], "benchmark")));
    expect(metricCell(result, "correlation", rows[1])).toBeNull();
    expect(metricRows(result, "drawdown")).toHaveLength(3);
    expect(shownChartLines([{ ...lines[0], values: Array.from({ length: 500 }, (_, i) => ({
      date: "2026-10-20", value: i,
    })) }])[0].values).toHaveLength(450);
  });

  it("chooses last actually stored common date, not the computer clock", () => {
    const catalog = [
      { code: "SP500", last_session: "2026-10-22", first_session: "2020-01-01" },
      { code: "DJIA", last_session: "2026-10-20", first_session: "2023-01-01" },
      { code: "VIX", last_session: null, first_session: null },
    ] as IndexCatalogRecord[];
    expect(latestCommonDate(catalog, "SP500", "DJIA")).toBe("2026-10-20");
    expect(latestCommonDate(catalog, "SP500", null)).toBe("2026-10-22");
    expect(latestCommonDate(catalog, "SP500", "VIX")).toBeNull();
    const disjoint = [
      { code: "SP500", first_session: "2026-10-21", last_session: "2026-10-22" },
      { code: "DJIA", first_session: "2026-10-10", last_session: "2026-10-20" },
    ] as IndexCatalogRecord[];
    expect(latestCommonDate(disjoint, "SP500", "DJIA")).toBeNull();
  });
});
