import { describe, expect, it } from "vitest";
import { chartBars, chartInterval, yearsAgo } from "./history-range";
import type { DailyBar } from "./market";

const values: DailyBar[] = [
  { session_date: "2026-01-02", open: "100", high: "107", low: "99", close: "104", volume: 100 },
  { session_date: "2026-01-05", open: "104", high: "110", low: "102", close: "108", volume: 120 },
  { session_date: "2026-01-06", open: "108", high: "112", low: "106", close: "111", volume: 130 },
  { session_date: "2026-02-02", open: "111", high: "120", low: "110", close: "117", volume: 140 },
];
describe("historical market ranges and chart aggregation", () => {
  it("computes calendar-year cutoffs, including leap days", () => {
    expect(yearsAgo(5, new Date(2026, 9, 22))).toBe("2021-10-22");
    expect(yearsAgo(10, new Date(2026, 9, 22))).toBe("2016-10-22");
    expect(yearsAgo(1, new Date(2024, 1, 29))).toBe("2023-03-01");
  });

  it("uses chart intervals suitable for 5 and 10 years", () => {
    expect(chartInterval("5y", 1250)).toBe("Monthly");
    expect(chartInterval("10y", 2500)).toBe("Quarterly");
    expect(chartInterval("recent", 100)).toBe("Daily");
    expect(chartInterval("custom", 2500)).toBe("Quarterly");
  });

  it("preserves real OHLC and sums volume in calendar buckets", () => {
    const monthly = chartBars(values, "Monthly");
    expect(monthly).toHaveLength(2);
    expect(monthly[0]).toMatchObject({
      start_session: "2026-01-02",
      session_date: "2026-01-06",
      open: "100", high: "112", low: "99", close: "111",
      volume: 350, sessions: 3,
    });
    expect(monthly[1].volume).toBe(140);
    const weekly = chartBars(values, "Weekly");
    expect(weekly).toHaveLength(3);
    expect(weekly[1]).toMatchObject({ open: "104", close: "111", volume: 250, sessions: 2 });
  });

  it("does not modify source daily bars during chart aggregation", () => {
    const original = JSON.stringify(values);
    chartBars(values, "Quarterly");
    expect(JSON.stringify(values)).toBe(original);
  });
});
