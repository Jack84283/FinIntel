import { describe, expect, it } from "vitest";
import { mergeAcquirableResults, parseCatalogSync, parseSymbolSearch } from "./symbol-catalog-api";

describe("local Stock / ETF directory contracts", () => {
  it("accepts a typed catalog result from the backend", () => {
    const data = parseSymbolSearch({
      source: "starter", updated_at: null, total: 2,
      results: [
        { symbol: "AAPL", name: "Apple", asset_type: "Stock", exchange: "" },
        { symbol: "SPY", name: "SPDR S&P 500 ETF Trust", asset_type: "ETF", exchange: "" },
      ],
    });
    expect(data.results.map((item) => item.asset_type)).toEqual(["Stock", "ETF"]);
    expect(data.source).toBe("starter");
    const expanded = parseSymbolSearch({
      source: "starter", updated_at: null, total: 4,
      results: [
        { symbol: "^GSPC", name: "S&P 500 Index", asset_type: "Index", exchange: "" },
        { symbol: "FXAIX", name: "Fidelity 500 Index Fund", asset_type: "Mutual Fund", exchange: "" },
      ],
    });
    expect(expanded.results.map((x) => x.asset_type)).toEqual(["Index", "Mutual Fund"]);
  });

  it("only offers Stock and ETF history symbols, excluding indexes and mutual funds", () => {
    const stocks = parseSymbolSearch({
      source: "starter", updated_at: null, total: 4,
      results: [
        { symbol: "AAPL", name: "Apple", asset_type: "Stock", exchange: "" },
        { symbol: "^GSPC", name: "S&P 500 Index", asset_type: "Index", exchange: "" },
      ],
    });
    const etfs = parseSymbolSearch({
      source: "starter", updated_at: null, total: 4,
      results: [
        { symbol: "SPY", name: "SPDR S&P 500 ETF", asset_type: "ETF", exchange: "" },
        { symbol: "FXAIX", name: "Fidelity 500 Fund", asset_type: "Mutual Fund", exchange: "" },
      ],
    });
    const combined = mergeAcquirableResults(stocks, etfs, "", 40);
    expect(combined.results.map(({ symbol }) => symbol)).toEqual(["AAPL", "SPY"]);
    expect(mergeAcquirableResults(stocks, etfs, "", 1).results).toHaveLength(1);
  });

  it("validates sync results and rejects malformed search data", () => {
    expect(parseCatalogSync({
      status: "synced", source: "alpha_vantage", updated_at: "2026-10-22T20:10:00Z", total: 10000,
    }).total).toBe(10000);
    expect(() => parseSymbolSearch({
      source: "starter", updated_at: null, total: 1, results: [
        { symbol: "AAPL", name: "Apple", asset_type: "Coin", exchange: "" },
      ],
    })).toThrow();
    expect(() => parseCatalogSync({ status: "synced", total: 0 })).toThrow();
  });
});
