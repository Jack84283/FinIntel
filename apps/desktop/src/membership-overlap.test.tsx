import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  fetchMembershipOverlap, parseOverlapResult, validateOverlapQuery,
  type OverlapQuery, type OverlapResult, type OverlapSource,
} from "./membership-overlap-api";
import {
  ConstituentOverlapPanel, OverlapSourceDates, limitOverlapSearch, membershipIndexForMarket, overlapAsOfDate, overlapQueryForSelection, overlapStatusDescription,
} from "./ConstituentOverlapPanel";
import { translate } from "./localization";

const native = vi.hoisted(() => ({ enabled: false, invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => native.enabled,
  invoke: native.invoke,
}));
const query: OverlapQuery = {
  left: "SP500", right: "NDX", asOf: "2026-10-20", q: "", limit: 50, offset: 0,
};
function source(code: "SP500" | "NDX"): OverlapSource {
  return {
    code,
    name: code,
    requested_as_of: query.asOf,
    snapshot_as_of: query.asOf,
    source: "verified-test-snapshot",
    source_kind: "verified_public",
    source_as_of: query.asOf,
    validation_status: "complete",
    availability_status: "available",
    confidence: "medium",
    expected_count: 3,
    member_count: 3,
  };
}
function example(): OverlapResult {
  return {
    left_code: "SP500", right_code: "NDX", requested_as_of: query.asOf,
    sources: [source("SP500"), source("NDX")],
    status: "verified_snapshot", common_members: 2, union_members: 4,
    jaccard_ratio: "0.5", query: "", limit: 50, offset: 0, filtered_total: 2,
    items: [
      { symbol: "AAPL", name: "Apple", asset_type: "Stock" },
      { symbol: "NVDA", name: "NVIDIA", asset_type: "Stock" },
    ],
    snapshot_only: true, no_lookahead: true, read_only: true,
  };
}
beforeEach(() => { native.enabled = false; native.invoke.mockReset(); });
afterEach(() => vi.unstubAllGlobals());

describe("Dated membership overlap API", () => {
  it("refuses identical codes, impossible dates and unbounded searches before I/O", () => {
    expect(() => validateOverlapQuery({ ...query, right: "SP500" })).toThrow();
    expect(() => validateOverlapQuery({ ...query, asOf: "2026-02-30" })).toThrow();
    expect(() => validateOverlapQuery({ ...query, q: "x".repeat(81) })).toThrow();
    expect(() => validateOverlapQuery({ ...query, q: "中".repeat(80) })).not.toThrow();
    expect(() => validateOverlapQuery({ ...query, q: "中".repeat(81) })).toThrow();
    expect(() => validateOverlapQuery({ ...query, q: "📈".repeat(80) })).not.toThrow();
    expect(() => validateOverlapQuery({ ...query, q: "📈".repeat(81) })).toThrow();
    expect(() => validateOverlapQuery({ ...query, q: "\n" })).toThrow();
    expect(() => validateOverlapQuery({ ...query, limit: 101 })).toThrow();
    expect(() => validateOverlapQuery({ ...query, offset: -1 })).toThrow();
    expect(() => validateOverlapQuery({ ...query, left: "VIX" as OverlapQuery["left"] })).toThrow();
  });

  it("limits pasted Unicode search text by code point without splitting emoji", () => {
    expect(limitOverlapSearch("中".repeat(80))).toBe("中".repeat(80));
    expect(limitOverlapSearch("中".repeat(81))).toBe("中".repeat(80));
    expect(limitOverlapSearch("📈".repeat(80))).toBe("📈".repeat(80));
    expect(limitOverlapSearch("📈".repeat(81))).toBe("📈".repeat(80));
    const mixed = "A".repeat(79) + "📈" + "B";
    expect(limitOverlapSearch(mixed)).toBe("A".repeat(79) + "📈");
    expect([...limitOverlapSearch(mixed)]).toHaveLength(80);
    expect(limitOverlapSearch("📈".repeat(80)).length).toBe(160);
    const overlapQuery = { ...query, q: limitOverlapSearch("📈".repeat(81)) };
    expect(() => validateOverlapQuery(overlapQuery)).not.toThrow();
  });

  it("uses a fixed same-origin read-only URL for browser development", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => example() });
    vi.stubGlobal("fetch", fetcher);
    const result = await fetchMembershipOverlap(query);
    expect(result.common_members).toBe(2);
    const [url, options] = fetcher.mock.calls[0];
    expect(url).toContain("/api/v1/market-data/indexes/overlap?");
    expect(url).toContain("as_of=2026-10-20");
    expect(url).toContain("left=SP500");
    expect(url).toContain("right=NDX");
    expect(options).toEqual({ cache: "no-store" });
  });

  it("calls only managed native IPC, not raw HTTP from packaged desktop", async () => {
    native.enabled = true;
    native.invoke.mockResolvedValue(example());
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    expect((await fetchMembershipOverlap(query)).status).toBe("verified_snapshot");
    expect(native.invoke).toHaveBeenCalledWith("index_membership_overlap", {
      left: "SP500", right: "NDX", asOf: "2026-10-20",
      q: "", limit: 50, offset: 0,
    });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("passes an 80-character Chinese company search unchanged to native IPC", async () => {
    native.enabled = true;
    const chineseQuery = { ...query, q: "中".repeat(80) };
    native.invoke.mockRejectedValue(new Error("No matching local symbols"));
    await expect(fetchMembershipOverlap(chineseQuery)).rejects.toThrow("No matching local symbols");
    expect(native.invoke).toHaveBeenCalledWith("index_membership_overlap", {
      left: "SP500", right: "NDX", asOf: query.asOf,
      q: chineseQuery.q, limit: 50, offset: 0,
    });
  });

  it("preserves offline and backend policy errors instead of inventing membership", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("Backend unavailable")));
    await expect(fetchMembershipOverlap(query)).rejects.toThrow("Backend unavailable");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false, status: 422, json: async () => ({ detail: "Future date rejected" }),
    }));
    await expect(fetchMembershipOverlap(query)).rejects.toThrow("Future date rejected");
  });

  it("rejects source lookahead, misleading verified state and incoherent counts", () => {
    const future = example();
    future.sources[0].snapshot_as_of = "2026-10-21";
    expect(() => parseOverlapResult(future, query)).toThrow();
    const earlier = example();
    earlier.sources[1].snapshot_as_of = "2026-10-01";
    earlier.sources[1].source_as_of = "2026-10-01";
    expect(() => parseOverlapResult(earlier, query)).toThrow();
    earlier.status = "earlier_snapshot";
    expect(parseOverlapResult(earlier, query).status).toBe("earlier_snapshot");
    const oldEvidence = example();
    oldEvidence.sources[1].source_as_of = "2026-10-01";
    expect(() => parseOverlapResult(oldEvidence, query)).toThrow();
    oldEvidence.status = "earlier_snapshot";
    expect(parseOverlapResult(oldEvidence, query).status).toBe("earlier_snapshot");
    const proxy = example();
    proxy.sources[1].source_kind = "fund_derived_proxy";
    proxy.sources[1].availability_status = "proxy_only";
    expect(() => parseOverlapResult(proxy, query)).toThrow();
    proxy.status = "proxy_only";
    expect(parseOverlapResult(proxy, query).status).toBe("proxy_only");
    const wrongRatio = example();
    wrongRatio.jaccard_ratio = "0.9";
    expect(() => parseOverlapResult(wrongRatio, query)).toThrow();
    const forgedUnion = example();
    forgedUnion.union_members = 20;
    forgedUnion.jaccard_ratio = "0.1";
    expect(() => parseOverlapResult(forgedUnion, query)).toThrow("Invalid sourced overlap counts.");
    const missingCount = example();
    missingCount.sources[0].member_count = null;
    expect(() => parseOverlapResult(missingCount, query)).toThrow("Invalid sourced overlap counts.");
    const invalid = example();
    invalid.items[0].symbol = "AAPL";
    invalid.items[1].symbol = "AAPL";
    expect(() => parseOverlapResult(invalid, query)).toThrow();
  });

  it("treats missing data as unknown, never zero overlap", () => {
    const absent = example();
    absent.sources[1] = {
      ...absent.sources[1], snapshot_as_of: null, source_as_of: null,
      source: null, source_kind: null, validation_status: null,
      confidence: null, availability_status: "not_imported",
      member_count: null, expected_count: null,
    };
    expect(() => parseOverlapResult(absent, query)).toThrow();
    absent.status = "unavailable";
    absent.common_members = null;
    absent.union_members = null;
    absent.jaccard_ratio = null;
    absent.filtered_total = null;
    absent.items = [];
    expect(parseOverlapResult(absent, query).status).toBe("unavailable");
    const zero = example();
    zero.common_members = 0;
    zero.union_members = 6;
    zero.jaccard_ratio = "0";
    zero.filtered_total = 0;
    zero.items = [];
    expect(parseOverlapResult(zero, query).common_members).toBe(0);
  });
});

describe("Point-in-time membership date alignment", () => {
  it("uses explicit date override, then analysis end, never today's date", () => {
    expect(overlapAsOfDate(null, "2026-10-20")).toBe("2026-10-20");
    expect(overlapAsOfDate("2025-03-17", "2026-10-20")).toBe("2025-03-17");
    // The calendar's Clear date action sends "". It must not re-inherit the parent.
    expect(overlapAsOfDate("", "2026-10-20")).toBeNull();
    expect(overlapAsOfDate("", "2026-11-05")).toBeNull();
    expect(overlapAsOfDate(null, "2026-11-05")).toBe("2026-11-05");
    expect(overlapAsOfDate(null, "")).toBeNull();
    expect(overlapAsOfDate(null, "2026-02-30")).toBeNull();
    // An invalid explicit override must not silently fall back to another date.
    expect(overlapAsOfDate("2026-02-30", "2026-10-20")).toBeNull();
    expect(overlapAsOfDate("2026-11-09", "")).toBe("2026-11-09");
  });

  it("does not dispatch after Clear date; explicit reset restores the parent's date", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ...example(), limit: 10 }) });
    vi.stubGlobal("fetch", fetcher);
    const selection = (overrideDate: string | null) =>
      overlapQueryForSelection(
        true, "SP500", "NDX", overlapAsOfDate(overrideDate, query.asOf), "", 0,
      );
    const initial = selection(null);
    expect(initial).toEqual({ ...query, limit: 10 });
    if (initial) await fetchMembershipOverlap(initial);
    expect(fetcher).toHaveBeenCalledTimes(1);

    // The date picker returns "", and there is no request to dispatch.
    const cleared = selection("");
    expect(cleared).toBeNull();
    if (cleared) await fetchMembershipOverlap(cleared);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(selection("2026-02-30")).toBeNull();
    expect(overlapQueryForSelection(false, "SP500", "NDX", query.asOf, "", 0)).toBeNull();
    expect(overlapQueryForSelection(true, null, "NDX", query.asOf, "", 0)).toBeNull();

    // The separate "Use analysis end date" action restores null/inheritance.
    const restored = selection(null);
    expect(restored).toEqual({ ...query, limit: 10 });
    if (restored) await fetchMembershipOverlap(restored);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("uses localized guidance rather than claiming missing source dates are available", () => {
    expect(translate("zh-CN", "Select a valid constituent snapshot date to compare."))
      .toBe("请选择有效的成分股快照日期进行比较。");
    expect(() => validateOverlapQuery({ ...query, asOf: "" })).toThrow();
    expect(() => validateOverlapQuery({ ...query, asOf: "2026-02-30" })).toThrow();
  });
});

describe("Index Intelligence overlap entry and localized evidence", () => {
  it("requires explicit membership index selection when a price index is unsupported", () => {
    expect(membershipIndexForMarket("NASDAQCOM")).toBeNull();
    expect(membershipIndexForMarket("VIX")).toBeNull();
    expect(membershipIndexForMarket("NDX")).toBe("NDX");
    expect(membershipIndexForMarket("RUT")).toBe("RUT");
    const supported = renderToStaticMarkup(
      <ConstituentOverlapPanel primaryIndex="SP500" analysisEnd="2026-10-20" />,
    );
    expect(supported).toContain("Dated constituent overlap");
    expect(supported).toContain("Explore constituent overlap");
    expect(supported).not.toContain("Download index");
    const unsupported = renderToStaticMarkup(
      <ConstituentOverlapPanel primaryIndex="NASDAQCOM" analysisEnd="2026-10-20" />,
    );
    expect(unsupported).toContain("NASDAQCOM");
    expect(unsupported).toContain("No sourced constituent catalog for this analysis index.");
    expect(unsupported).not.toContain("Download index");
    expect(translate("zh-CN", "No sourced constituent catalog for this analysis index. Choose constituent indexes explicitly; overlap does not represent the selected price index."))
      .not.toContain("No sourced constituent catalog");
  });

  it("renders the imported snapshot date and the distinct underlying source-as-of date", () => {
    const olderEvidence = source("NDX");
    olderEvidence.source_as_of = "2026-10-01";
    const markup = renderToStaticMarkup(<OverlapSourceDates source={olderEvidence} />);
    expect(markup).toContain("Source snapshot date");
    expect(markup).toContain("2026-10-20");
    expect(markup).toContain("Underlying source as-of date");
    expect(markup).toContain("2026-10-01");
    expect(translate("zh-CN", "Underlying source as-of date"))
      .not.toBe("Underlying source as-of date");
  });

  it("labels ETF proxies and older snapshots explicitly in both languages", () => {
    const labels = [
      "Both source snapshots verified for the exact selected date",
      "Earlier snapshots — membership on the requested date is not confirmed",
      "ETF holdings proxy — indicative constituents, not official",
      "Source snapshot unavailable — overlap is unknown, not zero",
    ];
    for (const label of labels) {
      expect(translate("zh-CN", label)).not.toBe(label);
    }
    expect(overlapStatusDescription("proxy_only")).toContain("not official");
    expect(overlapStatusDescription("unavailable")).toContain("not zero");
  });
});
