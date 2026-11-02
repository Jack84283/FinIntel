import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  exportQualityCsv, fetchQualityCorrections, fetchQualityOverview, runQualityScan,
  submitFindingReview, transitionQualityCorrection,
  type QualityFinding,
} from "./quality-api";

const host = vi.hoisted(() => ({ native: false, invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => host.native,
  invoke: host.invoke,
}));

const finding: QualityFinding = {
  id: 1, code: "SP500", instrument_kind: "index", provider: "fred",
  session_date: "2026-10-20", finding_type: "price_anomaly", rule: "price_jump_candidate",
  severity: "warning", confidence: "low", review_state: "confirmed",
  evidence: { left_close: "5000" }, fingerprint: "synthetic-hash",
  resolution_note: null, superseded: false, superseded_at: null,
  updated_at: "2026-10-22", created_at: "2026-10-20",
};

describe("Issue 29 quality safety and offline workflows", () => {
  beforeEach(() => {
    host.native = false;
    host.invoke.mockReset();
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    host.native = false;
  });

  it("never calls HTTP fetch from packaged Tauri for reads or protected writes", async () => {
    host.native = true;
    host.invoke.mockResolvedValue({ total: 0, items: [] });
    await fetchQualityCorrections("SP500");
    await fetchQualityOverview("SP500");
    await submitFindingReview(3, "confirmed", "Reviewed offline");
    await transitionQualityCorrection("12345678-1234-1234-1234-123456789012", "reverse", "Reversed");
    expect(fetch).not.toHaveBeenCalled();
    expect(host.invoke).toHaveBeenCalledWith("quality_read", {
      route: "corrections", query: { code: "SP500", limit: 50, offset: 0 }, id: undefined,
    });
    expect(host.invoke).toHaveBeenCalledWith("quality_write", {
      route: "review", id: "3", action: undefined,
      body: { state: "confirmed", note: "Reviewed offline" },
    });
    expect(host.invoke).toHaveBeenCalledWith("quality_write", {
      route: "transition", id: "12345678-1234-1234-1234-123456789012",
      action: "reverse", body: { note: "Reversed" },
    });
  });

  it("does not treat a protected HTTP 403 as success in browser-only mode", async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: false, status: 403,
      json: async () => ({ detail: "Desktop process authorization required" }),
    } as Response);
    await expect(submitFindingReview(3, "confirmed", "Review"))
      .rejects.toThrow("Desktop process authorization required");
    expect(fetch).toHaveBeenCalledOnce();
  });

  it("continues to show an actionable offline failure instead of fabricating findings", async () => {
    vi.mocked(fetch).mockRejectedValue(new Error("Local backend offline"));
    await expect(fetchQualityOverview("SP500")).rejects.toThrow("Local backend offline");
    await expect(runQualityScan(1000)).rejects.toThrow("Local backend offline");
  });

  it("neutralizes formula injection after tabs, line breaks and leading spaces", () => {
    for (const payload of ["\t=2+3", " \t@SUM(1)", "\r\n+cmd", " -1"]) {
      const csv = exportQualityCsv([{ ...finding, code: payload }]);
      expect(csv).toContain('"\'');
      expect(csv).not.toContain('"'+payload+'"');
    }
  });

  it("preserves source labels in exported CSV but never includes private finding notes", () => {
    const csv = exportQualityCsv([{
      ...finding, resolution_note: "private-reviewed-user-notes",
      evidence: { close: "100", api_key: "never-export-this", nested: { secret: "hidden" } },
    }]);
    expect(csv).toContain("SP500");
    expect(csv).toContain("price_anomaly");
    expect(csv).not.toContain("private-reviewed-user-notes");
    expect(csv).not.toContain("never-export-this");
    expect(csv).not.toContain("hidden");
  });
});
