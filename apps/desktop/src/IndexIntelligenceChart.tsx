import type { AnalyticsResult, AnalyticsPoint, ComparisonPoint } from "./index-intelligence-api";
import { useLocale } from "./localization";

export type ChartMetric = "normalized" | "drawdown" | "volatility" | "relative" | "correlation";
export type SeriesPoint = { date: string; value: number };
export type ChartLine = { key: string; label: string; values: SeriesPoint[]; color: string };
const COLORS = ["#5c9ded", "#e6a24d"] as const;

function numeric(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}
/** Charts and table always use the same verbatim backend-provided metric cells. */
export function metricCell(
  _result: AnalyticsResult,
  metric: ChartMetric,
  row: AnalyticsPoint | ComparisonPoint,
  side: "primary" | "benchmark" = "primary",
): string | null {
  if (metric === "normalized") {
    return "left_normalized_100" in row
      ? (side === "primary" ? row.left_normalized_100 : row.right_normalized_100)
      : side === "primary" ? row.normalized_100 : null;
  }
  if (metric === "drawdown") return "drawdown" in row ? row.drawdown : null;
  if (metric === "volatility") return "rolling_volatility_annualized" in row ? row.rolling_volatility_annualized : null;
  if (metric === "relative") return "relative_strength" in row ? row.relative_strength : null;
  return "rolling_correlation" in row ? row.rolling_correlation : null;
}
export function metricRows(result: AnalyticsResult, metric: ChartMetric): (AnalyticsPoint | ComparisonPoint)[] {
  return (metric === "normalized" && result.comparison) || metric === "relative" || metric === "correlation"
    ? result.comparison?.points ?? [] : result.points;
}
export function chartLines(result: AnalyticsResult, metric: ChartMetric): ChartLine[] {
  const rows = metricRows(result, metric);
  const line = (key: string, label: string, side: "primary" | "benchmark", color: string): ChartLine => ({
    key, label, color,
    values: rows.flatMap((row) => {
      const value = numeric(metricCell(result, metric, row, side));
      return value === null ? [] : [{ date: row.session_date, value }];
    }),
  });
  if (metric === "normalized" && result.comparison) {
    return [
      line(result.identity.code, result.identity.code, "primary", COLORS[0]),
      line(result.comparison.right.code, result.comparison.right.code, "benchmark", COLORS[1]),
    ];
  }
  return [line(result.identity.code, result.identity.code, "primary", COLORS[0])];
}
export function shownChartLines(lines: ChartLine[], max = 450): ChartLine[] {
  // Decimate only for pixels/performance; never synthesize or average an observation.
  return lines.map(line => {
    if (line.values.length <= max) return line;
    const indices = new Set(Array.from({ length: max }, (_, i) =>
      Math.round(i * (line.values.length - 1) / (max - 1))));
    return { ...line, values: line.values.filter((_, i) => indices.has(i)) };
  });
}
export function IndexMetricChart({ result, metric }: { result: AnalyticsResult; metric: ChartMetric }) {
  const { t, language } = useLocale();
  const lines = chartLines(result, metric);
  const valid = lines.some(line => line.values.length);
  if (!valid) return <p role="status" className="py-9 text-center text-sm text-muted-foreground">{t("No calculated values in the selected range.")}</p>;
  const available = shownChartLines(lines);
  const all = available.flatMap(line => line.values);
  const min = Math.min(...all.map(point => point.value));
  const max = Math.max(...all.map(point => point.value));
  const spread = Math.max(max - min, Math.abs(max) * 0.002, 0.0001);
  const floor = min - spread * 0.08;
  const ceil = max + spread * 0.08;
  const width = 840, left = 73, right = 20, top = 18, bottom = 245;
  const earliest = all.reduce((x, p) => p.date < x ? p.date : x, all[0].date);
  const latest = all.reduce((x, p) => p.date > x ? p.date : x, all[0].date);
  const start = Date.parse(earliest + "T00:00:00Z"), end = Date.parse(latest + "T00:00:00Z");
  const x = (date: string) => left + (Date.parse(date + "T00:00:00Z") - start) /
    Math.max(end - start, 1) * (width - left - right);
  const y = (v: number) => bottom - (v - floor) / (ceil - floor) * (bottom - top);
  const fmt = (v: number) => new Intl.NumberFormat(language, { maximumFractionDigits: 3 }).format(v);
  const ticks = Array.from({ length: 5 }, (_, i) => floor + i * (ceil - floor) / 4);
  return (
    <div className="min-w-0 space-y-2">
      <div className="flex flex-wrap items-center gap-4 text-xs text-muted-foreground">
        {available.map(line => (
          <span key={line.key} className="flex items-center gap-2">
            <span aria-hidden="true" className="h-0.5 w-6" style={{ backgroundColor: line.color }} />{line.label}
          </span>
        ))}
      </div>
      <div className="overflow-x-auto rounded-md border border-border bg-muted/30 p-2">
        <svg viewBox="0 0 840 285" role="img"
          aria-label={t("Historical index analytics chart") + " · " + t(metric) + " · " + earliest + " – " + latest}
          className="h-auto min-w-[540px] w-full">
          {ticks.map(tick => (
            <g key={tick}>
              <line x1={left} x2={width - right} y1={y(tick)} y2={y(tick)} stroke="currentColor" opacity="0.12" strokeDasharray="3 5" />
              <text x={left - 9} y={y(tick) + 4} textAnchor="end" fontSize="11" fill="currentColor" opacity="0.8">{fmt(tick)}</text>
            </g>
          ))}
          {available.map(line => (
            <path key={line.key} fill="none" stroke={line.color} strokeWidth="2.25" strokeLinejoin="round"
              strokeLinecap="round" d={line.values.map((point, i) =>
                (i ? "L" : "M") + x(point.date).toFixed(2) + "," + y(point.value).toFixed(2)).join(" ")}>
              <title>{line.label + " · " + t(metric) + " · " + line.values.length + " " + t("plotted observations")}</title>
            </path>
          ))}
          {Array.from(new Set([earliest, latest])).map(date => (
            <text key={date} x={x(date)} y="272" fontSize="11" textAnchor={date === earliest ? "start" : "end"}
              fill="currentColor" opacity="0.75">{date}</text>
          ))}
        </svg>
      </div>
      <p className="text-xs text-muted-foreground">{t("Only actual locally observed dates are plotted. Large charts sample existing points without altering calculations.")}</p>
    </div>
  );
}
