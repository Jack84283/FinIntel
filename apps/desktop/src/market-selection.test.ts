import { describe, expect, it } from "vitest";
import { DEFAULT_MARKET_SELECTION, resolveMarketSelection } from "./market-selection";

describe("Market selection persistence", () => {
  it("defaults a first Market visit to SP500", () => {
    expect(resolveMarketSelection(null)).toEqual({ kind: "index", code: "SP500" });
    expect(DEFAULT_MARKET_SELECTION).toEqual({ kind: "index", code: "SP500" });
  });

  it("preserves the current selection unless explicit navigation overrides it", () => {
    const stock = { kind: "equity" as const, symbol: "AAPL", provider: "tiingo" as const };
    expect(resolveMarketSelection(stock)).toEqual(stock);
    expect(resolveMarketSelection(stock, { kind: "index", code: "VIX" })).toEqual({ kind: "index", code: "VIX" });
    expect(resolveMarketSelection({ kind: "index", code: "NDX" }, stock)).toEqual(stock);
  });
});
