import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchConstituents, fetchIndexes, fetchMembershipChanges, fetchStockIndexes } from "./index-api";

afterEach(() => vi.unstubAllGlobals());

function fakeFetch(value: unknown) {
  return vi.fn(async () => ({
    ok: true,
    json: async () => value,
  }));
}

describe("index membership API contracts", () => {
  it("preserves unknown snapshot vs known nonmembership", async () => {
    const statuses = ["SP500", "DJIA", "NDX", "RUT"].map((code, index) => ({
      code,
      name: code,
      requested_as_of: "2026-10-20",
      snapshot_as_of: index === 0 ? "2026-10-15" : null,
      source: index === 0 ? "synthetic-test" : null,
      members: index === 0 ? 1 : null,
      source_kind: index === 0 ? "verified_public" : null,
      source_as_of: index === 0 ? "2026-10-15" : null,
      expected_count: index === 0 ? 1 : null,
      validation_status: index === 0 ? "complete" : null,
      availability_status: index === 0 ? "available" : "not_imported",
      confidence: index === 0 ? "medium" : null,
      status: index === 0 ? "available" : "not_imported",
    }));
    vi.stubGlobal("fetch", fakeFetch(statuses));
    const list = await fetchIndexes("2026-10-20");
    expect(list[0].members).toBe(1);
    expect(list[1].members).toBeNull();
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("as_of=2026-10-20"), { cache: "no-store" });
    vi.stubGlobal("fetch", fakeFetch({
      symbol: "AAPL", requested_as_of: "2026-10-20",
      indexes: statuses.map((entry, index) => ({
        ...entry, is_member: index === 0 ? false : null,
      })),
    }));
    const stock = await fetchStockIndexes("aapl", "2026-10-20");
    expect(stock.indexes[0].is_member).toBe(false);
    expect(stock.indexes[1].is_member).toBeNull();
  });

  it("parses a page with cross-index memberships", async () => {
    vi.stubGlobal("fetch", fakeFetch({
      code: "SP500", name: "S&P 500", requested_as_of: "2026-10-20",
      snapshot_as_of: "2026-10-15", source: "synthetic",
      source_kind: "verified_public", source_as_of: "2026-10-15", expected_count: 2,
      validation_status: "complete", availability_status: "available", confidence: "medium",
      snapshot_members: 2, total: 1, limit: 50, offset: 0,
      items: [{ symbol: "AAPL", name: "Apple", asset_type: "Stock", member_indexes: ["NDX", "SP500"] }],
    }));
    const page = await fetchConstituents("SP500", "2026-10-20", "AAPL");
    expect(page.items[0].member_indexes).toEqual(["NDX", "SP500"]);
    expect(page.snapshot_as_of).toBe("2026-10-15");
  });

  it("rejects impossible ranges and malformed data before making claims", async () => {
    await expect(fetchConstituents("SP500", "", "x".repeat(81))).rejects.toThrow();
    await expect(fetchStockIndexes("^GSPC")).rejects.toThrow();
    vi.stubGlobal("fetch", fakeFetch({
      code: "SP500", name: "S&P", requested_as_of: "2026-10-20",
      snapshot_as_of: null, source: null, snapshot_members: 0,
      source_kind: null, source_as_of: null, expected_count: null,
      validation_status: null, availability_status: "not_imported", confidence: null,
      total: 1, limit: 50, offset: 0, items: [{ symbol: "AAPL", member_indexes: ["FAKE"] }],
    }));
    await expect(fetchConstituents("SP500", "2026-10-20")).rejects.toThrow();
  });

  it("parses effective membership changes", async () => {
    vi.stubGlobal("fetch", fakeFetch({
      code: "SP500", name: "S&P 500", from_requested_as_of: "2026-10-01",
      to_requested_as_of: "2026-10-20", from_snapshot_as_of: "2026-10-01",
      to_snapshot_as_of: "2026-10-15", from_source: "source-01", to_source: "source-15",
      status: "available", total: 1, limit: 50, offset: 0,
      items: [{ symbol: "NVDA", name: "NVIDIA", asset_type: "Stock", change: "added" }],
    }));
    const changes = await fetchMembershipChanges("SP500", "2026-10-01", "2026-10-20");
    expect(changes.items[0].change).toBe("added");
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("from_as_of=2026-10-01"), { cache: "no-store" });
  });
});
