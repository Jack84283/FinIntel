import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { fetchIndexBasket, parseBasketResult, validateBasketQuery,
  type BasketQuery, type BasketResult, type BasketValues } from "./index-basket-api";
import { IndexBasketPanel, BasketChart, basketCoverage, basketMetricCell, basketMetricLines, decimateBasketLines } from "./IndexBasketPanel";
import { translate } from "./localization";
import type { IndexCatalogRecord } from "./index-history-api";

const native = vi.hoisted(() => ({ enabled: false, invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => native.enabled,
  invoke: native.invoke,
}));
const query: BasketQuery = {
  codes: ["SP500", "DJIA", "NASDAQCOM"],
  start: "2026-10-10", end: "2026-10-12",
  window: 20, qualityPolicy: "allow_unverified",
};
const sha = "f".repeat(64);
function example(): BasketResult {
  const codes = [...query.codes];
  const rows = [100, 110, 99].map((v, index) => ({
    session_date: "2026-10-" + String(10 + index),
    values: Object.fromEntries(codes.map((code, i) => [code, {
      normalized_100: String(v + i * index),
      simple_return: index ? String(index * 0.03) : null,
      drawdown: index === 2 ? "-0.1" : "0",
      rolling_volatility_annualized: null,
      relative_strength: i === 0 ? null : String(1 + i * 0.01 * index),
      rolling_correlation: null,
    }])) as Record<string, BasketValues>,
  }));
  return {
    calculation_version: "index-analytics-v1", codes, baseline_code: codes[0],
    request: { start: query.start, end: query.end, window_sessions: 20 },
    quality_policy: query.qualityPolicy, quality: { quality_verified: false, status: "stale_or_partial" },
    unverified_data: true, alignment: "intersection", common_sessions: rows.length,
    first_common_session: rows[0].session_date, last_common_session: rows.at(-1)!.session_date,
    series: codes.map(code => ({
      identity: { instrument_kind: "index", code, provider: "fred", adjustment: "price_index",
        return_semantics: "price_index", frequency: "daily", units: "index_points" },
      source: { source_url: "https://fred.stlouisfed.org/series/" + code,
        coverage_updated_at: null, snapshot_sha256: sha },
      stored_sessions: 4, excluded_nonshared_sessions: 1,
    })),
    points: rows, notes: ["No forward fill"],
    read_only: true, no_network: true, no_synthetic_sessions: true,
  };
}
beforeEach(() => { native.enabled = false; native.invoke.mockReset(); });
afterEach(() => vi.unstubAllGlobals());

describe("Issue 31 batch local index APIs", () => {
  it("rejects VIX, duplicates, too many indices, invalid ranges and windows before network", () => {
    for (const codes of [
      ["SP500", "VIX"], ["SP500", "SP500"],
      ["SP500"], ["SP500", "DJIA", "NDX", "NASDAQCOM", "VIX"],
    ]) expect(() => validateBasketQuery({ ...query, codes: codes as BasketQuery["codes"] })).toThrow();
    expect(() => validateBasketQuery({ ...query, start: "2026-02-30" })).toThrow();
    expect(() => validateBasketQuery({ ...query, end: "2026-10-01" })).toThrow();
    expect(() => validateBasketQuery({ ...query, window: 7 as BasketQuery["window"] })).toThrow();
  });

  it("uses local same-origin route only with immutable query parameters", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => example() });
    vi.stubGlobal("fetch", fetcher);
    const data = await fetchIndexBasket(query);
    expect(data.common_sessions).toBe(3);
    const [url, options] = fetcher.mock.calls[0];
    expect(url).toContain("codes=SP500%2CDJIA%2CNASDAQCOM");
    expect(url).toContain("quality_policy=allow_unverified");
    expect(options).toEqual({ cache: "no-store" });
  });

  it("calls the restricted managed desktop command without invoking HTTP", async () => {
    native.enabled = true;
    native.invoke.mockResolvedValue(example());
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    expect((await fetchIndexBasket(query)).codes).toEqual(query.codes);
    expect(native.invoke).toHaveBeenCalledWith("analytics_basket", {
      codes: query.codes, start: query.start, end: query.end,
      window: 20, qualityPolicy: "allow_unverified",
    });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("preserves offline, quality policy and entitlement failures as errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("Offline backend")));
    await expect(fetchIndexBasket(query)).rejects.toThrow("Offline backend");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false, status: 422,
      json: async () => ({ detail: "Current complete Data Quality scan required" }),
    }));
    await expect(fetchIndexBasket({ ...query, qualityPolicy: "require_complete" }))
      .rejects.toThrow("Current complete Data Quality scan required");
  });

  it("rejects wrong source, missing/duplicate dates, bad hashes and fabricated numbers", () => {
    const wrong = example();
    wrong.series[1].identity.provider = "unknown";
    expect(() => parseBasketResult(wrong, query)).toThrow();
    const forged = example();
    forged.points[0].values.DJIA!.normalized_100 = "NaN";
    expect(() => parseBasketResult(forged, query)).toThrow();
    const dates = example();
    dates.points[1].session_date = dates.points[0].session_date;
    expect(() => parseBasketResult(dates, query)).toThrow();
    const hash = example();
    hash.series[1].source.snapshot_sha256 = "";
    expect(() => parseBasketResult(hash, query)).toThrow();
    const mismatched = example();
    mismatched.baseline_code = "NDX";
    expect(() => parseBasketResult(mismatched, query)).toThrow();
    expect(parseBasketResult(example(), query).points).toHaveLength(3);
    const strict = { ...query, qualityPolicy: "require_complete" as const };
    const unverified = example();
    unverified.quality_policy = "require_complete";
    expect(() => parseBasketResult(unverified, strict)).toThrow();
  });
});

describe("multi-index chart/grid consistency and date envelope", () => {
  it("calculates chart and grid cells from exactly the same returned values", () => {
    const data = example(), series = basketMetricLines(data, "normalized_100");
    expect(series).toHaveLength(3);
    for (const line of series) {
      for (const item of line.points) {
        expect(item.value).toBe(Number(basketMetricCell(data, "normalized_100", item.date, line.code)));
      }
    }
    const layout = renderToStaticMarkup(<BasketChart result={data} metric="normalized_100" />);
    expect(layout).toContain("SP500");
    expect(layout).toContain("NASDAQCOM");
    expect(layout).toContain("Multi-index historical comparison chart");
    expect(renderToStaticMarkup(<IndexBasketPanel catalog={[]} catalogError="" />))
      .toContain("Open multi-index comparison");
    const longLine = { ...series[0], points: Array.from({ length: 750 }, (_, i) => ({
      date: "2026-10-10", value: i,
    })) };
    const sample = decimateBasketLines([longLine])[0].points;
    expect(sample).toHaveLength(450);
    expect(sample[0].value).toBe(0);
    expect(sample.at(-1)?.value).toBe(749);
  });

  it("requires overlapping local coverage and never invents last-common dates", () => {
    const catalog = [
      { code: "SP500", first_session: "2026-01-01", last_session: "2026-10-20" },
      { code: "DJIA", first_session: "2026-03-01", last_session: "2026-10-18" },
      { code: "NASDAQCOM", first_session: "2026-02-01", last_session: "2026-10-19" },
    ] as IndexCatalogRecord[];
    expect(basketCoverage(catalog, query.codes)).toEqual({
      first: "2026-03-01", last: "2026-10-18",
    });
    expect(basketCoverage(catalog, ["SP500", "VIX"])).toBeNull();
    expect(basketCoverage(catalog, ["SP500", "SP500"])).toBeNull();
    expect(translate("zh-CN", "Multi-index comparison")).not.toBe("Multi-index comparison");
  });
});
