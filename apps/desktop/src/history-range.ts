import type { DailyBar } from "./market";

export type HistoryPeriod = "recent" | "1y" | "5y" | "10y" | "custom";
export type ChartInterval = "Daily" | "Weekly" | "Monthly" | "Quarterly";

export interface ChartBar extends DailyBar {
  start_session: string;
  sessions: number;
}

/** Calendar years rather than 252-record approximations; uses the local date. */
export function yearsAgo(years: 1 | 5 | 10, today = new Date()): string {
  const date = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  // setFullYear handles leap days; range is based on calendar date, not record count.
  date.setFullYear(date.getFullYear() - years);
  const pad = (value: number) => String(value).padStart(2, "0");
  return [date.getFullYear(), pad(date.getMonth() + 1), pad(date.getDate())].join("-");
}

export function chartInterval(period: HistoryPeriod, observations: number): ChartInterval {
  if (period === "recent") return "Daily";
  if (period === "1y") return "Weekly";
  if (period === "5y") return "Monthly";
  if (period === "10y") return "Quarterly";
  if (observations <= 120) return "Daily";
  if (observations <= 350) return "Weekly";
  if (observations <= 1400) return "Monthly";
  return "Quarterly";
}

function bucket(day: string, interval: ChartInterval): string {
  if (interval === "Daily") return day;
  if (interval === "Monthly") return day.slice(0, 7);
  if (interval === "Quarterly") {
    return day.slice(0, 4) + "-Q" + (Math.floor((Number(day.slice(5, 7)) - 1) / 3) + 1);
  }
  const value = new Date(day + "T00:00:00Z");
  const weekday = (value.getUTCDay() + 6) % 7;
  value.setUTCDate(value.getUTCDate() - weekday);
  return value.toISOString().slice(0, 10);
}

/**
 * Aggregate true OHLCV intervals for display: first open, highest high,
 * lowest low, last close, sum of volume. Stored daily records stay unchanged.
 */
export function chartBars(bars: DailyBar[], interval: ChartInterval): ChartBar[] {
  const result: ChartBar[] = [];
  let previousKey = "";
  for (const bar of bars) {
    const key = bucket(bar.session_date, interval);
    if (key !== previousKey) {
      result.push({ ...bar, start_session: bar.session_date, sessions: 1 });
      previousKey = key;
    } else {
      const previous = result[result.length - 1];
      result[result.length - 1] = {
        ...previous,
        session_date: bar.session_date,
        high: String(Math.max(Number(previous.high), Number(bar.high))),
        low: String(Math.min(Number(previous.low), Number(bar.low))),
        close: bar.close,
        volume: previous.volume + bar.volume,
        sessions: previous.sessions + 1,
      };
    }
  }
  return result;
}
