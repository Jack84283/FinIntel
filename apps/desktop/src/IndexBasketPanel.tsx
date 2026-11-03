import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useLocale } from "./localization";
import { MarketDatePicker } from "./components/MarketDatePicker";
import { INDEX_CODES, type IndexCatalogRecord, type IndexCode } from "./index-history-api";
import {
  fetchIndexBasket, type BasketResult, type BasketMetric, type BasketQuery,
} from "./index-basket-api";
import { ANALYTICS_WINDOWS, type AnalyticsWindow, type QualityPolicy } from "./index-intelligence-api";

const PRICE_CODES: readonly IndexCode[] = INDEX_CODES.filter(code => code !== "VIX");
const KEY = "finintel.index-basket.v1";
type BasketMode = "1Y" | "5Y" | "ALL" | "custom";
type Selection = {
  codes: IndexCode[]; window: AnalyticsWindow; qualityPolicy: QualityPolicy;
  start: string; end: string; mode: BasketMode;
};
const initial: Selection = {
  codes: ["SP500", "DJIA", "NASDAQCOM"], window: 20,
  qualityPolicy: "require_complete", start: "", end: "", mode: "1Y",
};
const metricNames: readonly BasketMetric[] = [
  "normalized_100", "drawdown", "rolling_volatility_annualized",
  "relative_strength", "rolling_correlation",
];
const palette = ["#5c9ded", "#e7a74f", "#61b69d", "#c38deb"];
const labelClass = "grid gap-1 text-xs font-medium text-muted-foreground";
const selectClass = "h-9 min-w-0 rounded-md border border-input bg-background px-2 text-sm text-foreground focus-visible:ring-2 focus-visible:ring-ring";
function saved(): Selection {
  try {
    if (typeof localStorage === "undefined") return initial;
    const value = JSON.parse(localStorage.getItem(KEY) || "null") as Partial<Selection> | null;
    if (!value || typeof value !== "object") return initial;
    const codes = Array.isArray(value.codes) ? value.codes.filter(code => PRICE_CODES.includes(code)) : [];
    return {
      codes: [...new Set(codes)].slice(0, 4).length >= 2 ? [...new Set(codes)].slice(0, 4) : initial.codes,
      window: ANALYTICS_WINDOWS.includes(value.window as AnalyticsWindow) ? value.window as AnalyticsWindow : 20,
      qualityPolicy: value.qualityPolicy === "allow_unverified" ? "allow_unverified" : "require_complete",
      start: typeof value.start === "string" ? value.start : "",
      end: typeof value.end === "string" ? value.end : "",
      mode: value.mode === "custom" || value.mode === "ALL" || value.mode === "5Y" ? value.mode : "1Y",
    };
  } catch { return initial; }
}
function yearsBefore(end: string, years: number): string {
  const [year, month, day] = end.split("-").map(Number);
  return new Date(Date.UTC(year - years, month - 1, day)).toISOString().slice(0, 10);
}
/** Catalog bounds are only an envelope; exact common dates come from SQLite. */
export function basketCoverage(catalog: IndexCatalogRecord[], codes: readonly string[]): { first: string; last: string } | null {
  if (codes.length < 2 || new Set(codes).size !== codes.length) return null;
  const matched = codes.map(code => catalog.find(item => item.code === code));
  if (matched.some(item => !item?.first_session || !item.last_session)) return null;
  const first = matched.reduce((a, item) => item!.first_session! > a ? item!.first_session! : a, "0000-01-01");
  const last = matched.reduce((a, item) => item!.last_session! < a ? item!.last_session! : a, "9999-12-31");
  return first <= last ? { first, last } : null;
}
export function basketMetricCell(result: BasketResult, metric: BasketMetric, date: string, code: string): string | null {
  const row = result.points.find(item => item.session_date === date);
  return row?.values[code]?.[metric] ?? null;
}
export function basketMetricLines(result: BasketResult, metric: BasketMetric) {
  return result.codes.map((code, i) => ({
    code, color: palette[i % palette.length],
    points: result.points.flatMap(item => {
      const raw = item.values[code]?.[metric];
      if (raw === null || raw === undefined) return [];
      const value = Number(raw);
      return Number.isFinite(value) ? [{ date: item.session_date, value }] : [];
    }),
  }));
}
/** Preserve original first/last observations; render sampling never changes calculations. */
export function decimateBasketLines(lines: ReturnType<typeof basketMetricLines>, max = 450) {
  return lines.map(line => {
    if (line.points.length <= max) return line;
    const indices = new Set(Array.from({ length: max }, (_, index) =>
      Math.round(index * (line.points.length - 1) / (max - 1))));
    return { ...line, points: line.points.filter((_, index) => indices.has(index)) };
  });
}
export function BasketChart({ result, metric }: { result: BasketResult; metric: BasketMetric }) {
  const { t, language } = useLocale();
  const series = basketMetricLines(result, metric);
  const nonEmpty = series.filter(line => line.points.length > 0);
  if (!nonEmpty.length) return <p role="status" className="py-8 text-center text-sm text-muted-foreground">{t("Not enough shared observations for this metric.")}</p>;
  const plot = decimateBasketLines(nonEmpty);
  const values = plot.flatMap(line => line.points.map(point => point.value));
  const floor = Math.min(...values), ceiling = Math.max(...values);
  const pad = Math.max((ceiling - floor) * 0.08, Math.abs(ceiling) * 0.002, 0.0001);
  const min = floor - pad, max = ceiling + pad;
  const start = Date.parse(result.first_common_session + "T00:00:00Z");
  const last = Date.parse(result.last_common_session + "T00:00:00Z");
  const x = (date: string) => 64 + (Date.parse(date + "T00:00:00Z") - start) /
    Math.max(last - start, 1) * 752;
  const y = (value: number) => 232 - (value - min) / (max - min) * 210;
  const format = (value: number) => new Intl.NumberFormat(language, { maximumFractionDigits: 3 }).format(value);
  return <div className="space-y-2">
    <div className="flex flex-wrap gap-3 text-xs">
      {plot.map(line => <span key={line.code} className="flex items-center gap-2">
        <span aria-hidden="true" style={{ backgroundColor: line.color }} className="h-0.5 w-5" />{line.code}
      </span>)}
    </div>
    <div className="overflow-x-auto rounded-md border border-border bg-muted/30 p-2">
      <svg role="img" aria-label={t("Multi-index historical comparison chart") + " · " + t(metric)}
        viewBox="0 0 840 267" className="h-auto min-w-[520px] w-full">
        {Array.from({ length: 5 }, (_, i) => min + (max - min) * i / 4).map(value => (
          <g key={value}>
            <line x1="64" x2="816" y1={y(value)} y2={y(value)} stroke="currentColor" opacity="0.12" strokeDasharray="3 4" />
            <text x="58" y={y(value) + 4} textAnchor="end" fontSize="11" fill="currentColor">{format(value)}</text>
          </g>
        ))}
        {plot.map(line => (
          <path key={line.code} fill="none" stroke={line.color} strokeWidth="2"
            d={line.points.map((item, i) => (i ? "L" : "M") +
              x(item.date).toFixed(2) + "," + y(item.value).toFixed(2)).join(" ")}>
            <title>{line.code + " · " + t(metric)}</title>
          </path>
        ))}
        <text x="64" y="259" fontSize="11" fill="currentColor">{result.first_common_session}</text>
        <text x="816" y="259" fontSize="11" textAnchor="end" fill="currentColor">{result.last_common_session}</text>
      </svg>
    </div>
    <p className="text-xs text-muted-foreground">{t("Charts show only real common dates. Render sampling does not change reported figures.")}</p>
  </div>;
}
export function IndexBasketPanel({ catalog, catalogError }: {
  catalog: IndexCatalogRecord[]; catalogError: string;
}) {
  const { t, language } = useLocale();
  const [expanded, setExpanded] = useState(false);
  const [selection, setSelection] = useState<Selection>(saved);
  const [result, setResult] = useState<BasketResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [metric, setMetric] = useState<BasketMetric>("normalized_100");
  const generation = useRef(0);
  const { codes, start, end, window, qualityPolicy, mode } = selection;
  const patch = (value: Partial<Selection>) => setSelection(prev => ({ ...prev, ...value }));
  const bounds = basketCoverage(catalog, codes);
  useEffect(() => {
    try { localStorage.setItem(KEY, JSON.stringify(selection)); } catch { /* device local only */ }
  }, [selection]);
  useEffect(() => {
    if (!expanded || mode === "custom" || !bounds) return;
    const from = mode === "ALL" ? bounds.first : yearsBefore(bounds.last, mode === "5Y" ? 5 : 1);
    const nextStart = from < bounds.first ? bounds.first : from;
    if (nextStart !== start || bounds.last !== end) patch({ start: nextStart, end: bounds.last });
  }, [expanded, bounds?.first, bounds?.last, mode, start, end]);
  useEffect(() => {
    const id = ++generation.current;
    setResult(null); setError("");
    if (!expanded || !bounds || !start || !end) { setBusy(false); return; }
    if (start > end) { setBusy(false); setError(t("Start date cannot be after end date.")); return; }
    const query: BasketQuery = { codes, start, end, window, qualityPolicy };
    setBusy(true);
    fetchIndexBasket(query).then(data => {
      if (generation.current === id) setResult(data);
    }).catch(reason => {
      if (generation.current === id) setError(reason instanceof Error ? reason.message : String(reason));
    }).finally(() => {
      if (generation.current === id) setBusy(false);
    });
    return () => { generation.current++; };
  }, [expanded, codes.join(","), start, end, window, qualityPolicy, revision, bounds?.first, bounds?.last]);
  const shown = result?.points.slice(-20).reverse() ?? [];
  const format = (value: string | null) => value === null ? "—" :
    new Intl.NumberFormat(language, { maximumFractionDigits: 5 }).format(Number(value));
  return <Card>
    <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
      <div><CardTitle>{t("Multi-index comparison")}</CardTitle>
        <CardDescription>{t("Compare 2–4 price indices on exactly the same observed sessions.")}</CardDescription></div>
      <Button type="button" size="sm" variant="outline" aria-expanded={expanded}
        onClick={() => setExpanded(value => !value)}>{expanded ? t("Close comparison") : t("Open multi-index comparison")}</Button>
    </CardHeader>
    {expanded && <CardContent className="space-y-4">
      <fieldset className="space-y-2">
        <legend className="text-xs font-medium text-muted-foreground">{t("Index selection (first selected is baseline)")}</legend>
        <div className="flex flex-wrap gap-3">
          {PRICE_CODES.map(code => <label key={code} className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={codes.includes(code)}
              onChange={event => {
                const next = event.target.checked ? [...codes, code] : codes.filter(item => item !== code);
                if (next.length >= 2 && next.length <= 4) { patch({ codes: next }); setMetric("normalized_100"); }
              }} />{code}
          </label>)}
        </div>
      </fieldset>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <label className={labelClass}>{t("Rolling window")}
          <select className={selectClass} aria-label={t("Multi-index rolling window")} value={window}
            onChange={event => patch({ window: Number(event.target.value) as AnalyticsWindow })}>
            {ANALYTICS_WINDOWS.map(value => <option key={value} value={value}>{value} {t("sessions")}</option>)}
          </select>
        </label>
        <label className={labelClass}>{t("Data quality policy")}
          <select className={selectClass} aria-label={t("Multi-index quality policy")} value={qualityPolicy}
            onChange={event => patch({ qualityPolicy: event.target.value as QualityPolicy })}>
            <option value="require_complete">{t("Verified only")}</option>
            <option value="allow_unverified">{t("Allow unverified (labeled)")}</option>
          </select>
        </label>
      </div>
      <div className="flex flex-wrap gap-2">
        {(["1Y", "5Y", "ALL"] as const).map(value => <Button key={value} type="button" size="sm"
          aria-pressed={mode === value} variant={mode === value ? "secondary" : "ghost"}
          onClick={() => patch({ mode: value })}>{value === "ALL" ? t("All stored history") : value}</Button>)}
        <Button type="button" size="sm" variant="outline" disabled={!bounds}
          onClick={() => bounds && patch({ end: bounds.last, start: mode === "custom" ? bounds.first : start, mode: mode === "custom" ? "ALL" : mode })}>
          {t("Last available")}
        </Button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 xl:max-w-[760px]">
        <MarketDatePicker label={t("Start date")} value={start} onChange={value => patch({ start: value, mode: "custom" })} />
        <MarketDatePicker label={t("End date")} value={end} onChange={value => patch({ end: value, mode: "custom" })} />
      </div>
      {!bounds && !catalogError && <p role="status" className="text-sm text-muted-foreground">
        {t("Selected indices have no overlapping stored coverage. Choose another index or import history manually.")}
      </p>}
      {catalogError && <p role="alert" className="text-sm text-destructive">{catalogError}</p>}
      {busy && <p role="status" className="text-sm text-muted-foreground">{t("Calculating common-session comparison…")}</p>}
      {error && <div className="flex flex-wrap items-center gap-2">
        <p role="alert" className="text-sm text-destructive">{error}</p>
        <Button size="sm" variant="outline" type="button" onClick={() => setRevision(v => v + 1)}>{t("Retry")}</Button>
      </div>}
      {!busy && result && !error && <>
        <div className="flex flex-wrap gap-2">
          <Badge variant={result.unverified_data ? "outline" : "success"}>
            {result.unverified_data ? t("Unverified local data") : t("Quality verified")}
          </Badge>
          <Badge variant="outline">{t("Common observed sessions")}: {result.common_sessions.toLocaleString(language)}</Badge>
          <Badge variant="outline">{t("Baseline")}: {result.baseline_code}</Badge>
        </div>
        <p className="text-xs text-muted-foreground">
          {t("No forward fill. Calculations use the intersection, including rolling warm-up on shared sessions.")}
        </p>
        <div className="flex flex-wrap gap-1" role="group" aria-label={t("Multi-index chart metric")}>
          {metricNames.map(name => <Button key={name} size="sm" type="button" aria-pressed={metric === name}
            variant={metric === name ? "secondary" : "ghost"} onClick={() => setMetric(name)}>{t(name)}</Button>)}
        </div>
        <BasketChart result={result} metric={metric} />
        <div className="max-h-[360px] overflow-auto rounded-md border border-border">
          <table className="w-full border-collapse text-xs tabular-nums">
            <caption className="px-2 py-2 text-left text-muted-foreground">{t("Latest 20 common-session rows, matching chart calculations")}</caption>
            <thead className="sticky top-0 bg-muted">
              <tr><th className="p-2 text-left" scope="col">{t("Session date")}</th>
                {codes.map(code => <th key={code} className="p-2 text-right" scope="col">{code} · {t(metric)}</th>)}</tr>
            </thead>
            <tbody>{shown.map(item => <tr key={item.session_date} className="border-t border-border">
              <th scope="row" className="p-2 text-left font-normal">{item.session_date}</th>
              {codes.map(code => <td key={code} className="p-2 text-right">{format(basketMetricCell(result, metric, item.session_date, code))}</td>)}
            </tr>)}</tbody>
          </table>
        </div>
        <div className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
          <p>{t("Calculation version")}: {result.calculation_version}</p>
          <p>{t("Quality status")}: {result.quality.status}</p>
          <p>{t("Common coverage")}: {result.first_common_session} – {result.last_common_session}</p>
          <p>{t("Missing-data policy")}: {result.alignment}</p>
          {result.series.map(item => <div key={item.identity.code} className="min-w-0 rounded-md border border-border p-2">
            <p className="font-semibold">{item.identity.code} · {item.identity.provider} · {item.identity.return_semantics}</p>
            <p>{t("Source-only observations excluded")}: {item.excluded_nonshared_sessions}</p>
            <p className="break-all">{t("Source snapshot SHA-256")}: {item.source.snapshot_sha256}</p>
          </div>)}
        </div>
      </>}
    </CardContent>}
  </Card>;
}
