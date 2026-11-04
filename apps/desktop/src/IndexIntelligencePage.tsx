import { useEffect, useRef, useState } from "react";
import { BarChart3, CalendarClock, Database, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "./PageHeader";
import { MarketDatePicker } from "./components/MarketDatePicker";
import { useLocale } from "./localization";
import { INDEX_CODES, listIndexHistory, type IndexCode, type IndexCatalogRecord } from "./index-history-api";
import {
  fetchIndexAnalytics, type AnalyticsQuery, type AnalyticsResult, type AnalyticsWindow,
  type QualityPolicy, ANALYTICS_WINDOWS,
} from "./index-intelligence-api";
import { IndexMetricChart, metricCell, metricRows, type ChartMetric } from "./IndexIntelligenceChart";
import { IndexBasketPanel } from "./IndexBasketPanel";
import { ConstituentOverlapPanel } from "./ConstituentOverlapPanel";

const STORAGE_KEY = "finintel.index-intelligence.v1";
type Preset = "1Y" | "5Y" | "ALL" | "custom";
type Config = {
  code: IndexCode;
  comparisonCode: IndexCode | null;
  window: AnalyticsWindow;
  qualityPolicy: QualityPolicy;
  preset: Preset;
  start: string;
  end: string;
};
const initial: Config = {
  code: "SP500", comparisonCode: null, window: 20, qualityPolicy: "require_complete",
  preset: "1Y", start: "", end: "",
};
function isCode(value: unknown): value is IndexCode {
  return typeof value === "string" && INDEX_CODES.includes(value as IndexCode);
}
function readSaved(): Config {
  try {
    if (typeof localStorage === "undefined") return initial;
    const value: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
    if (!value || typeof value !== "object") return initial;
    const item = value as Record<string, unknown>;
    const code = isCode(item.code) ? item.code : "SP500";
    const comparisonCode = isCode(item.comparisonCode) && item.comparisonCode !== code &&
      item.comparisonCode !== "VIX" && code !== "VIX" ? item.comparisonCode : null;
    return {
      code, comparisonCode,
      window: ANALYTICS_WINDOWS.includes(item.window as AnalyticsWindow) ? item.window as AnalyticsWindow : 20,
      qualityPolicy: item.qualityPolicy === "allow_unverified" ? "allow_unverified" : "require_complete",
      preset: item.preset === "5Y" || item.preset === "ALL" || item.preset === "custom" ? item.preset : "1Y",
      start: typeof item.start === "string" && /^\d{4}-\d{2}-\d{2}$/.test(item.start) ? item.start : "",
      end: typeof item.end === "string" && /^\d{4}-\d{2}-\d{2}$/.test(item.end) ? item.end : "",
    };
  } catch {
    return initial;
  }
}
function shiftYears(value: string, amount: number): string {
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(Date.UTC(year - amount, month - 1, day));
  return date.toISOString().slice(0, 10);
}
function readableError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
export function latestCommonDate(catalog: IndexCatalogRecord[], code: IndexCode, benchmark: IndexCode | null): string | null {
  const primary = catalog.find(item => item.code === code);
  const other = benchmark ? catalog.find(item => item.code === benchmark) : null;
  const a = primary?.last_session;
  const b = benchmark ? other?.last_session : null;
  if (!a || (benchmark && !b)) return null;
  const upper = b && b < a ? b : a;
  const lower = benchmark && other?.first_session && primary?.first_session
    ? (other.first_session > primary.first_session ? other.first_session : primary.first_session)
    : primary?.first_session;
  return lower && lower > upper ? null : upper;
}
function presetDates(catalog: IndexCatalogRecord[], code: IndexCode, benchmark: IndexCode | null, preset: Preset) {
  const last = latestCommonDate(catalog, code, benchmark);
  if (!last) return null;
  const a = catalog.find(item => item.code === code)?.first_session;
  const b = benchmark ? catalog.find(item => item.code === benchmark)?.first_session : null;
  const overlapFirst = b && a ? (a > b ? a : b) : a;
  const selected = preset === "5Y" ? shiftYears(last, 5) : preset === "ALL" ? (overlapFirst || "1900-01-01") : shiftYears(last, 1);
  return { start: selected, end: last };
}
const selectClass = "h-9 w-full min-w-0 rounded-md border border-input bg-background px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const labelClass = "grid min-w-0 gap-1 text-xs font-medium text-muted-foreground";

export function IndexIntelligencePage() {
  const { t, language } = useLocale();
  const [config, setConfig] = useState<Config>(readSaved);
  const [catalog, setCatalog] = useState<IndexCatalogRecord[]>([]);
  const [catalogLoaded, setCatalogLoaded] = useState(false);
  const [catalogError, setCatalogError] = useState("");
  const [revision, setRevision] = useState(0);
  const [result, setResult] = useState<AnalyticsResult | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [metric, setMetric] = useState<ChartMetric>("normalized");
  const requestGeneration = useRef(0);
  const { code, comparisonCode, start, end, window, qualityPolicy, preset } = config;
  const patch = (updates: Partial<Config>) => setConfig(current => ({ ...current, ...updates }));

  useEffect(() => {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(config)); } catch { /* device-local storage optional */ }
  }, [config]);
  useEffect(() => {
    let active = true;
    listIndexHistory().then(rows => {
      if (active) { setCatalog(rows); setCatalogError(""); setCatalogLoaded(true); }
    }).catch(reason => {
      if (active) { setCatalogError(readableError(reason)); setCatalogLoaded(true); }
    });
    return () => { active = false; };
  }, [revision]);
  useEffect(() => {
    if (!catalogLoaded || catalogError || preset === "custom") return;
    const dates = presetDates(catalog, code, comparisonCode, preset);
    if (dates && (dates.start !== start || dates.end !== end)) patch(dates);
  }, [catalog, catalogLoaded, catalogError, code, comparisonCode, preset, start, end]);

  useEffect(() => {
    const id = ++requestGeneration.current;
    setResult(null);
    setError("");
    if (!catalogLoaded || catalogError || !start || !end) { setLoading(false); return; }
    if (start > end) { setError(t("Start date cannot be after end date.")); setLoading(false); return; }
    const query: AnalyticsQuery = { code, comparisonCode, start, end, window, qualityPolicy };
    setLoading(true);
    fetchIndexAnalytics(query).then(value => {
      if (id === requestGeneration.current) { setResult(value); setError(""); }
    }).catch(reason => {
      if (id === requestGeneration.current) setError(readableError(reason));
    }).finally(() => {
      if (id === requestGeneration.current) setLoading(false);
    });
    return () => { requestGeneration.current++; };
  }, [catalogLoaded, catalogError, code, comparisonCode, start, end, window, qualityPolicy, revision]);

  function selectPrimary(next: IndexCode) {
    patch({ code: next, comparisonCode: next === comparisonCode || next === "VIX" ? null : comparisonCode });
    setMetric("normalized");
  }
  function selectComparison(next: IndexCode | null) {
    patch({ comparisonCode: next });
    setMetric("normalized");
  }
  function choosePreset(next: Preset) {
    patch({ preset: next });
  }
  function lastAvailable() {
    const dates = presetDates(catalog, code, comparisonCode, preset === "custom" ? "1Y" : preset);
    if (dates) patch({ ...dates, preset: preset === "custom" ? "1Y" : preset });
  }
  const available = catalog.find(item => item.code === code);
  const latest = latestCommonDate(catalog, code, comparisonCode);
  const counts = result?.observations ?? 0;
  const showComparisonMetrics = Boolean(result?.comparison);
  const modes: ChartMetric[] = !result?.metrics_enabled ? ["normalized"] : showComparisonMetrics
    ? ["normalized", "drawdown", "volatility", "relative", "correlation"]
    : ["normalized", "drawdown", "volatility"];
  const rows = result ? metricRows(result, metric) : [];
  const shown = rows.slice(-25).reverse();
  const formatValue = (value: string | null) =>
    value === null ? "—" : new Intl.NumberFormat(language, { maximumFractionDigits: 5 }).format(Number(value));
  return (
    <section aria-labelledby="index-intelligence-title" className="min-w-0 space-y-3">
      <PageHeader id="index-intelligence-title" breadcrumb={t("FinIntel / Index Intelligence")}
        title={t("Index Intelligence")}
        subtitle={t("Compare locally stored index levels using reproducible, source-backed calculations.")}
        actions={
          <Button type="button" variant="outline" size="sm" onClick={() => setRevision(value => value + 1)}>
            <RefreshCw aria-hidden="true" className="size-4" />{t("Reload local data")}
          </Button>
        } />
      <Card>
        <CardHeader className="pb-2">
          <CardTitle>{t("Analysis selection")}</CardTitle>
          <CardDescription>{t("Only stored FRED index levels are used; changing selections never downloads market data.")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <label className={labelClass}>{t("Primary index")}
              <select aria-label={t("Primary index")} className={selectClass} value={code}
                onChange={event => selectPrimary(event.target.value as IndexCode)}>
                {INDEX_CODES.map(index => (
                  <option key={index} value={index}>{index} {index === "VIX" ? "· " + t("Volatility level") : ""}</option>
                ))}
              </select>
            </label>
            <label className={labelClass}>{t("Compare with")}
              <select aria-label={t("Compare with")} className={selectClass} value={comparisonCode ?? ""}
                onChange={event => selectComparison(event.target.value ? event.target.value as IndexCode : null)}>
                <option value="">{t("No comparison")}</option>
                {INDEX_CODES.filter(item => item !== code && item !== "VIX" && code !== "VIX").map(index =>
                  <option key={index} value={index}>{index}</option>)}
              </select>
            </label>
            <label className={labelClass}>{t("Rolling window")}
              <select aria-label={t("Rolling window")} value={window} className={selectClass}
                onChange={event => patch({ window: Number(event.target.value) as AnalyticsWindow })}>
                {ANALYTICS_WINDOWS.map(item => <option key={item} value={item}>{item} {t("sessions")}</option>)}
              </select>
            </label>
            <label className={labelClass}>{t("Data quality policy")}
              <select aria-label={t("Data quality policy")} className={selectClass} value={qualityPolicy}
                onChange={event => patch({ qualityPolicy: event.target.value as QualityPolicy })}>
                <option value="require_complete">{t("Verified only")}</option>
                <option value="allow_unverified">{t("Allow unverified (labeled)")}</option>
              </select>
            </label>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {(["1Y", "5Y", "ALL"] as const).map(value => (
              <Button type="button" size="sm" variant={preset === value ? "secondary" : "ghost"}
                aria-pressed={preset === value} key={value} onClick={() => choosePreset(value)}>
                {value === "ALL" ? t("All stored history") : value}
              </Button>
            ))}
            <Button type="button" size="sm" variant="outline" disabled={!latest} onClick={lastAvailable}>
              <CalendarClock aria-hidden="true" className="size-4" />{t("Last available")}
            </Button>
            {latest && <span className="text-xs text-muted-foreground">{t("Latest shared coverage end")}: {latest}</span>}
          </div>
          <div className="grid gap-3 sm:grid-cols-2 xl:max-w-[760px]">
            <MarketDatePicker label={t("Start date")} value={start}
              onChange={value => patch({ start: value, preset: "custom" })} />
            <MarketDatePicker label={t("End date")} value={end}
              onChange={value => patch({ end: value, preset: "custom" })} />
          </div>
          {available && <p className="text-xs text-muted-foreground">
            <Database aria-hidden="true" className="mr-1 inline size-3" />
            {t("Stored index coverage")}: {available.sessions.toLocaleString(language)} {t("sessions")} ·
            {" "}{available.first_session ?? t("Unavailable")} – {available.last_session ?? t("Unavailable")} · {available.provider}
          </p>}
          {catalogError && <p role="alert" className="text-sm text-destructive">{t("Local index catalog unavailable")}: {catalogError}</p>}
        </CardContent>
      </Card>
      {loading && <p role="status" aria-live="polite" className="rounded-md border border-border px-4 py-5 text-sm text-muted-foreground">{t("Calculating local index analytics…")}</p>}
      {!loading && error && <Card><CardContent className="space-y-3 pt-5">
        <p role="alert" className="text-sm text-destructive">{t("Local analytics unavailable")}: {error}</p>
        {qualityPolicy === "require_complete" && <p className="text-xs text-muted-foreground">{t("Run Data Quality validation first, or explicitly choose labeled unverified data for research.")}</p>}
      </CardContent></Card>}
      {!loading && !error && catalogLoaded && !result && !latest && !catalogError && (
        <p role="status" className="rounded-md border border-border p-4 text-sm text-muted-foreground">
          {t("No local index history for this selection. Use Data Acquisition to import it explicitly.")}
        </p>
      )}
      {!loading && result && !error && <>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={result.unverified_data ? "outline" : "success"}>
            {result.unverified_data ? t("Unverified local data") : t("Quality verified")}
          </Badge>
          {result.observations < 2 && <Badge variant="outline">{t("Insufficient history")}</Badge>}
          {code === "VIX" && <Badge variant="outline">{t("VIX is a volatility level, not an equity return")}</Badge>}
          {result.comparison && <Badge variant="outline">{t("Aligned common sessions")}: {result.comparison.overlap_sessions}</Badge>}
        </div>
        {result.unverified_data && <p role="status" className="text-sm text-muted-foreground">
          {t("Unverified observations are displayed for research only. Candidate findings are not confirmed defects.")}
        </p>}
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
          <Card><CardHeader className="pb-1"><CardDescription>{t("Observations")}</CardDescription>
            <CardTitle className="text-xl tabular-nums">{counts.toLocaleString(language)}</CardTitle></CardHeader></Card>
          <Card><CardHeader className="pb-1"><CardDescription>{t("Last observed session")}</CardDescription>
            <CardTitle className="text-base tabular-nums">{result.last_session ?? "—"}</CardTitle></CardHeader></Card>
          <Card><CardHeader className="pb-1"><CardDescription>{t("Maximum drawdown")}</CardDescription>
            <CardTitle className="text-xl tabular-nums">{result.max_drawdown === null ? "—" : formatValue(String(Number(result.max_drawdown) * 100)) + "%"}</CardTitle></CardHeader></Card>
          <Card><CardHeader className="pb-1"><CardDescription>{t("Longest completed recovery")}</CardDescription>
            <CardTitle className="text-xl tabular-nums">{result.longest_completed_recovery_observed_sessions === null
              ? "—" : result.longest_completed_recovery_observed_sessions.toLocaleString(language)}</CardTitle>
            <p className="text-xs text-muted-foreground">{t("Observed session intervals")}</p>
          </CardHeader></Card>
          <Card><CardHeader className="pb-1"><CardDescription>{t("Open drawdown duration")}</CardDescription>
            <CardTitle className="text-xl tabular-nums">{result.ongoing_drawdown_observed_sessions === null
              ? "—" : result.ongoing_drawdown_observed_sessions.toLocaleString(language)}</CardTitle>
            <p className="text-xs text-muted-foreground">{t("Unrecovered observed sessions")}</p>
          </CardHeader></Card>
        </div>
        {result.metrics_enabled && <p className="text-xs text-muted-foreground">
          {t("Recovery is the first observed close at or above its prior peak. Open drawdowns have no known recovery date; durations exclude non-observed days.")}
        </p>}
        <Card>
          <CardHeader className="pb-1"><CardTitle>{t("Historical comparison")}</CardTitle>
            <CardDescription>{t("Charts use returned calculations, with no synthetic, interpolated or merged observations.")}</CardDescription></CardHeader>
          <CardContent className="space-y-3">
            <div role="group" aria-label={t("Chart metric")} className="flex flex-wrap gap-1">
              {modes.map(mode => <Button type="button" key={mode} size="sm"
                aria-pressed={metric === mode} variant={metric === mode ? "secondary" : "ghost"}
                onClick={() => setMetric(mode)}>{t(mode)}</Button>)}
            </div>
            <IndexMetricChart result={result} metric={metric} />
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-1"><CardTitle>{t("Historical analytics grid")}</CardTitle>
            <CardDescription>{t("Latest 25 available rows for the selected chart metric; values match the plotted source calculations.")}</CardDescription></CardHeader>
          <CardContent>
            <div className="max-h-[430px] overflow-auto rounded-md border border-border">
              <table className="w-full border-collapse text-left text-xs tabular-nums">
                <thead className="sticky top-0 bg-muted text-muted-foreground">
                  <tr><th scope="col" className="p-2">{t("Session date")}</th>
                    <th scope="col" className="p-2 text-right">{t(metric)} · {code}</th>
                    {metric === "normalized" && result.comparison && <th scope="col" className="p-2 text-right">
                      {t("Normalized performance")} · {result.comparison.right.code}</th>}</tr>
                </thead>
                <tbody>
                  {shown.map(row => (
                    <tr key={row.session_date} className="border-t border-border">
                      <th scope="row" className="p-2 font-normal">{row.session_date}</th>
                      <td className="p-2 text-right">{formatValue(metricCell(result, metric, row))}</td>
                      {metric === "normalized" && result.comparison &&
                        <td className="p-2 text-right">{formatValue(metricCell(result, metric, row, "benchmark"))}</td>}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!shown.length && <p className="py-4 text-sm text-muted-foreground">{t("No calculated values in the selected range.")}</p>}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-1"><CardTitle>{t("Coverage and calculation provenance")}</CardTitle>
            <CardDescription>{t("Every result is calculated offline from a fixed local source snapshot.")}</CardDescription></CardHeader>
          <CardContent className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
            <p>{t("Primary index")}: {result.identity.code} · {result.identity.provider}</p>
            <p>{t("Series semantics")}: {result.identity.return_semantics}</p>
            <p>{t("Coverage")}: {result.first_session ?? "—"} – {result.last_session ?? "—"}</p>
            <p>{t("Calculation version")}: {result.calculation_version}</p>
            <p>{t("Rolling window")}: {result.settings.window_sessions} {t("sessions")}</p>
            <p>{t("Quality status")}: {result.quality.status}</p>
            <p className="break-all sm:col-span-2">{t("Source snapshot SHA-256")}: {result.source.snapshot_sha256}</p>
            {result.source.source_url && <p className="break-all sm:col-span-2">{t("Source reference")}: {result.source.source_url}</p>}
            {result.comparison && <>
              <p>{t("Benchmark")}: {result.comparison.right.code} · {result.comparison.right.provider}</p>
              <p>{t("Missing-data policy")}: {result.comparison.missing_data_policy}</p>
              <p className="break-all sm:col-span-2">{t("Benchmark snapshot SHA-256")}: {result.comparison.source.snapshot_sha256}</p>
              <p className="sm:col-span-2">{t("Source-only observations excluded")}: {result.comparison.left_only_sessions} / {result.comparison.right_only_sessions}</p>
            </>}
          </CardContent>
        </Card>
      </>}
      <IndexBasketPanel catalog={catalog} catalogError={catalogError} />
      <ConstituentOverlapPanel primaryIndex={code} analysisEnd={end} />
      <p className="text-xs text-muted-foreground">
        <BarChart3 aria-hidden="true" className="mr-1 inline size-3" />
        {t("Analysis configurations are saved on this device only. No acquisition or corrections are triggered.")}
      </p>
    </section>
  );
}
