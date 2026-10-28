import { describe, expect, it } from "vitest";
import { parseDaily, parseImports, parseInstruments, sortableBars } from "./market";

const observed = {
  symbol: "IBM",
  provider: "alpha_vantage",
  frequency: "1d",
  price_adjustment: "raw_unadjusted",
  data_kind: "historical_eod",
  source_url: "https://www.alphavantage.co/query",
  provider_terms_url: "https://www.alphavantage.co/terms_of_service/",
  retrieved_at: "2026-10-22T13:00:00+00:00",
  provider_last_refreshed: "2026-10-21",
  has_more: false,
  observations: [
    { session_date: "2026-10-19", open: "100.10", high: "104", low: "99", close: "103.10", volume: 100 },
    { session_date: "2026-10-20", open: "103.10", high: "107", low: "102", close: "105.22", volume: 200 },
  ],
};

describe("historical market API contracts", () => {
  it("accepts chronological decimal-valued OHLCV records", () => {
    const data = parseDaily(observed);
    expect(data.observations).toHaveLength(2);
    expect(data.observations[0].open).toBe("100.10");
  });

  it("accepts finite Decimal exponent notation from SQLite text values", () => {
    const data = parseDaily({
      ...observed,
      observations: [{
        session_date: "2026-10-19", open: "1E+3", high: "1.1E+3",
        low: "9E+2", close: "1.03E+3", volume: 100,
      }],
    });
    expect(data.observations[0].close).toBe("1.03E+3");
  });

  it("rejects invalid history, symbols, and oversized results", () => {
    expect(() => parseDaily(null)).toThrow();
    expect(() => parseDaily({ ...observed, observations: [...observed.observations].reverse() })).toThrow();
    expect(() => parseDaily({ ...observed, observations: [
      { ...observed.observations[0], high: "1" },
    ] })).toThrow();
    expect(() => parseDaily({ ...observed, observations: [
      { ...observed.observations[0], volume: -1 },
    ] })).toThrow();
    expect(() => parseDaily({ ...observed, has_more: "false" })).toThrow();
  });

  it("sorts without changing backend data", () => {
    const bars = parseDaily(observed).observations;
    const descending = sortableBars(bars, "session_date", true);
    expect(descending[0].session_date).toBe("2026-10-20");
    expect(bars[0].session_date).toBe("2026-10-19");
    expect(sortableBars(bars, "volume", false)[0].volume).toBe(100);
  });

  it("validates catalog and import history", () => {
    const catalog = parseInstruments([{
      symbol: "IBM", provider: "alpha_vantage", frequency: "1d", adjustment: "raw_unadjusted",
      data_kind: "historical_eod", sessions: 2, first_session: "2026-10-19", last_session: "2026-10-20",
    }]);
    expect(catalog[0].symbol).toBe("IBM");
    expect(parseImports([{
      id: 1, symbol: "IBM", provider: "alpha_vantage", imported_at: "2026-10-22",
      retrieved_at: "2026-10-22", first_session: "2026-10-19", last_session: "2026-10-20",
      observations: 2, inserted: 2, updated: 0, unchanged: 0, stale: 0,
    }])).toHaveLength(1);
    expect(() => parseInstruments([{ symbol: "IBM", sessions: "2" }])).toThrow();
  });
});


describe("long-horizon daily records", () => {
  it("accepts a chronological 10-year-sized response without truncating", () => {
    const entries = Array.from({ length: 2520 }, (_, index) => {
      const d = new Date(Date.UTC(2016, 0, 1 + index * 1.45));
      return {
        session_date: d.toISOString().slice(0, 10),
        open: "100", high: "110", low: "90", close: "105", volume: 1000,
      };
    });
    // The synthetic calendar is for contract sizing only, not market history.
    const data = parseDaily({ ...observed, observations: entries });
    expect(data.observations).toHaveLength(2520);
    expect(() => parseDaily({
      ...observed,
      observations: [...entries, ...entries],
    })).toThrow();
  });
});
