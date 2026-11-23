import { useId, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TablePager, boundedPage } from "./components/TablePagination";
import { getModelJobResult, listModelJobs, type ModelJobPage, type ModelJobSummary, type ModelJobWithResult } from "./model-jobs-api";
import {
  defaultBacktestAssumptions, queueBacktestJob, validBacktestDecimal, type BacktestJobSummary,
} from "./backtest-jobs-api";
import {
  backtestDisclosure, backtestError, backtestFields, fixedAssumptionLabels, observedBacktestWindow,
  pinnedBacktestRequest, sourceEligibility, trainingBoundaries,
} from "./backtest-workflow";
import { createModelJobDetailFence } from "./model-job-detail-fence";
import { useLocale } from "./localization";

const input = "h-9 w-full min-w-0 rounded-md border border-input bg-background px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const label = "grid min-w-0 gap-1 text-sm font-medium";
const historyCap = 10_010;

export type SmaBacktestSelection = Pick<ModelJobSummary, "id" | "request_sha256">;

export function BacktestForm({ initialSource, workspaceGeneration, onQueued, onInspect, onHistory }: {
  initialSource?: SmaBacktestSelection;
  workspaceGeneration?: RefObject<number>;
  onQueued: () => void;
  onInspect: (job: BacktestJobSummary) => void;
  onHistory: () => void;
}) {
  const { t, language } = useLocale();
  const fieldId = useId();
  const sourceControl = useRef<HTMLSelectElement>(null);
  const [page, setPage] = useState(0);
  const [sourceList, setSourceList] = useState<ModelJobPage>();
  const [listError, setListError] = useState("");
  const [listRefresh, setListRefresh] = useState(0);
  const [selection, setSelection] = useState(initialSource);
  const [source, setSource] = useState<ModelJobWithResult>();
  const [sourceError, setSourceError] = useState("");
  const [sourceRefresh, setSourceRefresh] = useState(0);
  const [trainingEnd, setTrainingEnd] = useState("");
  const [testEnd, setTestEnd] = useState("");
  const [assumptions, setAssumptions] = useState({ ...defaultBacktestAssumptions });
  const [queued, setQueued] = useState<BacktestJobSummary>();
  const [queueError, setQueueError] = useState("");
  const [sending, setSending] = useState(false);
  const sourceFence = useRef(createModelJobDetailFence()).current;
  const listGeneration = useRef(0);
  const submission = useRef({ pending: false, generation: 0 });

  const reason = source ? sourceEligibility(source, source.result) : undefined;
  const result = source?.result;
  const lineage = result?.lineage;
  const training = result && !reason ? trainingBoundaries(result) : [];
  const trainingIndex = result?.points.findIndex(point => point.session_date === trainingEnd) ?? -1;
  const firstTest = trainingIndex >= 0 ? result?.points[trainingIndex + 1]?.session_date : undefined;
  const endings = trainingIndex >= 0 ? result?.points.slice(trainingIndex + 2) ?? [] : [];
  const window = result ? observedBacktestWindow(result, trainingEnd, testEnd) : undefined;
  const valid = backtestFields.every(({ key }) => validBacktestDecimal(key, assumptions[key]));

  function invalidateSubmission() {
    submission.current.generation++;
    setQueueError("");
  }

  function selectSource(next?: SmaBacktestSelection) {
    if (submission.current.pending) return;
    sourceFence.invalidate();
    invalidateSubmission();
    setSource(undefined);
    setSourceError("");
    setQueued(undefined);
    setTrainingEnd("");
    setTestEnd("");
    setSelection(next);
  }

  function startNew() {
    sourceFence.invalidate();
    invalidateSubmission();
    setSource(undefined);
    setSourceError("");
    setQueued(undefined);
    setSourceRefresh(value => value + 1);
    sourceControl.current?.focus();
  }

  useLayoutEffect(() => {
    const gate = submission.current;
    return () => { gate.generation++; };
  }, []);

  useLayoutEffect(() => {
    const generation = ++listGeneration.current;
    let active = true;
    const workspace = workspaceGeneration?.current;
    const current = () => active && generation === listGeneration.current && workspace === workspaceGeneration?.current;
    void listModelJobs(page + 1, "completed").then(data => {
      if (!current()) return;
      const last = boundedPage(page, Math.min(data.total, historyCap));
      if (last !== page) setPage(last);
      else {
        setSourceList(data);
        setListError("");
      }
    }, () => {
      if (current()) setListError("Completed SMA jobs could not be loaded.");
    });
    return () => { active = false; };
  }, [page, listRefresh, workspaceGeneration]);

  useLayoutEffect(() => {
    if (!selection) return;
    const { id, request_sha256 } = selection;
    const version = sourceFence.begin(id);
    const workspace = workspaceGeneration?.current;
    const current = () => sourceFence.isCurrent(id, version) && workspace === workspaceGeneration?.current;
    void getModelJobResult(id).then(loaded => {
      if (!current()) return;
      if (loaded.id !== id || loaded.request_sha256 !== request_sha256) {
        setSourceError("The completed SMA source failed integrity verification.");
        return;
      }
      setSource(loaded);
    }, () => {
      if (current()) setSourceError("The completed SMA source is unavailable or failed integrity verification.");
    });
    return () => sourceFence.invalidate();
  }, [selection, sourceRefresh, sourceFence, workspaceGeneration]);

  async function queue() {
    const gate = submission.current;
    if (gate.pending || !source || !result || reason || !window || !valid || queued) return;
    const generation = ++gate.generation;
    const workspace = workspaceGeneration?.current;
    const current = () => generation === gate.generation && workspace === workspaceGeneration?.current;
    gate.pending = true;
    setSending(true);
    setQueueError("");
    try {
      const job = await queueBacktestJob(pinnedBacktestRequest(source, result, window, assumptions));
      if (!current()) return;
      setQueued(job);
      onQueued();
    } catch (error) {
      if (current()) setQueueError(backtestError(error));
    } finally {
      gate.pending = false;
      if (current()) setSending(false);
    }
  }

  return <Card className="min-w-0">
    <CardHeader><CardTitle><h2>{t("New Backtest")}</h2></CardTitle></CardHeader>
    <CardContent className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <label className={label + " flex-1"}>{t("Completed SMA source job")}
          <select ref={sourceControl} className={input} value={selection?.id ?? ""} disabled={sending}
            onChange={event => selectSource(sourceList?.items.find(item => item.id === event.target.value))}>
            <option value="">{t("Select a completed SMA job")}</option>
            {selection && !sourceList?.items.some(item => item.id === selection.id) &&
              <option value={selection.id}>{selection.id}</option>}
            {sourceList?.items.map(item => <option key={item.id} value={item.id}>
              {new Date(item.finished_at ?? item.created_at).toLocaleString(language)} · {item.id}
            </option>)}
          </select>
        </label>
        <Button type="button" variant="outline" size="sm" className="self-end" disabled={sending}
          onClick={() => { listGeneration.current++; setListRefresh(value => value + 1); }}>{t("Refresh source jobs")}</Button>
      </div>
      {sourceList && <div role="group" aria-label={t("Completed SMA source jobs")}>
        <TablePager page={page} total={Math.min(sourceList.total, historyCap)} disabled={sending}
          onPageChange={next => {
            if (next === page || submission.current.pending) return;
            listGeneration.current++;
            selectSource();
            setSourceList(undefined);
            setPage(next);
          }} />
      </div>}
      {!sourceList && !listError && <p role="status">{t("Loading completed SMA jobs…")}</p>}
      {sourceList?.total === 0 && <p role="status">{t("Run a legacy SMA job with Require complete to obtain a backtest source.")}</p>}
      {listError && <p role="alert" className="text-destructive">{t(listError)}</p>}
      {selection && !source && !sourceError && <p role="status">{t("Verifying the completed SMA source…")}</p>}
      {sourceError && <div className="flex flex-wrap items-center gap-2">
        <p role="alert" className="text-destructive">{t(sourceError)}</p>
        <Button type="button" size="sm" variant="outline" onClick={startNew}>{t("Verify source again")}</Button>
      </div>}
      {source && lineage && <section aria-label={t("Source evidence")} className="space-y-2 rounded-md border p-3 text-sm">
        <h3 className="font-medium">{t("Source evidence")}</h3>
        <p role={reason ? "alert" : "status"} className={reason ? "text-destructive" : "font-medium"}>{t(reason ?? "Eligible stored SMA source")}</p>
        <dl className="grid gap-2 sm:grid-cols-2">
          <div><dt className="text-muted-foreground">{t("Source index")}</dt><dd>{lineage.source_identity.code} · FRED</dd></div>
          <div><dt className="text-muted-foreground">{t("Model")}</dt><dd>{t("SMA crossover")} · {source.model_version}</dd></div>
          <div><dt className="text-muted-foreground">{t("Fast SMA window")} / {t("Slow SMA window")}</dt><dd>{lineage.parameters.fast_window} / {lineage.parameters.slow_window}</dd></div>
          <div><dt className="text-muted-foreground">{t("Observed sessions")}</dt><dd>{lineage.first_observed_session} – {lineage.last_observed_session} · {source.result.observations.toLocaleString(language)}</dd></div>
          <div><dt className="text-muted-foreground">{t("Requested window")}</dt><dd>{lineage.requested_start} – {lineage.requested_end}</dd></div>
          <div><dt className="text-muted-foreground">{t("Original quality policy")}</dt><dd>{t(lineage.quality_policy)}</dd></div>
          <div><dt className="text-muted-foreground">{t("Finished")}</dt><dd>{source.finished_at ? new Date(source.finished_at).toLocaleString(language) : "—"}</dd></div>
          <div><dt className="text-muted-foreground">{t("Source job ID")}</dt><dd className="break-all">{source.id}</dd></div>
        </dl>
        <details><summary className="cursor-pointer">{t("Source snapshot SHA-256")}</summary>
          <code className="select-all break-all">{lineage.source_snapshot_sha256}</code>
        </details>
        {!reason && <p className="text-xs text-muted-foreground">{t("Queueing checks the current local snapshot and data quality.")}</p>}
      </section>}
      {result && !reason && <form className="space-y-4" onSubmit={event => { event.preventDefault(); void queue(); }}>
        <fieldset className="grid min-w-0 gap-3 sm:grid-cols-3" disabled={sending || Boolean(queued)}>
          <legend className="mb-2 font-medium">{t("Observed-session window")}</legend>
          <label className={label}>{t("Training end")}
            <select className={input} value={trainingEnd} onChange={event => {
              invalidateSubmission();
              setTrainingEnd(event.target.value);
              // Retain the chosen end and show an error if the new start conflicts.
            }}>
              <option value="">{t("Select an observed session")}</option>
              {training.map(date => <option key={date} value={date}>{date}</option>)}
            </select>
          </label>
          <div className={label}><span>{t("Test start")}</span>
            <output className="flex min-h-9 items-center rounded-md border bg-muted px-2 tabular-nums" aria-label={t("Test start")}>{firstTest ?? "—"}</output>
          </div>
          <label className={label}>{t("Test end")}
            <select className={input} value={testEnd} disabled={!firstTest} onChange={event => { invalidateSubmission(); setTestEnd(event.target.value); }}>
              <option value="">{t("Select an observed session")}</option>
              {testEnd && !endings.some(point => point.session_date === testEnd) && <option value={testEnd} disabled>{testEnd}</option>}
              {endings.map(point => <option key={point.session_date} value={point.session_date}>{point.session_date}</option>)}
            </select>
          </label>
          {firstTest && <p className="text-xs text-muted-foreground sm:col-span-3">
            {t("Training observations")} · {(trainingIndex + 1).toLocaleString(language)} · {t("Test sessions")} · {window?.testCount.toLocaleString(language) ?? "—"}
          </p>}
          {trainingEnd && testEnd && !window && <p role="alert" className="text-destructive sm:col-span-3">
            {t("Choose a test end after the next observed session. At least two test sessions are required.")}
          </p>}
        </fieldset>
        <fieldset className="grid min-w-0 gap-3 sm:grid-cols-2 xl:grid-cols-3" disabled={sending || Boolean(queued)}>
          <legend className="mb-2 font-medium">{t("Simulation assumptions")}</legend>
          {backtestFields.map(({ key, label: fieldLabel, hint }) => {
            const invalid = !validBacktestDecimal(key, assumptions[key]);
            const hintId = fieldId + key;
            return <label className={label} key={key}>{t(fieldLabel)}
              <input className={input} inputMode="decimal" value={assumptions[key]}
                aria-label={t(fieldLabel)} aria-invalid={invalid} aria-describedby={hintId} onChange={event => {
                  invalidateSubmission();
                  setAssumptions({ ...assumptions, [key]: event.target.value });
                }} />
              <span id={hintId} className={invalid ? "text-xs text-destructive" : "text-xs text-muted-foreground"}>
                {invalid && t("Use a fixed-point decimal with up to six decimal places.") + " "}{t(hint)}
              </span>
            </label>;
          })}
        </fieldset>
        <section aria-label={t("Review backtest")} className="space-y-2 rounded-md border bg-muted/30 p-3 text-sm">
          <h3 className="font-medium">{t("Review backtest")}</h3>
          <p className="font-semibold">{t(backtestDisclosure)}</p>
          <p className="text-muted-foreground">{t("The source is a price index and returns exclude dividends. The benchmark ignores transaction costs.")}</p>
          <dl className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {window && <div><dt className="text-muted-foreground">{t("Training end")} · {t("Test period")}</dt>
              <dd>{window.training_end} · {window.test_start} – {window.test_end} · {window.testCount.toLocaleString(language)} {t("Test sessions")}</dd></div>}
            {backtestFields.map(({ key, label: fieldLabel }) => <div key={key}><dt className="text-muted-foreground">{t(fieldLabel)}</dt><dd className="break-all">{assumptions[key] || "—"}</dd></div>)}
            {fixedAssumptionLabels.map(([name, value]) => <div key={name}><dt className="text-muted-foreground">{t(name)}</dt><dd>{t(value)}</dd></div>)}
          </dl>
        </section>
        <Button type="submit" disabled={sending || Boolean(queued) || !window || !valid}>{t(sending ? "Queueing backtest…" : "Queue backtest")}</Button>
      </form>}
      {queueError && <p role="alert" className="text-destructive">{t(queueError)}</p>}
      {queued && <section aria-label={t("Backtest queued")} className="space-y-2 rounded-md border p-3">
        <p role="status">{t("Backtest queued")} · <span className="break-all">{queued.id}</span></p>
        <div className="flex flex-wrap gap-2">
          <Button type="button" variant="outline" onClick={() => onInspect(queued)}>{t("Inspect backtest")}</Button>
          <Button type="button" variant="outline" onClick={onHistory}>{t("Backtest History")}</Button>
          <Button type="button" variant="outline" onClick={startNew}>{t("New Backtest")}</Button>
        </div>
      </section>}
    </CardContent>
  </Card>;
}
