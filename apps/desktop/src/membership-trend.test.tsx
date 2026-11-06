import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  fetchMembershipTrend, parseTrendResult, validateTrendQuery,
  type TrendPoint, type TrendQuery, type TrendResult,
} from "./membership-trend-api";
import { ConstituentTrendPanel, SnapshotCountBar, TrendEvidenceControls, trendEvidenceDescription } from "./ConstituentTrendPanel";
import { translate } from "./localization";

const native = vi.hoisted(() => ({ enabled: false, invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => native.enabled,
  invoke: native.invoke,
}));
const query: TrendQuery = {
  code: "SP500", fromAsOf: "2026-10-01", toAsOf: "2026-10-20", limit: 25, offset: 0,
};
function samplePoint(date: string, total: number): TrendPoint {
  return {
    snapshot_as_of: date, source: "verified-source", source_as_of: date,
    source_kind: "verified_public", validation_status: "complete",
    availability_status: "available", confidence: "medium", expected_count: total,
    member_count: total, evidence_status: "verified_snapshot",
    previous_snapshot_as_of: null, previous_evidence_status: null,
    added_members: null, removed_members: null, change_status: "first_observed",
  };
}
function example(): TrendResult {
  const first = samplePoint("2026-10-01", 3);
  const second: TrendPoint = {
    ...samplePoint("2026-10-06", 3), previous_snapshot_as_of: "2026-10-01",
    previous_evidence_status: "verified_snapshot",
    added_members: 1, removed_members: 1, change_status: "verified_pair",
  };
  const proxy: TrendPoint = {
    ...samplePoint("2026-10-10", 2), source_kind: "fund_derived_proxy",
    availability_status: "proxy_only", evidence_status: "proxy_only",
    previous_snapshot_as_of: "2026-10-06", previous_evidence_status: "verified_snapshot",
    added_members: 0, removed_members: 1, change_status: "indicative_pair",
  };
  return {
    code: "SP500", name: "S&P 500", from_as_of: query.fromAsOf,
    to_as_of: query.toAsOf, limit: query.limit, offset: query.offset,
    total_snapshots: 3, snapshots: [first, second, proxy],
    snapshot_only: true, no_interpolation: true, no_lookahead: true, read_only: true,
  };
}
beforeEach(() => { native.enabled = false; native.invoke.mockReset(); });
afterEach(() => vi.unstubAllGlobals());

describe("Historical sourced constituent snapshot trend", () => {
  it("rejects unsupported indexes, impossible ranges and unbounded pages before I/O", () => {
    expect(() => validateTrendQuery(query)).not.toThrow();
    expect(() => validateTrendQuery({ ...query, code: "VIX" as TrendQuery["code"] })).toThrow();
    expect(() => validateTrendQuery({ ...query, fromAsOf: "2026-02-30" })).toThrow();
    expect(() => validateTrendQuery({ ...query, fromAsOf: "2026-10-21" })).toThrow();
    expect(() => validateTrendQuery({ ...query, limit: 101 })).toThrow();
    expect(() => validateTrendQuery({ ...query, offset: -1 })).toThrow();
  });

  it("accepts only real chronologically sorted snapshots and consistent differences", () => {
    expect(parseTrendResult(example(), query).snapshots).toHaveLength(3);
    const future = example();
    future.snapshots[2].snapshot_as_of = "2026-10-21";
    expect(() => parseTrendResult(future, query)).toThrow();
    const fabricated = example();
    fabricated.snapshots[2].snapshot_as_of = "2026-10-08";
    expect(() => parseTrendResult(fabricated, query)).toThrow();
    const count = example();
    count.snapshots[1].added_members = 2;
    expect(() => parseTrendResult(count, query)).toThrow();
    const missing = example();
    missing.total_snapshots = 4;
    expect(() => parseTrendResult(missing, query)).toThrow();
    const noInterpolation = example();
    noInterpolation.no_interpolation = false as true;
    expect(() => parseTrendResult(noInterpolation, query)).toThrow();
  });

  it("rejects forged verification, misleading proxy labels and first-snapshot deltas", () => {
    const forged = example();
    forged.snapshots[2].evidence_status = "verified_snapshot";
    expect(() => parseTrendResult(forged, query)).toThrow();
    const falselyVerified = example();
    falselyVerified.snapshots[2].change_status = "verified_pair";
    expect(() => parseTrendResult(falselyVerified, query)).toThrow();
    const sourceMismatch = example();
    sourceMismatch.snapshots[1].source_as_of = "2026-10-03";
    expect(() => parseTrendResult(sourceMismatch, query)).toThrow();
    sourceMismatch.snapshots[1].evidence_status = "source_date_mismatch";
    sourceMismatch.snapshots[2].previous_evidence_status = "source_date_mismatch";
    sourceMismatch.snapshots[1].change_status = "indicative_pair";
    expect(parseTrendResult(sourceMismatch, query).snapshots[1].change_status).toBe("indicative_pair");

    const first = example();
    first.snapshots[0].added_members = 0;
    expect(() => parseTrendResult(first, query)).toThrow();
  });

  it("supports correct page-boundary deltas without reinterpreting the first visible row", () => {
    const full = example();
    const page = {
      ...full, limit: 1, offset: 1, snapshots: [full.snapshots[1]],
    };
    const selection = { ...query, limit: 1, offset: 1 };
    expect(parseTrendResult(page, selection).snapshots[0].previous_snapshot_as_of).toBe("2026-10-01");
    const invalid = structuredClone(page);
    invalid.snapshots[0].previous_snapshot_as_of = null;
    expect(() => parseTrendResult(invalid, selection)).toThrow();
  });

  it("keeps missing source history unknown and never invents zero snapshots", () => {
    const empty = { ...example(), total_snapshots: 0, snapshots: [] };
    expect(parseTrendResult(empty, query).total_snapshots).toBe(0);
    const wrong = { ...empty, snapshots: [example().snapshots[0]] };
    expect(() => parseTrendResult(wrong, query)).toThrow();
  });

  it("uses only same-origin read-only browser API", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => example() });
    vi.stubGlobal("fetch", fetcher);
    expect((await fetchMembershipTrend(query)).snapshots).toHaveLength(3);
    const [url, options] = fetcher.mock.calls[0];
    expect(url).toContain("/api/v1/market-data/indexes/snapshot-trend?");
    expect(url).toContain("code=SP500");
    expect(url).toContain("from_as_of=2026-10-01");
    expect(options).toEqual({ cache: "no-store" });
  });

  it("uses managed native IPC instead of external provider fetches", async () => {
    native.enabled = true;
    native.invoke.mockResolvedValue(example());
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    expect((await fetchMembershipTrend(query)).snapshots).toHaveLength(3);
    expect(native.invoke).toHaveBeenCalledWith("index_membership_trend", {
      code: "SP500", fromAsOf: query.fromAsOf, toAsOf: query.toAsOf,
      limit: 25, offset: 0,
    });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("preserves offline errors rather than claiming empty source history", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("Backend offline")));
    await expect(fetchMembershipTrend(query)).rejects.toThrow("Backend offline");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false, status: 503, json: async () => ({ detail: "Local SQLite unavailable" }),
    }));
    await expect(fetchMembershipTrend(query)).rejects.toThrow("Local SQLite unavailable");
  });
});

describe("Bilingual snapshot-only Index Intelligence UI", () => {
  it("shows a dated snapshot card without implicit downloads", () => {
    const supported = renderToStaticMarkup(
      <ConstituentTrendPanel primaryIndex="SP500" analysisStart="2026-10-01" analysisEnd="2026-10-20" />,
    );
    expect(supported).toContain("Historical constituent snapshot trends");
    expect(supported).toContain("Explore snapshot trends");
    expect(supported).not.toContain("Download index");
    const unsupported = renderToStaticMarkup(
      <ConstituentTrendPanel primaryIndex="VIX" analysisStart="2026-10-01" analysisEnd="2026-10-20" />,
    );
    expect(unsupported).toContain("Choose a supported constituent index explicitly");
  });

  it("renders only an isolated observed-snapshot bar and localized evidence", () => {
    const markup = renderToStaticMarkup(
      <SnapshotCountBar point={example().snapshots[0]} maximum={5} />,
    );
    expect(markup).toContain("Stored snapshot constituent count");
    expect(markup).toContain("width:60%");
    expect(trendEvidenceDescription("proxy_only")).toContain("not official");
    for (const key of [
      "Historical constituent snapshot trends",
      "Snapshot-only; no interpolation",
      "Underlying source date does not match snapshot date",
      "Observed snapshot changes do not establish when membership changed between imports, and indicative sources must not be used as verified backtests.",
    ]) expect(translate("zh-CN", key)).not.toBe(key);
  });
});


describe("Zero-result evidence filter recovery", () => {
  const renderControls = (filter: string, total: number) => renderToStaticMarkup(
    <TrendEvidenceControls evidenceFilter={filter} total={total} onChange={() => undefined} />,
  );

  it("keeps the selector and All option visible when a selected status has zero matches", () => {
    const filtered = renderControls("proxy_only", 0);
    expect(filtered).toContain('value="proxy_only"');
    expect(filtered).toContain("All evidence statuses");
    expect(filtered).toContain("No snapshots match the selected evidence filter.");
    expect(filtered).not.toContain("No imported membership snapshots in this period");

    // Selecting All restores the unfiltered request (already verified by API tests below).
    const restored = renderControls("", 3);
    expect(restored).toContain("All evidence statuses");
    expect(restored).not.toContain("No snapshots match the selected evidence filter.");
    expect(restored).not.toContain("No imported membership snapshots in this period");
  });

  it("distinguishes no stored snapshots from zero evidence matches in both languages", () => {
    const missing = renderControls("", 0);
    expect(missing).toContain("No imported membership snapshots in this period");
    expect(missing).not.toContain("No snapshots match the selected evidence filter.");
    const label = "No snapshots match the selected evidence filter.";
    expect(translate("zh-CN", label)).not.toBe(label);
  });
});

describe("Global source-evidence filtering", () => {
  it("validates later-page hits and preserves their actual unfiltered predecessors", () => {
    const selection: TrendQuery = { ...query, limit: 1, evidenceStatus: "proxy_only" };
    const proxy = example().snapshots[2];
    const filtered: TrendResult = {
      ...example(), limit: 1, total_snapshots: 1, snapshots: [proxy],
    };
    expect(parseTrendResult(filtered, selection).snapshots[0].previous_snapshot_as_of).toBe("2026-10-06");
    expect(parseTrendResult(filtered, selection).snapshots[0].removed_members).toBe(1);
    const invalidStatus = { ...filtered, snapshots: [example().snapshots[1]] };
    expect(() => parseTrendResult(invalidStatus, selection)).toThrow();
    expect(() => validateTrendQuery({ ...selection, evidenceStatus: "fictional" as TrendPoint["evidence_status"] }))
      .toThrow();

    // Filtered page two is evaluated relative to its real preceding snapshot.
    const second: TrendQuery = { ...query, limit: 1, offset: 1, evidenceStatus: "verified_snapshot" };
    const selected: TrendResult = {
      ...example(), limit: 1, offset: 1, total_snapshots: 2,
      snapshots: [example().snapshots[1]],
    };
    expect(parseTrendResult(selected, second).snapshots[0].previous_snapshot_as_of).toBe("2026-10-01");
  });

  it("passes evidence status through browser queries and managed native IPC", async () => {
    const selection: TrendQuery = { ...query, limit: 1, evidenceStatus: "proxy_only" };
    const filtered: TrendResult = { ...example(), limit: 1, total_snapshots: 1, snapshots: [example().snapshots[2]] };
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => filtered });
    vi.stubGlobal("fetch", fetcher);
    expect((await fetchMembershipTrend(selection)).total_snapshots).toBe(1);
    expect(fetcher.mock.calls[0][0]).toContain("evidence_status=proxy_only");

    native.enabled = true;
    native.invoke.mockResolvedValue(filtered);
    expect((await fetchMembershipTrend(selection)).snapshots).toHaveLength(1);
    expect(native.invoke).toHaveBeenCalledWith("index_membership_trend", {
      code: "SP500", fromAsOf: query.fromAsOf, toAsOf: query.toAsOf,
      limit: 1, offset: 0, evidenceStatus: "proxy_only",
    });
  });
});
