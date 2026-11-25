import { webcrypto } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { invoke, isTauri } from "@tauri-apps/api/core";
import fixture from "../../../contracts/test-fixtures/market-comparison.json";
import { fetchMarketComparison, parseComparisonResult, type ComparisonRequest } from "./market-comparison-api";
import { parseComparisonPreferences, presetWindow } from "./market-comparison-preferences";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(), isTauri: vi.fn(() => true) }));
const request = fixture.request as ComparisonRequest;
beforeEach(() => vi.stubGlobal("crypto", webcrypto));
afterEach(() => vi.unstubAllGlobals());

describe("comparison transport", () => {
  it("accepts the backend fixture through the fixed IPC command", async () => {
    vi.mocked(invoke).mockResolvedValue(fixture);
    const result = await fetchMarketComparison(request.sources, request.start, request.end);
    expect(invoke).toHaveBeenCalledWith("market_comparison", { request });
    expect(result.sources[1]).toMatchObject({ latest_close: "5250", common_end_close: "5200" });
    expect(result.points[1].values[0]).toBeNull();
  });

  it.each([
    (result: typeof fixture) => { result.request.sources.reverse(); },
    (result: typeof fixture) => { result.points[1].values.fill(null); },
    (result: typeof fixture) => { result.points[1].session_date = result.baseline_date; },
    (result: typeof fixture) => { result.points[0].values[0]!.close = "0"; },
    (result: typeof fixture) => { result.sources[0].common_end_close = "3"; },
    (result: typeof fixture) => { result.sources[0].identity.provider = "other"; },
    (result: typeof fixture) => { result.sources[0].snapshot_sha256 = "a".repeat(64); },
    (result: typeof fixture) => { result.points[0].values[0]!.normalized_100 = "99"; },
  ])("fails closed on a modified response", async mutate => {
    const bad = structuredClone(fixture);
    mutate(bad);
    await expect(parseComparisonResult(bad, request)).rejects.toThrow("invalid_response");
  });

  it("uses the fixed read-only browser route without requesting another source", async () => {
    vi.mocked(isTauri).mockReturnValueOnce(false);
    const localFetch = vi.fn(async () => Response.json(fixture));
    vi.stubGlobal("fetch", localFetch);
    await fetchMarketComparison(request.sources, request.start, request.end);
    expect(localFetch).toHaveBeenCalledExactlyOnceWith("/api/v1/market-data/comparison",
      expect.objectContaining({ method: "POST", body: JSON.stringify(request) }));
  });

  it("validates saved selections and retains only valid preferences", () => {
    const settings = parseComparisonPreferences({
      sources: [...request.sources, request.sources[0], { kind: "index", code: "VIX", provider: "fred" }],
      hidden: ["tiingo:AAPL", "fred:VIX"], period: "custom", start: "bad", end: "bad",
      results: fixture.points,
    });
    expect(settings.sources).toEqual(request.sources);
    expect(settings.hidden).toEqual(["tiingo:AAPL"]);
    expect(settings.period).toBe("1y");
    expect(settings).not.toHaveProperty("results");
    expect(presetWindow("1y", new Date("2024-02-29T12:00:00Z"))).toEqual({ start: "2023-02-28", end: "2024-02-29" });
  });
});
