/** Deterministic indicators from chronological, locally stored daily close prices.
 * No live data fetches, forecasts, or database changes.
 */
import type { DailyBar } from "./market";

export type TechnicalIndicators = {
  sessionDate: string | null;
  sessions: number;
  close: number | null;
  sma20: number | null;
  sma50: number | null;
  sma200: number | null;
  rsi14: number | null;
};

export function simpleMovingAverage(closes: readonly number[], window: number): number | null {
  if (!Number.isInteger(window) || window <= 0 || closes.length < window) return null;
  const values = closes.slice(-window);
  if (values.some(value => !Number.isFinite(value) || value <= 0)) return null;
  return values.reduce((sum, value) => sum + value, 0) / window;
}

/** Wilder's RSI with a 14-price-change warmup; no future observations. */
export function relativeStrengthIndex(closes: readonly number[], window = 14): number | null {
  if (!Number.isInteger(window) || window <= 0 || closes.length <= window) return null;
  if (closes.some(value => !Number.isFinite(value) || value <= 0)) return null;
  let gains = 0;
  let losses = 0;
  for (let i = 1; i <= window; i += 1) {
    const delta = closes[i] - closes[i - 1];
    gains += Math.max(0, delta);
    losses += Math.max(0, -delta);
  }
  let averageGain = gains / window;
  let averageLoss = losses / window;
  for (let i = window + 1; i < closes.length; i += 1) {
    const delta = closes[i] - closes[i - 1];
    averageGain = (averageGain * (window - 1) + Math.max(delta, 0)) / window;
    averageLoss = (averageLoss * (window - 1) + Math.max(-delta, 0)) / window;
  }
  if (averageGain === 0 && averageLoss === 0) return 50;
  if (averageLoss === 0) return 100;
  if (averageGain === 0) return 0;
  const rs = averageGain / averageLoss;
  return 100 - 100 / (1 + rs);
}

export function calculateTechnicalIndicators(bars: readonly DailyBar[]): TechnicalIndicators {
  if (!bars.length) {
    return { sessionDate: null, sessions: 0, close: null, sma20: null, sma50: null, sma200: null, rsi14: null };
  }
  // API supplies chronological, distinct trading sessions. Reject malformed or reordered data
  // rather than show a seemingly valid indicator over the wrong time axis.
  for (let i = 1; i < bars.length; i += 1) {
    if (bars[i - 1].session_date >= bars[i].session_date) {
      throw new Error("Technical indicators require chronological, unique daily sessions.");
    }
  }
  const closes = bars.map(bar => Number(bar.close));
  const latest = bars[bars.length - 1];
  if (closes.some(value => !Number.isFinite(value) || value <= 0)) {
    return { sessionDate: latest.session_date, sessions: bars.length, close: null, sma20: null, sma50: null, sma200: null, rsi14: null };
  }
  return {
    sessionDate: latest.session_date,
    sessions: closes.length,
    close: closes[closes.length - 1],
    sma20: simpleMovingAverage(closes, 20),
    sma50: simpleMovingAverage(closes, 50),
    sma200: simpleMovingAverage(closes, 200),
    rsi14: relativeStrengthIndex(closes),
  };
}
