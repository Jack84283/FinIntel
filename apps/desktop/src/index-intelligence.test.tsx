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
      missing_data_policy: "observed_sessions_only", numeric_boundary: "decimal",
      volatility_returns_explicit: false },
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
/** A volatility-level source provides observed levels but no financial returns by default. */
function vixSample(): AnalyticsResult {
  const result = sample();
  result.identity = {
    ...result.identity, code: "VIX",
    adjustment: "volatility_index", return_semantics: "volatility_index",
  };
  result.source.source_url = "https://fred.stlouisfed.org/series/VIXCLS";
  result.metrics_enabled = false;
  result.max_drawdown = null;
  result.longest_underwater_observed_sessions = null;
  result.longest_completed_recovery_observed_sessions = null;
  result.ongoing_drawdown_observed_sessions = null;
  result.drawdown_episodes = [];
  result.period_returns = { week: [], month: [], year: [] };
  result.points = result.points.map(point => ({
    ...point,
    simple_return: null, log_return: null, cumulative_return: null, drawdown: null,
    rolling_volatility_annualized: null, momentum: null, rsi: null,
  }));
  return result;
}

/** Source-price fixtures keep actual trading-session dates within the selected query. */
function sampleFromPrices(closes: string[]): { result: AnalyticsResult; selection: AnalyticsQuery } {
  const dates = [
    "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15",
    "2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22",
  ].slice(-closes.length);
  const result = sample();
  result.points = closes.map((close, index) => ({
    ...result.points[0], session_date: dates[index], close,
  }));
  result.observations = closes.length;
  result.first_session = dates[0];
  result.last_session = dates[dates.length - 1];
  result.request = { start: dates[0], end: dates[dates.length - 1] };
  return { result, selection: { ...query, ...result.request } };
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

  it("rejects omitted open or completed recovery episodes even with consistent null summaries", () => {
    const missingOpen = sample();
    missingOpen.drawdown_episodes = [];
    missingOpen.longest_completed_recovery_observed_sessions = null;
    missingOpen.ongoing_drawdown_observed_sessions = null;
    expect(() => parseAnalyticsResult(missingOpen, query)).toThrow();

    const { result: missingCompleted, selection } = sampleFromPrices(["100", "120", "110", "120"]);
    missingCompleted.drawdown_episodes = [];
    missingCompleted.longest_completed_recovery_observed_sessions = null;
    missingCompleted.ongoing_drawdown_observed_sessions = null;
    expect(() => parseAnalyticsResult(missingCompleted, selection)).toThrow();
  });

  it("rejects fabricated local highs and shifted trough dates despite plausible durations", () => {
    const { result: forged, selection } = sampleFromPrices(
      ["100", "120", "110", "113", "100", "105", "115"]
    );
    const d = forged.points.map(p => p.session_date);
    // The earlier high is 120 and is never recovered, but a false 113 high
    // produces a self-consistent 100 -> 115 'completed recovery'.
    forged.drawdown_episodes = [{
      status: "recovered",
      peak_session_date: d[3], trough_session_date: d[4], recovery_session_date: d[6],
      trough_drawdown: String(100 / 113 - 1),
      underwater_observed_sessions: 2,
      peak_to_recovery_observed_sessions: 3,
      trough_to_recovery_observed_sessions: 2,
    }];
    forged.longest_completed_recovery_observed_sessions = 3;
    forged.ongoing_drawdown_observed_sessions = null;
    forged.longest_underwater_observed_sessions = 2;
    expect(() => parseAnalyticsResult(forged, selection)).toThrow();

    const { result: shifted, selection: shiftedQuery } = sampleFromPrices(
      ["100", "120", "110", "108", "115", "120"]
    );
    const days = shifted.points.map(p => p.session_date);
    shifted.drawdown_episodes = [{
      status: "recovered",
      peak_session_date: days[1], trough_session_date: days[2],
      recovery_session_date: days[5], trough_drawdown: String(110 / 120 - 1),
      underwater_observed_sessions: 3, peak_to_recovery_observed_sessions: 4,
      trough_to_recovery_observed_sessions: 3,
    }];
    shifted.longest_completed_recovery_observed_sessions = 4;
    shifted.ongoing_drawdown_observed_sessions = null;
    shifted.longest_underwater_observed_sessions = 3;
    expect(() => parseAnalyticsResult(shifted, shiftedQuery)).toThrow();
  });

  it("accepts complete tied-peak episodes and sub-float-precision Decimal drawdowns", () => {
    const { result: complete, selection } = sampleFromPrices(["100", "120", "110", "120", "118"]);
    const days = complete.points.map(p => p.session_date);
    complete.drawdown_episodes = [
      {
        status: "recovered", peak_session_date: days[1], trough_session_date: days[2],
        recovery_session_date: days[3], trough_drawdown: String(110 / 120 - 1),
        underwater_observed_sessions: 1, peak_to_recovery_observed_sessions: 2,
        trough_to_recovery_observed_sessions: 1,
      },
      {
        status: "ongoing", peak_session_date: days[3], trough_session_date: days[4],
        recovery_session_date: null, trough_drawdown: String(118 / 120 - 1),
        underwater_observed_sessions: 1, peak_to_recovery_observed_sessions: null,
        trough_to_recovery_observed_sessions: null,
      },
    ];
    complete.longest_completed_recovery_observed_sessions = 2;
    complete.ongoing_drawdown_observed_sessions = 1;
    complete.longest_underwater_observed_sessions = 1;
    expect(parseAnalyticsResult(complete, selection).drawdown_episodes).toHaveLength(2);

    const high = "100.0000000000000000000001";
    const { result: precise, selection: preciseQuery } = sampleFromPrices([high, "100", high]);
    const actualDates = precise.points.map(p => p.session_date);
    // All three closes round to 100 as a JS Number, but differ as Decimal.
    precise.drawdown_episodes = [{
      status: "recovered", peak_session_date: actualDates[0],
      trough_session_date: actualDates[1], recovery_session_date: actualDates[2],
      trough_drawdown: "-0.000000000000000000000001",
      underwater_observed_sessions: 1, peak_to_recovery_observed_sessions: 2,
      trough_to_recovery_observed_sessions: 1,
    }];
    precise.longest_completed_recovery_observed_sessions = 2;
    precise.ongoing_drawdown_observed_sessions = null;
    precise.longest_underwater_observed_sessions = 1;
    expect(parseAnalyticsResult(precise, preciseQuery).drawdown_episodes).toHaveLength(1);
  });

  it("accepts levels but not financial return metrics for default VIX queries", () => {
    const selection: AnalyticsQuery = { ...query, code: "VIX" };
    const valid = vixSample();
    expect(parseAnalyticsResult(valid, selection)).toEqual(valid);
    // The volatility level can still be normalized and have a moving average.
    expect(valid.points[1].normalized_100).toBe("110");
    const withMovingAverage = vixSample();
    withMovingAverage.points[2].moving_average = "103";
    expect(parseAnalyticsResult(withMovingAverage, selection)).toEqual(withMovingAverage);
  });

  it("rejects disabled VIX responses carrying summary, per-session or period returns", () => {
    const selection: AnalyticsQuery = { ...query, code: "VIX" };
    const falseDrawdown = vixSample();
    falseDrawdown.max_drawdown = "-0.5";
    expect(() => parseAnalyticsResult(falseDrawdown, selection)).toThrow();

    const underwater = vixSample();
    underwater.longest_underwater_observed_sessions = 1;
    expect(() => parseAnalyticsResult(underwater, selection)).toThrow();

    for (const field of [
      "simple_return", "log_return", "cumulative_return", "drawdown",
      "rolling_volatility_annualized", "momentum", "rsi",
    ] as const) {
      const forged = vixSample();
      forged.points[2][field] = "-0.1";
      expect(() => parseAnalyticsResult(forged, selection)).toThrow();
    }

    const periods = vixSample();
    periods.period_returns.week = [{ session_date: "2026-10-22", return: "-0.1", period: "week" }];
    expect(() => parseAnalyticsResult(periods, selection)).toThrow();
  });

  it("rejects unrequested VIX return enablement and inconsistent index identity", () => {
    const selection: AnalyticsQuery = { ...query, code: "VIX" };
    const unauthorized = sample();
    unauthorized.identity = { ...vixSample().identity };
    // The completed/ongoing episodes are internally consistent, but the desktop
    // never requested the backend's explicit volatility-return opt-in.
    expect(() => parseAnalyticsResult(unauthorized, selection)).toThrow();

    const explicitFlag = vixSample();
    explicitFlag.settings.volatility_returns_explicit = true;
    expect(() => parseAnalyticsResult(explicitFlag, selection)).toThrow();

    const wrongSemantics = vixSample();
    wrongSemantics.identity.return_semantics = "price_index";
    expect(() => parseAnalyticsResult(wrongSemantics, selection)).toThrow();

    const wrongAdjustment = vixSample();
    wrongAdjustment.identity.adjustment = "price_index";
    expect(() => parseAnalyticsResult(wrongAdjustment, selection)).toThrow();

    const disabledPriceIndex = vixSample();
    disabledPriceIndex.identity = { ...sample().identity };
    expect(() => parseAnalyticsResult(disabledPriceIndex, query)).toThrow();
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
