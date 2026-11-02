import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import {
  exportQualityCsv, exportQualityJson, redactedEvidence, fetchQualityFindings,
  fetchQualityOverview, type QualityFinding, type QualityOverview,
} from "./quality-api";

vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => false,
  invoke: vi.fn(),
}));

const example: QualityFinding = {
  id: 12, instrument_kind: "index", code: "SP500", provider: "fred",
  session_date: "2026-10-28", finding_type: "price_anomaly", severity: "warning",
  confidence: "low", rule: "price_jump_candidate",
  fingerprint: "hash", review_state: "candidate", resolution_note: null,
  superseded: false, superseded_at: null, updated_at: "now", created_at: "before",
  evidence: {
    left_close: "6000", right_close: "6080", relative_difference: "0.0133",
    api_key: "top-secret", authorization: "Bearer secret",
    source_url: "https://vendor.example.com/api?token=hidden",
    notes: { account: "private" }, previous_session: "2026-10-27",
  },
};

const overview: QualityOverview = {
  code: "SP500", total: 2, counts: {
    severity: { warning: 2 }, review_state: { candidate: 2 },
    finding_type: { price_anomaly: 2 }, instrument_kind: { index: 2 },
  },
  scan: {
    status: "partial", stale: false, complete: false, partial: true,
    findings_examined: 200, findings_stored: 2, total_known_findings: 2,
    cursor_phase: "index", cursor_next: 100,
    validation_reference_date: "2026-11-01",
    validation_reference_date_saved: "2026-11-01",
    source_revision_changed: false, source_catalog_identity_changed: false,
  },
};

describe("Data Quality redacted local reports", () => {
  it("includes permitted scalar evidence while excluding secrets and raw payloads", () => {
    const result = redactedEvidence(example.evidence);
    expect(result).toEqual({
      left_close: "6000", right_close: "6080",
      relative_difference: "0.0133", previous_session: "2026-10-27",
    });
    const encoded = exportQualityJson("SP500", overview, [example]);
    expect(encoded).toContain("finintel-data-quality-redacted-v1");
    expect(encoded).toContain("6000");
    expect(encoded).not.toContain("top-secret");
    expect(encoded).not.toContain("vendor.example.com");
    expect(encoded).not.toContain("Bearer");
    expect(encoded).not.toContain("private");
    expect(encoded).not.toContain("hash");
  });

  it("escapes CSV formulas, quotes and newlines without exposing arbitrary evidence", () => {
    const record: QualityFinding = { ...example, code: "=HYPERLINK(\"evil\")", provider: "fred" };
    const csv = exportQualityCsv([record]);
    expect(csv).toContain("'=HYPERLINK");
    expect(csv).toContain('""evil""');
    expect(csv).toContain("left_close");
    expect(csv).not.toContain("top-secret");
    expect(csv.split("\n").length).toBe(2);
  });
});

describe("Data Quality local API routing", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => ({
      ok: true,
      json: async () => url.includes("/overview")
        ? overview : { items: [example], total: 1, offset: 0, limit: 20 },
    })));
  });
  afterEach(() => vi.unstubAllGlobals());

  it("defaults to SP500 scope and uses bounded backend pagination", async () => {
    const summary = await fetchQualityOverview("SP500");
    const page = await fetchQualityFindings({
      code: "SP500", finding_type: "price_anomaly", limit: 20, offset: 20,
    });
    expect(summary.code).toBe("SP500");
    expect(page.items).toHaveLength(1);
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/quality/overview?code=SP500", { cache: "no-store" },
    );
    expect((fetch as ReturnType<typeof vi.fn>).mock.calls[1][0]).toContain("offset=20");
  });
});
