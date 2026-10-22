import { describe, expect, it } from "vitest";
import { parseCatalogSync, parseSymbolSearch } from "./symbol-catalog-api";

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
