import { describe, expect, it } from "vitest";
import type { DailyBar } from "./market";
import {
  calculateTechnicalIndicators,
  relativeStrengthIndex,
  simpleMovingAverage,
} from "./technical-indicators";

function prices(values: number[]): DailyBar[] {
  const first = Date.UTC(2026, 0, 1);
  return values.map((close, index) => {
    const date = new Date(first + index * 86_400_000).toISOString().slice(0, 10);
    return {
      session_date: date,
      open: String(close),
      high: String(close),
      low: String(close),
      close: String(close),
      volume: 100,
    };
  });
}

describe("technical indicators from local sessions", () => {
  it("returns null values rather than invented signals without sufficient history", () => {
    const result = calculateTechnicalIndicators(prices(Array(14).fill(100)));
    expect(result.sma20).toBeNull();
    expect(result.sma50).toBeNull();
    expect(result.sma200).toBeNull();
    expect(result.rsi14).toBeNull();
  });

  it("SMA uses exactly the newest N closes and never later prices", () => {
    expect(simpleMovingAverage([1, 2, 3, 4], 2)).toBe(3.5);
    expect(simpleMovingAverage([1, 2], 3)).toBeNull();
    expect(simpleMovingAverage([1, 2, 3], 0)).toBeNull();
    const result = calculateTechnicalIndicators(prices(Array.from({ length: 200 }, (_, i) => i + 1)));
    expect(result.sma20).toBeCloseTo(190.5);
    expect(result.sma50).toBeCloseTo(175.5);
    expect(result.sma200).toBeCloseTo(100.5);
    expect(result.sessions).toBe(200);
    expect(result.close).toBe(200);
  });

  it("Wilder RSI recognizes increasing, decreasing and flat price runs", () => {
    const ascending = Array.from({ length: 16 }, (_, i) => i + 1);
    const descending = Array.from({ length: 16 }, (_, i) => 100 - i);
    expect(relativeStrengthIndex(ascending)).toBe(100);
    expect(relativeStrengthIndex(descending)).toBe(0);
    expect(relativeStrengthIndex(Array(16).fill(42))).toBe(50);
    expect(relativeStrengthIndex(ascending.slice(0, 14))).toBeNull();
  });

  it("rejects reordered prices and does not mutate source bars", () => {
    const bars = prices([100, 110, 105, 120]);
    expect(calculateTechnicalIndicators(bars).sessions).toBe(4);
    expect(bars[0].close).toBe("100");
    expect(() => calculateTechnicalIndicators([...bars].reverse())).toThrow(/chronological/);
  });

  it("marks invalid or zero prices unavailable rather than dividing by zero", () => {
    const result = calculateTechnicalIndicators(prices(Array(50).fill(0)));
    expect(result.close).toBeNull();
    expect(result.rsi14).toBeNull();
    expect(result.sma20).toBeNull();
  });

  it("does not use high, low, open, volume or network services", () => {
    const bars = prices(Array.from({ length: 30 }, (_, i) => 100 + i));
    bars[0] = { ...bars[0], open: "1", high: "100000", low: "1", volume: 0 };
    const result = calculateTechnicalIndicators(bars);
    expect(result.sma20).toBeCloseTo(119.5);
    expect(result.rsi14).toBe(100);
  });
});
