import { useEffect, useRef, useState } from "react";
import { isTauri } from "@tauri-apps/api/core";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { TablePager, LocalDataTable, TABLE_PAGE_SIZE } from "./components/TablePagination";
import { PageHeader } from "./PageHeader";
import { useLocale } from "./localization";
import { useNotify } from "./notifications";
import { fetchIndexAnalytics, type AnalyticsResult, type QualityPolicy } from "./index-intelligence-api";
import { validateModelResearchQuery, type ModelResearchQuery, type ModelResearchResult } from "./model-research-api";
import {
  cancelModelJob, getModelJob, getModelJobResult, listModelJobs,
  queueModelJob, retryModelJob, type ModelJobFilter, type ModelJobSummary,
} from "./model-jobs-api";

const INDEX_CODES = ["SP500", "DJIA", "NASDAQCOM", "NDX"] as const;
const STATUS_FILTERS: readonly ModelJobFilter[] = [
  "all", "queued", "running", "cancel_requested", "completed", "failed", "cancelled",
];
const FAST_WINDOWS = [2, 5, 10, 20, 50, 100];
const SLOW_WINDOWS = [10, 20, 50, 100, 200, 252];
const inputClass = "h-9 w-full min-w-0 rounded-md border border-input bg-background px-3 text-sm text-foreground";
const labelClass = "grid min-w-0 gap-1 text-xs font-medium text-muted-foreground";
const JOB_SOURCE_LIMIT = 2000;

function message(error: unknown): string {
  const detail = error instanceof Error ? error.message : String(error);
  // Errors returned by native commands must not expose local file paths.
  return detail.length <= 180 && !/(?:\/Users\/|\/home\/|\/private\/|[a-z]:\\)/i.test(detail)
    ? detail : "Local model-job operation unavailable.";
}
function dateTime(value: string | null, language: string): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isFinite(parsed.getTime()) ? parsed.toLocaleString(language) : "—";
}

/** Only explicitly selected, local, bounded index analytics may pin a job. */
export function verifiedJobSnapshot(query: ModelResearchQuery, data: AnalyticsResult): string {
  validateModelResearchQuery(query);
  if (data.identity.code !== query.code ||
    data.identity.instrument_kind !== "index" ||
    data.identity.provider !== "fred" ||
    data.identity.adjustment !== "price_index" ||
    data.identity.return_semantics !== "price_index" ||
    data.quality_policy !== query.qualityPolicy ||
    data.observations > JOB_SOURCE_LIMIT ||
    data.observations < query.slowWindow + 1 ||
    !/^[a-f0-9]{64}$/.test(data.source.snapshot_sha256)) {
    throw new Error("Local source is incompatible, too long, or has insufficient observations.");
  }
  return data.source.snapshot_sha256;
}

export function ModelJobsPage() {
  const { t, language } = useLocale();
  const notify = useNotify();
  const managed = typeof window !== "undefined" && isTauri();
  const [code, setCode] = useState<(typeof INDEX_CODES)[number]>("SP500");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [qualityPolicy, setQualityPolicy] = useState<QualityPolicy>("require_complete");
  const [fastWindow, setFastWindow] = useState(5);
  const [slowWindow, setSlowWindow] = useState(20);
  const [queueBusy, setQueueBusy] = useState(false);
  const [queueError, setQueueError] = useState("");
  const [statusFilter, setStatusFilter] = useState<ModelJobFilter>("all");
  const [page, setPage] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [items, setItems] = useState<ModelJobSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState("");
  const [selected, setSelected] = useState<ModelJobSummary | null>(null);
  const [result, setResult] = useState<ModelResearchResult | null>(null);
  const [detailsBusy, setDetailsBusy] = useState(false);
  const [detailsError, setDetailsError] = useState("");
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const detailSerial = useRef(0);

  const modelQuery: ModelResearchQuery = {
    code, start, end, qualityPolicy, fastWindow, slowWindow,
  };
  const validQueue = (() => {
    try { validateModelResearchQuery(modelQuery); return true; }
    catch { return false; }
  })();

  // The local ledger is queried page-by-page; status filtering happens in
  // SQLite *before* LIMIT/OFFSET. Poll only while this page is mounted.
  useEffect(() => {
    if (!managed) { setLoading(false); return; }
    let active = true;
    let request = 0;
    async function load() {
      const current = ++request;
      try {
        const data = await listModelJobs(page + 1, statusFilter);
        if (!active || current !== request) return;
        const last = Math.max(0, Math.ceil(data.total / TABLE_PAGE_SIZE) - 1);
        if (page > last) { setPage(Math.min(last, 1000)); return; }
        setItems(data.items);
        setTotal(data.total);
        setListError("");
      } catch (error) {
        if (active && current === request) setListError(message(error));
      } finally {
        if (active && current === request) setLoading(false);
      }
    }
    void load();
    const timer = window.setInterval(() => { void load(); }, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [managed, page, statusFilter, refresh]);

  async function queue() {
    if (!managed || queueBusy || !validQueue) return;
    setQueueBusy(true);
    setQueueError("");
    const query = { ...modelQuery };
    try {
      // Explicit, read-only local snapshot preview. The backend rechecks the
      // same SHA at enqueue, claim and completion; it never silently re-pins.
      const analytics = await fetchIndexAnalytics({
        code: query.code, comparisonCode: null, start: query.start,
        end: query.end, window: 20, qualityPolicy: query.qualityPolicy,
      });
      const expectedSnapshot = verifiedJobSnapshot(query, analytics);
      await queueModelJob({ ...query, expectedSnapshot });
      notify(t("Model job queued locally."), "success");
      setPage(0);
      setRefresh(value => value + 1);
    } catch (error) {
      setQueueError(message(error));
      notify(t("Model job could not be queued."), "error");
    } finally {
      setQueueBusy(false);
    }
  }

  async function inspect(job: ModelJobSummary) {
    const request = ++detailSerial.current;
    setSelected(job);
    setResult(null);
    setDetailsBusy(true);
    setDetailsError("");
    try {
      if (job.state === "completed") {
        const data = await getModelJobResult(job.id);
        if (request === detailSerial.current) {
          setSelected(data);
          setResult(data.result);
        }
      } else {
        const data = await getModelJob(job.id);
        if (request === detailSerial.current) setSelected(data);
      }
    } catch (error) {
      if (request === detailSerial.current) setDetailsError(message(error));
    } finally {
      if (request === detailSerial.current) setDetailsBusy(false);
    }
  }

  async function act(job: ModelJobSummary, action: "cancel" | "retry") {
    if (!managed || actionBusy) return;
    setActionBusy(job.id);
    setListError("");
    try {
      if (action === "cancel") await cancelModelJob(job.id);
      else await retryModelJob(job.id);
      notify(t(action === "cancel" ? "Model job cancellation requested." : "Model job queued for retry."), "success");
      setRefresh(value => value + 1);
      if (selected?.id === job.id) {
        ++detailSerial.current;
        setSelected(null);
        setResult(null);
      }
    } catch (error) {
      setListError(message(error));
      notify(t("Model job operation failed."), "error");
    } finally {
      setActionBusy(null);
    }
  }

  function canCancel(job: ModelJobSummary): boolean {
    return job.state === "queued" || (job.state === "running" && !job.cancel_requested);
  }
  function canRetry(job: ModelJobSummary): boolean {
    return (job.state === "failed" || job.state === "cancelled") && job.attempts < 3;
  }
  const visibleTotal = Math.min(total, 10010);

  return <section className="min-w-0 space-y-2" aria-labelledby="model-jobs-title">
    <PageHeader id="model-jobs-title" breadcrumb={t("FinIntel / Model Jobs")}
      title={t("Model Jobs")}
      subtitle={t("Run and inspect source-pinned local research jobs. No trades or backtests.")} />
    <Card>
      <CardHeader>
        <CardTitle>{t("Queue built-in model")}</CardTitle>
        <CardDescription>{t("Select a local FRED price index and date window. Job inputs are pinned to a verified source snapshot before execution.")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          <label className={labelClass}>{t("Source index")}
            <select className={inputClass} value={code} disabled={queueBusy}
              onChange={event => setCode(event.target.value as (typeof INDEX_CODES)[number])}>
              {INDEX_CODES.map(index => <option key={index} value={index}>{index}</option>)}
            </select>
          </label>
          <label className={labelClass}>{t("Start date")}
            <input type="date" className={inputClass} value={start} disabled={queueBusy}
              onChange={event => setStart(event.target.value)} />
          </label>
          <label className={labelClass}>{t("End date")}
            <input type="date" className={inputClass} value={end} disabled={queueBusy}
              onChange={event => setEnd(event.target.value)} />
          </label>
          <label className={labelClass}>{t("Data quality policy")}
            <select className={inputClass} value={qualityPolicy} disabled={queueBusy}
              onChange={event => setQualityPolicy(event.target.value as QualityPolicy)}>
              <option value="require_complete">{t("Verified only")}</option>
              <option value="allow_unverified">{t("Allow unverified (labeled)")}</option>
            </select>
          </label>
          <label className={labelClass}>{t("Fast SMA window")}
            <select className={inputClass} value={fastWindow} disabled={queueBusy}
              onChange={event => setFastWindow(Number(event.target.value))}>
              {FAST_WINDOWS.map(value => <option key={value} value={value}>{value} {t("sessions")}</option>)}
            </select>
          </label>
          <label className={labelClass}>{t("Slow SMA window")}
            <select className={inputClass} value={slowWindow} disabled={queueBusy}
              onChange={event => setSlowWindow(Number(event.target.value))}>
              {SLOW_WINDOWS.map(value => <option key={value} value={value}>{value} {t("sessions")}</option>)}
            </select>
          </label>
        </div>
        <p className="text-xs text-muted-foreground">{t("At most 2,000 actual sessions. SMA signals are research-only and generated after the observed close.")}</p>
        {!managed && <p role="status" className="text-sm text-muted-foreground">
          {t("Durable model jobs require the managed FinIntel desktop.")}
        </p>}
        {managed && !validQueue && <p role="status" className="text-sm text-muted-foreground">
          {t("Select valid dates and a fast SMA window shorter than the slow window.")}
        </p>}
        {queueError && <p role="alert" className="text-sm text-destructive">{t("Model job could not be queued.")} {t(queueError)}</p>}
        <Button type="button" disabled={!managed || !validQueue || queueBusy} onClick={() => { void queue(); }}>
          {queueBusy ? t("Pinning source and queuing…") : t("Queue model job")}
        </Button>
      </CardContent>
    </Card>

    <Card>
      <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2">
        <div className="space-y-1">
          <CardTitle>{t("Model job history")}</CardTitle>
          <CardDescription>{t("Persisted local jobs, 10 per page. Status filters cover the entire ledger.")}</CardDescription>
        </div>
        <Button type="button" variant="secondary" size="sm" disabled={!managed}
          onClick={() => setRefresh(value => value + 1)}>{t("Refresh Status")}</Button>
      </CardHeader>
      <CardContent className="space-y-3">
        <label className={labelClass + " max-w-[260px]"}>{t("Filter job status")}
          <select className={inputClass} value={statusFilter} disabled={!managed}
            onChange={event => {
              setStatusFilter(event.target.value as ModelJobFilter);
              setPage(0);
              setLoading(true);
            }}>
            {STATUS_FILTERS.map(status => <option key={status} value={status}>
              {status === "all" ? t("All statuses") : t(status)}
            </option>)}
          </select>
        </label>
        {loading && <p role="status" className="text-xs text-muted-foreground">{t("Loading model jobs…")}</p>}
        {listError && <p role="alert" className="text-sm text-destructive">{t("Model job history unavailable.")} {t(listError)}</p>}
        {managed && !loading && !listError && items.length === 0 &&
          <p role="status" className="py-4 text-sm text-muted-foreground">{t("No matching model jobs.")}</p>}
        {!loading && !listError && items.length > 0 && <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full min-w-[730px] text-left text-xs tabular-nums">
            <thead className="bg-muted text-muted-foreground"><tr>
              <th scope="col" className="p-2">{t("Created")}</th>
              <th scope="col" className="p-2">{t("Model")}</th>
              <th scope="col" className="p-2">{t("Status")}</th>
              <th scope="col" className="p-2">{t("Attempts")}</th>
              <th scope="col" className="p-2">{t("Progress")}</th>
              <th scope="col" className="p-2 text-right">{t("Actions")}</th>
            </tr></thead>
            <tbody>{items.map(job => <tr key={job.id} className="border-t border-border align-top">
              <td className="p-2 whitespace-nowrap">{dateTime(job.created_at, language)}</td>
              <td className="p-2">
                <p className="font-medium">{job.model_id} v{job.model_version}</p>
                <p className="text-[11px] text-muted-foreground">{job.id.slice(0, 8)}…</p>
              </td>
              <td className="p-2"><Badge variant={job.state === "completed" ? "success" : "outline"}>{t(job.status)}</Badge></td>
              <td className="p-2">{job.attempts} / 3</td>
              <td className="p-2">{job.state === "queued" ? "—" : String(job.progress) + "%"}</td>
              <td className="p-2 text-right">
                <div className="flex flex-wrap justify-end gap-1">
                  <Button type="button" size="sm" variant="outline" onClick={() => { void inspect(job); }}>{t("Inspect")}</Button>
                  {canCancel(job) && <Button type="button" size="sm" variant="outline"
                    disabled={Boolean(actionBusy)} onClick={() => { void act(job, "cancel"); }}>{t("Cancel")}</Button>}
                  {canRetry(job) && <Button type="button" size="sm" variant="secondary"
                    disabled={Boolean(actionBusy)} onClick={() => { void act(job, "retry"); }}>{t("Retry")}</Button>}
                </div>
              </td>
            </tr>)}</tbody>
          </table>
        </div>}
        <TablePager page={page} total={visibleTotal} disabled={!managed || loading || Boolean(listError)}
          onPageChange={value => { setPage(Math.min(value, 1000)); setLoading(true); }} />
        {total > visibleTotal && <p className="text-xs text-muted-foreground">
          {t("History is limited to the first 10,010 entries; narrow the status filter.")} ({total.toLocaleString(language)})
        </p>}
      </CardContent>
    </Card>

    {selected && <Card>
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 space-y-1">
          <CardTitle>{t("Model job details")}</CardTitle>
          <CardDescription className="break-all">{selected.id}</CardDescription>
        </div>
        <Button type="button" variant="outline" size="sm" onClick={() => {
          detailSerial.current++;
          setSelected(null); setResult(null); setDetailsError("");
        }}>{t("Close details")}</Button>
      </CardHeader>
      <CardContent className="space-y-3">
        {detailsBusy && <p role="status" className="text-sm text-muted-foreground">{t("Loading model result…")}</p>}
        {detailsError && <p role="alert" className="text-sm text-destructive">{t("Verified model result unavailable.")} {t(detailsError)}</p>}
        <dl className="grid gap-2 text-xs sm:grid-cols-2 xl:grid-cols-3">
          <div><dt className="text-muted-foreground">{t("Status")}</dt><dd className="font-medium">{t(selected.status)}</dd></div>
          <div><dt className="text-muted-foreground">{t("Started")}</dt><dd>{dateTime(selected.started_at, language)}</dd></div>
          <div><dt className="text-muted-foreground">{t("Finished")}</dt><dd>{dateTime(selected.finished_at, language)}</dd></div>
          <div><dt className="text-muted-foreground">{t("Attempts")}</dt><dd>{selected.attempts} / 3</dd></div>
          <div><dt className="text-muted-foreground">{t("Model error code")}</dt><dd>{selected.error_code ? t(selected.error_code) : "—"}</dd></div>
          <div><dt className="text-muted-foreground">{t("Progress")}</dt><dd>{selected.progress}%</dd></div>
          <div className="sm:col-span-2 xl:col-span-3">
            <dt className="text-muted-foreground">{t("Source snapshot SHA-256")}</dt>
            <dd className="break-all">{selected.source_snapshot_sha256 ?? "—"}</dd>
          </div>
          <div className="sm:col-span-2 xl:col-span-3">
            <dt className="text-muted-foreground">{t("Output SHA-256")}</dt>
            <dd className="break-all">{selected.output_sha256 ?? "—"}</dd>
          </div>
        </dl>
        {result && <>
          <div className="grid gap-2 text-xs sm:grid-cols-2 xl:grid-cols-4">
            <div><p className="text-muted-foreground">{t("Source index")}</p>
              <strong>{result.lineage.source_identity.code} · {result.lineage.source_identity.provider}</strong></div>
            <div><p className="text-muted-foreground">{t("Selected analysis window")}</p>
              <strong>{result.lineage.requested_start} – {result.lineage.requested_end}</strong></div>
            <div><p className="text-muted-foreground">{t("Fast SMA window")} / {t("Slow SMA window")}</p>
              <strong>{result.lineage.parameters.fast_window} / {result.lineage.parameters.slow_window}</strong></div>
            <div><p className="text-muted-foreground">{t("Observed sessions")}</p>
              <strong>{result.observations.toLocaleString(language)}</strong></div>
            <div><p className="text-muted-foreground">{t("Quality status")}</p>
              <strong>{t(result.lineage.quality_status)}</strong></div>
            <div><p className="text-muted-foreground">{t("Series semantics")}</p>
              <strong>{result.lineage.source_identity.return_semantics}</strong></div>
            <div><p className="text-muted-foreground">{t("Calculation version")}</p>
              <strong>{result.lineage.implementation_revision}</strong></div>
            <div><p className="text-muted-foreground">{t("Latest research signal")}</p>
              <strong>{t(result.points.at(-1)?.signal_after_close ?? "Warm-up")}</strong></div>
          </div>
          {result.quality_warning && <p role="status" className="text-sm text-muted-foreground">
            {t("Unverified model input: research only; not valid for backtesting.")}
          </p>}
          <LocalDataTable rows={[...result.points].reverse()} label={t("Model output sessions")}
            searchable={point => [point.session_date, point.close, point.signal_after_close ?? ""].join(" ")}>
            {visible => <div className="overflow-x-auto rounded-md border border-border">
              <table className="w-full min-w-[520px] text-right text-xs tabular-nums">
                <thead className="bg-muted text-muted-foreground"><tr>
                  <th scope="col" className="p-2 text-left">{t("Session date")}</th>
                  <th scope="col" className="p-2">{t("Close")}</th>
                  <th scope="col" className="p-2">{t("Fast SMA")}</th>
                  <th scope="col" className="p-2">{t("Slow SMA")}</th>
                  <th scope="col" className="p-2">{t("Signal after close")}</th>
                </tr></thead>
                <tbody>{visible.map(point => <tr key={point.session_date} className="border-t border-border">
                  <td className="p-2 text-left">{point.session_date}</td>
                  <td className="p-2">{point.close}</td>
                  <td className="p-2">{point.fast_sma ?? "—"}</td>
                  <td className="p-2">{point.slow_sma ?? "—"}</td>
                  <td className="p-2">{point.signal_after_close ? t(point.signal_after_close) : "—"}</td>
                </tr>)}</tbody>
              </table>
            </div>}
          </LocalDataTable>
        </>}
        <p className="text-xs text-muted-foreground">
          {t("Only stored observations and post-close research signals are shown. No trading or validated backtesting is performed.")}
        </p>
      </CardContent>
    </Card>}
  </section>;
}
