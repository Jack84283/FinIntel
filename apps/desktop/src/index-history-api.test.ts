import { afterEach, describe, expect, it, vi } from "vitest";
import { INDEX_CODES, listIndexHistory, readIndexHistory } from "./index-history-api";

afterEach(() => vi.unstubAllGlobals());

function fakeFetch(value: unknown) {
  return vi.fn(async () => ({ ok: true, json: async () => value }));
}

describe("local index history API", () => {
  it("lists all supported indexes without requesting external data", async () => {
    const catalog = INDEX_CODES.map((code) => ({
      code, name: code === "SP500" ? "S&P 500" : code, fred_series: code,
      provider: "fred", source_url: "https://fred.stlouisfed.org/series/" + code,
      series_type: code === "VIX" ? "volatility" : "price", frequency: "daily",
      currency: null, units: "index_points", licensing_note: "FRED terms apply",
      known_history_limit: "Provider coverage", coverage_updated_at: null,
      local_first_session: null, local_last_session: null,
      provider_earliest_available: null, provider_latest_available: null,
      sessions: 0, first_session: null, last_session: null, data_kind: "daily_close_only",
    }));
    const request = fakeFetch(catalog);
    vi.stubGlobal("fetch", request);

    const result = await listIndexHistory();

    expect(result.map((item) => item.code)).toEqual(INDEX_CODES);
    expect(request).toHaveBeenCalledWith("/api/v1/market-data/index-history", { cache: "no-store" });
  });

  it("parses close-only history and preserves an empty local history", async () => {
    vi.stubGlobal("fetch", fakeFetch({
      code: "SP500", name: "S&P 500", data_kind: "daily_close_only", unit: "index_points",
      provider: "fred", source_url: "https://fred.stlouisfed.org/series/SP500", has_more: false,
      observations: [{ session_date: "2026-10-27", close: "6878.25", open: "ignored", volume: 123 }],
    }));

    const history = await readIndexHistory("SP500", 100);
    expect(history.observations).toEqual([{ session_date: "2026-10-27", close: "6878.25" }]);
    expect((history.observations[0] as unknown as Record<string, unknown>).volume).toBeUndefined();

    vi.stubGlobal("fetch", fakeFetch({
      code: "VIX", name: "Cboe VIX", data_kind: "daily_close_only", unit: "index_points",
      provider: "fred", source_url: "https://fred.stlouisfed.org/series/VIXCLS", has_more: false,
      observations: [],
    }));
    await expect(readIndexHistory("VIX")).resolves.toMatchObject({ code: "VIX", observations: [] });
  });
});
