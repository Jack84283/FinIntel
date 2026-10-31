import { describe, expect, it } from "vitest";
import { parseDailySettings, parseDailyStatus } from "./daily-acquisition-api";

const idleJob = {
  id: null,
  kind: null,
  status: "idle",
  provider: "alpaca_sip",
  secondary_provider: null,
  scheduled_for: null,
  started_at: null,
  finished_at: null,
  current_instrument: null,
  total_instruments: 0,
  completed_instruments: 0,
  failed_instruments: 0,
  incomplete_instruments: 0,
  pending_instruments: 0,
  successful_imports: 0,
  inserted: 0,
  updated: 0,
  unchanged: 0,
  retry_count: 0,
  provider_errors: [],
  index_results: {},
  duration_ms: null,
  items: [],
};

describe("daily acquisition API parsing", () => {
  it("accepts the idle status contract and normalizes optional values", () => {
    const status = parseDailyStatus({
      ...idleJob,
      enabled: false,
      scheduled_execution_time: "20:00",
      timezone: "America/New_York",
      primary_provider: "alpaca_sip",
      secondary_provider: null,
      catch_up_limit: 5,
      last_execution_time: null,
      last_successful_execution: null,
      next_scheduled_execution: "2026-11-03T01:00:00+00:00",
      duration_ms: 2500,
      job_duration_ms: 2500,
      last_updated_trading_session: null,
      job_id: null,
      job: idleJob,
    });
    expect(status.status).toBe("idle");
    expect(status.job.items).toEqual([]);
    expect(status.duration_ms).toBe(2500);
  });

  it("rejects malformed settings instead of trusting backend JSON", () => {
    expect(() => parseDailySettings({
      enabled: false,
      schedule_time: "20:00",
      timezone: "America/New_York",
      primary_provider: "unknown",
      secondary_provider: null,
      overlap_days: 8,
      retry_attempts: 3,
      retry_backoff_seconds: 2,
      catch_up_limit: 5,
      updated_at: null,
    })).toThrow("Invalid daily acquisition settings.");
  });
});
