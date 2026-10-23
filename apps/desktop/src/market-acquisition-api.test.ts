import { describe, expect, it } from "vitest";
import { parseAcquisitionResult, parseAcquisitionStatus } from "./market-acquisition-api";

describe("Integrated acquisition frontend contracts", () => {
  it("exposes only key presence, not the provider credential", () => {
    const parsed = parseAcquisitionStatus({
      provider: "alpha_vantage",
      key_configured: true,
      requires_explicit_action: true,
      api_key_environment_variable: "FININTEL_ALPHA_VANTAGE_API_KEY",
      full_requires_premium: true,
    });
    expect(parsed.key_configured).toBe(true);
    expect(JSON.stringify(parsed)).not.toContain("SYNTHETIC_TEST_KEY");
    expect(() => parseAcquisitionStatus({ provider: "alpha_vantage", key_configured: "yes" })).toThrow();
  });

  it("accepts validated import counts without exposing provider payload", () => {
    const parsed = parseAcquisitionResult({
      status: "stored", symbol: "AAPL", provider: "alpha_vantage", frequency: "1d",
      price_adjustment: "raw_unadjusted", first_session: "2026-09-01",
      last_session: "2026-10-21", retrieved_at: "2026-10-22T20:00:00Z",
      observations: 37, inserted: 36, updated: 0, unchanged: 1, stale: 0,
      import_id: 2, not_live_quotes: true,
    });
    expect(parsed.inserted).toBe(36);
    expect(parsed.not_live_quotes).toBe(true);
    expect(() => parseAcquisitionResult({ ...parsed, inserted: -1 })).toThrow();
    expect(() => parseAcquisitionResult({ ...parsed, symbol: "../../etc" })).toThrow();
  });
});
