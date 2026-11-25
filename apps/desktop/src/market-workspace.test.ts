import { describe, expect, it } from "vitest";
import type { MarketSelection } from "./market-selection";
import { closeMarketTab, MARKET_TAB_LIMIT, openMarketTab, parseMarketWorkspace } from "./market-workspace";

const stock: MarketSelection = { kind: "equity", symbol: "AAPL", provider: "tiingo" };

describe("market workspace preferences", () => {
  it("defaults to SP500 when no valid tab remains", () => {
    expect(parseMarketWorkspace({ active: "bad", tabs: [null, {}, { selection: { kind: "index", code: "OTHER" } }] }).active).toBe("fred:SP500");
  });

  it("validates, canonicalizes, deduplicates, bounds and removes extraneous persisted values", () => {
    const result = parseMarketWorkspace({
      active: "alpha_vantage:AAPL",
      tabs: [
        { selection: { ...stock, symbol: "aapl" }, view: {
          period: "invalid", page: -1, sortColumn: "arbitrary", start: "2024-02-30",
          end: "2024-03-01", tableFilter: "x".repeat(200), observations: [10], credentials: "secret",
        } },
        { selection: stock },
        { selection: { ...stock, provider: "alpha_vantage" }, view: {
          period: "custom", start: "2024-04-01", end: "2024-03-01", draftStart: "2024-02-29",
        } },
        { selection: { ...stock, provider: "invalid" } },
        ...Array.from({ length: 10 }, (_, i) => ({ selection: { ...stock, symbol: "TEST" + i } })),
      ],
    });
    expect(result.tabs).toHaveLength(MARKET_TAB_LIMIT);
    expect(result.active).toBe("alpha_vantage:AAPL");
    expect(result.tabs[0]).toMatchObject({
      selection: stock,
      view: { period: "recent", start: "", end: "2024-03-01", sortColumn: "session_date", page: 0, tableFilter: "x".repeat(80) },
    });
    expect(result.tabs[1].view).toMatchObject({ period: "custom", start: "", end: "", draftStart: "2024-02-29" });
    expect(JSON.stringify(result)).not.toMatch(/observations|credentials|secret/);
  });

  it("retains matching tabs and chooses the adjacent tab when closing the active one", () => {
    const a = openMarketTab(parseMarketWorkspace(null), stock, "new");
    const b = openMarketTab(a, { kind: "index", code: "VIX" }, "new");
    const c = openMarketTab(b, stock, "current");
    expect(c.active).toBe("tiingo:AAPL");
    expect(c.tabs).toEqual(b.tabs);
    expect(closeMarketTab(c, c.active).active).toBe("fred:VIX");
    expect(closeMarketTab(c, "fred:VIX").active).toBe(c.active);
  });
});
