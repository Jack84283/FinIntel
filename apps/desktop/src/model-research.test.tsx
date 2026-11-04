import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  BUILTIN_MODEL_ID, BUILTIN_MODEL_VERSION,
  fetchModelResearch, parseModelResearchResult, validateModelResearchQuery,
  type ModelResearchQuery, type ModelResearchResult, type ModelResearchPoint,
} from "./model-research-api";
import { ModelResearchPanel } from "./ModelResearchPanel";
import { translate } from "./localization";

const native = vi.hoisted(() => ({ enabled: false, invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => native.enabled,
  invoke: native.invoke,
}));

const query: ModelResearchQuery = {
  code: "SP500", start: "2026-09-01", end: "2026-09-30",
  qualityPolicy: "allow_unverified", fastWindow: 5, slowWindow: 20,
};
const sourceHash = "a".repeat(64);
const runHash = "b".repeat(64);
const outputHash = "c".repeat(64);
function isoDay(index: number): string {
  return new Date(Date.UTC(2026, 8, index + 1)).toISOString().slice(0, 10);
}
function sample(): ModelResearchResult {
  const points: ModelResearchPoint[] = Array.from({ length: 25 }, (_, i) => {
    const fast = i >= 4 ? String(100 + 2 * (i - 2)) : null;
    const slow = i >= 19 ? String(100 + 2 * (i - 9.5)) : null;
    return {
      session_date: isoDay(i), close: String(100 + 2 * i),
      fast_sma: fast, slow_sma: slow,
      signal_after_close: slow === null ? null : "long",
    };
  });
  return {
    execution_contract: "model-research-evaluation-v1",
    lineage: {
      model_id: BUILTIN_MODEL_ID, model_version: BUILTIN_MODEL_VERSION,
      implementation_revision: "sma-crossover-observed-close-v1",
      parameters: { fast_window: 5, slow_window: 20 },
      source_identity: {
        instrument_kind: "index", code: "SP500", provider: "fred",
        adjustment: "price_index", return_semantics: "price_index",
        frequency: "daily", units: "index_points",
      },
      source_snapshot_sha256: sourceHash,
      analysis_calculation_version: "index-analytics-v2",
      requested_start: "2026-09-01", requested_end: "2026-09-30",
      first_observed_session: points[0].session_date,
      last_observed_session: points[24].session_date,
      quality_policy: "allow_unverified",
      quality_status: "stale_or_partial",
      quality_verified: false,
      point_in_time_membership: "not_used",
      dividends_included: false,
      corporate_action_adjustment: "none_index_price_level",
    },
    reproducibility_sha256: runHash, output_sha256: outputHash,
    evaluated_at_utc: "2026-11-03T10:00:00+00:00",
    status: "completed", observations: points.length, points,
    quality_warning: "Unverified data: research-only results",
    read_only: true, no_network: true, no_synthetic_sessions: true,
    signal_after_close_only: true, backtest_validated: false, persistent_job: false,
  };
}
beforeEach(() => { native.enabled = false; native.invoke.mockReset(); });
afterEach(() => vi.unstubAllGlobals());

describe("Built-in model client and no-trading contract", () => {
  it("rejects invalid semantics, dates, windows and volatile index before I/O", () => {
    expect(() => validateModelResearchQuery(query)).not.toThrow();
    expect(() => validateModelResearchQuery({ ...query, code: "VIX" })).toThrow();
    expect(() => validateModelResearchQuery({ ...query, fastWindow: 20 })).toThrow();
    expect(() => validateModelResearchQuery({ ...query, slowWindow: 253 })).toThrow();
    expect(() => validateModelResearchQuery({ ...query, start: "2026-09-31" })).toThrow();
    expect(() => validateModelResearchQuery({ ...query, end: "2026-08-01" })).toThrow();
    expect(() => validateModelResearchQuery({ ...query, expectedSnapshot: "123" })).toThrow();
  });

  it("preserves strict source identity, version and unverified research-only lineage", () => {
    const valid = sample();
    expect(parseModelResearchResult(valid, query).points).toHaveLength(25);
    const wrongVersion = sample();
    wrongVersion.lineage.model_version = "2.0";
    expect(() => parseModelResearchResult(wrongVersion, query)).toThrow();
    const wrongCode = sample();
    wrongCode.lineage.source_identity.code = "DJIA";
    expect(() => parseModelResearchResult(wrongCode, query)).toThrow();
    const wrongSemantics = sample();
    wrongSemantics.lineage.source_identity.return_semantics =
      "volatility_index" as "price_index";
    expect(() => parseModelResearchResult(wrongSemantics, query)).toThrow();
    const bogusQuality = sample();
    bogusQuality.quality_warning = null;
    expect(() => parseModelResearchResult(bogusQuality, query)).toThrow();
    const unsupportedBacktest = sample();
    unsupportedBacktest.backtest_validated = true as false;
    expect(() => parseModelResearchResult(unsupportedBacktest, query)).toThrow();
  });

  it("rejects fabricated early signals, reordered dates, or contradictory SMAs", () => {
    const early = sample();
    early.points[4].signal_after_close = "long";
    expect(() => parseModelResearchResult(early, query)).toThrow();
    const shifted = sample();
    shifted.points[20].session_date = shifted.points[19].session_date;
    expect(() => parseModelResearchResult(shifted, query)).toThrow();
    const falseLong = sample();
    falseLong.points[21].fast_sma = "1";
    expect(() => parseModelResearchResult(falseLong, query)).toThrow();
    const invented = sample();
    invented.no_synthetic_sessions = false as true;
    expect(() => parseModelResearchResult(invented, query)).toThrow();
    const wrongCoverage = sample();
    wrongCoverage.lineage.first_observed_session = "2026-09-02";
    expect(() => parseModelResearchResult(wrongCoverage, query)).toThrow();
  });

  it("refuses changed local source identity when caller pins its revision", () => {
    const pinned = { ...query, expectedSnapshot: sourceHash };
    expect(parseModelResearchResult(sample(), pinned).lineage.source_snapshot_sha256)
      .toBe(sourceHash);
    const changed = sample();
    changed.lineage.source_snapshot_sha256 = "0".repeat(64);
    expect(() => parseModelResearchResult(changed, pinned)).toThrow();
  });

  it("only executes through a managed local Tauri command", async () => {
    native.enabled = true;
    native.invoke.mockResolvedValue(sample());
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const result = await fetchModelResearch(query);
    expect(result.status).toBe("completed");
    expect(native.invoke).toHaveBeenCalledWith("model_research_evaluate", {
      code: "SP500", start: query.start, end: query.end,
      qualityPolicy: "allow_unverified", fastWindow: 5, slowWindow: 20,
      expectedSnapshot: null,
    });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("does not allow a browser-only endpoint or cloud fallback", async () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    await expect(fetchModelResearch(query)).rejects.toThrow("managed FinIntel desktop");
    expect(fetcher).not.toHaveBeenCalled();
    native.enabled = true;
    native.invoke.mockRejectedValue(new Error("Local sidecar unavailable"));
    await expect(fetchModelResearch(query)).rejects.toThrow("Local sidecar unavailable");
  });
});

describe("Bilingual model research UI", () => {
  it("starts collapsed and avoids implicit execution", () => {
    const markup = renderToStaticMarkup(
      <ModelResearchPanel code="SP500" start={query.start} end={query.end}
        qualityPolicy="allow_unverified" />,
    );
    expect(markup).toContain("Local model research");
    expect(markup).toContain("Explore model research");
    expect(markup).not.toContain("Execute order");
  });

  it("labels no-backtest, execution timing, and private research in both languages", () => {
    for (const key of [
      "Local model research",
      "Research signals only",
      "Evaluate local indicator",
      "SMA signals are generated after the observed close and cannot be traded at that same close. Dividends, slippage and membership backtests are not modeled.",
      "This evaluation is not a durable job or backtest; no equity curve or trading performance is claimed.",
    ]) expect(translate("zh-CN", key)).not.toBe(key);
  });
});
