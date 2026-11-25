import type { AnalysisOutputField } from "./analysis-jobs-api";

export type Observation = { session_date: string };
export type Translator = (key: string) => string;
export type ResultColumn = { field: AnalysisOutputField; label: string };

const percentageUnits = new Set(["decimal_return", "decimal_return_difference"]);
const decimal = /^(-?)(\d+)(?:\.(\d+))?$/;
const englishLabels: Record<string, string> = {
  var_95: "Historical VaR (95%)", expected_shortfall_95: "Historical Expected Shortfall (95%)",
  sma50: "Short-window SMA", sma200: "Long-window SMA", oscillator_0_100: "Oscillator (0–100)",
  index_base_100: "Index (base 100)", trend_insufficient_history: "Insufficient trend history",
  vol_calibration_insufficient: "Insufficient volatility calibration", trend_above_band: "Trend above the neutrality band",
  trend_below_band: "Trend below the neutrality band", trend_mixed: "Mixed trend features",
  vol_below_p25: "Volatility below P25", vol_above_p75: "Volatility above P75", vol_normal_range: "Volatility within P25–P75",
  quality_unverified: "Quality unverified",
};

const dateFormats = new Map<string, Intl.DateTimeFormat>();
const decimalSeparators = new Map<string, string>();
const utcDay = (day: string) => Date.parse(day + "T00:00:00Z");

export function fieldLabel(key: string, t: Translator): string {
  const translated = t(key);
  if (translated !== key) return translated;
  if (Object.hasOwn(englishLabels, key)) return t(englishLabels[key]);
  return key.replaceAll("_", " ").replace(/\b(?:ema|sma|rsi|utc|var)\b/g,
    token => token === "var" ? "VaR" : token.toUpperCase()).replace(/^./, letter => letter.toUpperCase());
}

export function unitLabel(unit: string, t: Translator) {
  return (percentageUnits.has(unit) || unit === "percent_points") ? t("Percent") : fieldLabel(unit, t);
}

export function observedDate(day: string, language: string) {
  let formatter = dateFormats.get(language);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat(language, { timeZone: "UTC", year: "numeric", month: "2-digit", day: "2-digit" });
    dateFormats.set(language, formatter);
  }
  return formatter.format(utcDay(day));
}

export function cell(row: Observation, field: string): unknown {
  return Object.hasOwn(row, field) ? (row as Record<string, unknown>)[field] : undefined;
}

export function chartNumber(value: unknown, unit: string): number | null {
  // Model evaluators cap canonical decimals at 256 characters. Keep the
  // same plotting bound to avoid overflow in chart domain arithmetic.
  if (typeof value !== "number" && (typeof value !== "string" || value.length > 256 || !decimal.test(value))) return null;
  if (typeof value === "number" && !Number.isSafeInteger(value)) return null;
  const scaled = Number(value) * (percentageUnits.has(unit) ? 100 : 1);
  return Number.isFinite(scaled) ? scaled : null;
}

/** Scale decimals by moving the radix point, preserving all returned digits. */
export function formatCell(value: unknown, field: AnalysisOutputField, language: string, t: Translator): string {
  if (value === null || value === undefined) return t("Unavailable");
  if (field.kind === "date") return observedDate(String(value), language);
  if (field.kind === "enum") return fieldLabel(String(value), t);
  if (field.kind === "reason_codes" && Array.isArray(value)) return value.map(reason => fieldLabel(String(reason), t)).join(" · ") || "—";
  if (typeof value === "boolean") return t(value ? "Yes" : "No");
  if (typeof value === "number") return value.toLocaleString(language);
  if (typeof value !== "string") return t("Unavailable");

  const match = decimal.exec(value);
  if (!match) return value;
  const [, sign, whole, fraction = ""] = match;
  const isPercent = percentageUnits.has(field.units);
  const digits = whole + (isPercent ? fraction.padEnd(2, "0") : fraction);
  const point = whole.length + (isPercent ? 2 : 0);
  const fractionDigits = digits.slice(point);
  let separator = decimalSeparators.get(language);
  if (!separator) {
    separator = new Intl.NumberFormat(language).formatToParts(1.1).find(part => part.type === "decimal")?.value ?? ".";
    decimalSeparators.set(language, separator);
  }
  const integer = BigInt(digits.slice(0, point)).toLocaleString(language);
  const magnitude = sign + integer + (fractionDigits ? separator + fractionDigits : "");
  return magnitude + (isPercent || field.units === "percent_points" ? "%" : "");
}

export function datePosition(date: string, first: string, last: string): number {
  return (utcDay(date) - utcDay(first)) / Math.max(1, utcDay(last) - utcDay(first));
}

export function continuousSegments(rows: readonly Observation[], column: ResultColumn, convert = chartNumber) {
  const segments: { date: string; value: number }[][] = [];
  let segment: { date: string; value: number }[] = [];
  for (const row of rows) {
    const value = convert(cell(row, column.field.name), column.field.units);
    if (value === null) {
      if (segment.length) segments.push(segment);
      segment = [];
    } else segment.push({ date: row.session_date, value });
  }
  if (segment.length) segments.push(segment);
  return segments;
}
