import { useLayoutEffect, useRef, useState, type RefObject } from "react";
import { Button } from "@/components/ui/button";
import { EvidenceHash } from "./components/EvidenceHash";
import {
  cancelBacktestJob, getBacktestJob, retryBacktestJob, type BacktestJobResult, type BacktestJobSummary,
} from "./backtest-jobs-api";
import { backtestDisclosure, backtestError, backtestFields, fixedAssumptionLabels } from "./backtest-workflow";
import { AnalysisJobError } from "./analysis-jobs-api";
import { useLocale } from "./localization";

function BacktestEvidence({ job, result }: { job: BacktestJobSummary; result: BacktestJobResult }) {
  const { t, language } = useLocale();
  const { request } = job;
  const { report } = result;
  const entries = [
    ["Backtest job ID", job.id], ["Source job ID", request.source_job_id],
    ["Model", `${t("SMA crossover")} · ${request.simulation.model_version}`],
    ["Source implementation revision", request.source_implementation_revision],
    ["Engine revision", request.engine_revision],
    ["Original quality policy", t("require_complete")],
    ["Evaluated at (UTC)", result.evaluated_at_utc],
  ];
  const hashes = [
    ["Source snapshot SHA-256", report.source_snapshot_sha256],
    ["Source request SHA-256", request.source_request_sha256],
    ["Source output SHA-256", request.source_output_sha256],
    ["Source lineage SHA-256", request.source_lineage_sha256],
    ["Backtest request SHA-256", result.request_sha256],
    ["Backtest input SHA-256", report.input_sha256],
    ["Backtest report output SHA-256", report.output_sha256],
  ];
  return <section aria-label={t("Verified backtest result")} className="space-y-3 border-t pt-3">
    <h3 className="font-semibold">{t("Verified backtest result")}</h3>
    <p className="font-medium">{t(backtestDisclosure)}</p>
    <p className="text-muted-foreground">{t("Archived backtests retain their original evidence after local source data changes. New backtests must pass current source checks.")}</p>
    <dl className="grid gap-2 sm:grid-cols-2">
      <div><dt className="text-muted-foreground">{t("Test sessions")}</dt><dd>{report.test_sessions.toLocaleString(language)}</dd></div>
      <div><dt className="text-muted-foreground">{t("Hypothetical transactions")}</dt><dd>{report.metrics.transactions.toLocaleString(language)}</dd></div>
      <div><dt className="text-muted-foreground">{t("Cumulative strategy return (fraction)")}</dt><dd className="break-all tabular-nums">{report.metrics.cumulative_return}</dd></div>
      <div><dt className="text-muted-foreground">{t("Cumulative benchmark return (fraction)")}</dt><dd className="break-all tabular-nums">{report.metrics.frictionless_benchmark_cumulative_return}</dd></div>
      {entries.map(([key, value]) => <div key={key}><dt className="text-muted-foreground">{t(key)}</dt><dd className="break-words">{value}</dd></div>)}
    </dl>
    <details className="rounded-md border px-3 py-2">
      <summary className="cursor-pointer font-medium">{t("Applied assumptions")}</summary>
      <dl className="mt-2 grid gap-2 sm:grid-cols-2">
        {backtestFields.map(({ key, label }) => <div key={key}><dt className="text-muted-foreground">{t(label)}</dt><dd>{request.simulation.assumptions[key]}</dd></div>)}
        {fixedAssumptionLabels.map(([key, value]) => <div key={key}><dt className="text-muted-foreground">{t(key)}</dt><dd>{t(value)}</dd></div>)}
      </dl>
    </details>
    <details className="rounded-md border px-3 py-2">
      <summary className="cursor-pointer font-medium">{t("Evidence hashes")}</summary>
      {hashes.map(([key, value]) => <EvidenceHash key={key} label={t(key)} value={value} />)}
    </details>
  </section>;
}

export function BacktestJobDetail({ initial, selectionGeneration, workspaceGeneration, onChanged, onClose }: {
  initial: BacktestJobSummary;
  selectionGeneration: RefObject<number>;
  workspaceGeneration?: RefObject<number>;
  onChanged: () => void;
  onClose: () => void;
}) {
  const { t } = useLocale();
  const { id, request_sha256 } = initial;
  const [job, setJob] = useState(initial);
  const [result, setResult] = useState<BacktestJobResult>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [refreshRequired, setRefreshRequired] = useState(false);
  const focus = useRef<HTMLElement>(null);
  const gate = useRef({ mounted: false, serial: 0, pending: null as "poll" | "action" | null });

  const watching = job.state === "queued" || job.state === "running";
  const canCancel = watching && job.status !== "cancel_requested";
  const canRetry = (job.state === "failed" || job.state === "cancelled") && job.attempts < 3;
  const checked = (candidate: BacktestJobSummary) => {
    if (candidate.id !== id || candidate.request_sha256 !== request_sha256) throw new AnalysisJobError("invalid_response");
    return candidate;
  };

  async function run(action: "refresh" | "result" | "cancel" | "retry", poll = false) {
    const scope = gate.current;
    if (!scope.mounted || scope.pending === "action" || (poll && scope.pending)) return;
    const serial = ++scope.serial;
    const selection = selectionGeneration.current;
    const workspace = workspaceGeneration?.current;
    const current = () => scope.mounted && scope.serial === serial &&
      selection === selectionGeneration.current && workspace === workspaceGeneration?.current;
    scope.pending = poll ? "poll" : "action";
    if (!poll) {
      setBusy(true);
      setError("");
      setResult(undefined);
    }
    let terminalCancellationRace = false;
    try {
      if (action === "cancel") {
        const ack = await cancelBacktestJob(id);
        if (!current()) return;
        if (ack.id !== id) throw new AnalysisJobError("invalid_response");
        const { status } = ack;
        terminalCancellationRace = status === "completed" || status === "failed";
        if (status === "cancel_requested" || status === "cancelled") {
          setJob(previous => ({
            ...previous,
            state: status === "cancel_requested" ? "running" : "cancelled",
            status,
            cancel_requested: 1,
          }));
        }
        onChanged();
      } else if (action === "retry") {
        const retry = checked(await retryBacktestJob(id));
        if (!current()) return;
        setJob(retry);
        onChanged();
        return;
      } else if (action === "result") {
        const detail = await getBacktestJob(id, true);
        if (!current()) return;
        checked(detail);
        if (detail.state !== "completed" || !detail.result || detail.result.request_sha256 !== request_sha256 ||
          detail.result.report.output_sha256 !== detail.output_sha256) throw new AnalysisJobError("invalid_response");
        setJob(detail);
        setResult(detail.result);
        return;
      }
      const latest = checked(await getBacktestJob(id));
      if (!current()) return;
      setJob(latest);
      setRefreshRequired(false);
      if (latest.state !== "completed") setResult(undefined);
      setError("");
    } catch (reason) {
      if (current()) {
        if (terminalCancellationRace) setRefreshRequired(true);
        else setError(backtestError(reason));
      }
    } finally {
      if (current()) {
        scope.pending = null;
        setBusy(false);
      }
    }
  }

  useLayoutEffect(() => {
    const scope = gate.current;
    scope.mounted = true;
    focus.current?.focus();
    void run("refresh", true);
    return () => { scope.mounted = false; scope.serial++; scope.pending = null; };
  }, [id, request_sha256]);

  useLayoutEffect(() => {
    if (!watching) return;
    const timer = window.setInterval(() => { void run("refresh", true); }, 5000);
    return () => window.clearInterval(timer);
  }, [id, request_sha256, watching]);

  const simulation = job.request.simulation;
  return <section ref={focus} tabIndex={-1} aria-label={t("Backtest details")}
    className="min-w-0 scroll-mt-24 space-y-3 rounded-md border p-3 text-sm focus-visible:ring-2 focus-visible:ring-ring">
    <header className="flex flex-wrap items-start justify-between gap-2">
      <div className="min-w-0 space-y-1">
        <h3 className="font-semibold">{t("Backtest details")}</h3>
        <p className="break-all text-xs text-muted-foreground">{job.id}</p>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void run("refresh")}>{t("Refresh details")}</Button>
        <Button type="button" size="sm" variant="ghost" onClick={onClose}>{t("Close details")}</Button>
      </div>
    </header>
    <div role="status" className="flex flex-wrap gap-x-3 gap-y-1 tabular-nums">
      <strong>{t(job.status)}</strong><span>{t("Progress")} {job.progress}%</span><span>{t("Attempts")} {job.attempts}/3</span>
      {busy && <span>{t("Loading backtest details…")}</span>}
    </div>
    <dl className="grid gap-2 sm:grid-cols-2">
      <div><dt className="text-muted-foreground">{t("Source index")}</dt><dd>{simulation.index_code} · {t("SMA crossover")}</dd></div>
      <div><dt className="text-muted-foreground">{t("Training end")}</dt><dd>{simulation.training_end}</dd></div>
      <div><dt className="text-muted-foreground">{t("Test period")}</dt><dd>{simulation.test_start} – {simulation.test_end}</dd></div>
      <div><dt className="text-muted-foreground">{t("Source job ID")}</dt><dd className="break-all">{job.request.source_job_id}</dd></div>
    </dl>
    {refreshRequired && <p role="alert" className="text-destructive">{t("The backtest finished before cancellation. Refresh details to see its final status and results.")}</p>}
    {job.error_code && <p role="status">{t(backtestError(job.error_code))}</p>}
    <div className="flex flex-wrap gap-2">
      {!refreshRequired && canCancel && <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void run("cancel")}>{t("Cancel job")}</Button>}
      {!refreshRequired && canRetry && <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void run("retry")}>{t("Retry original request")}</Button>}
      {job.state === "completed" && job.output_sha256 &&
        <Button type="button" size="sm" disabled={busy} onClick={() => void run("result")}>{t("View result")}</Button>}
    </div>
    {error && <p role="alert" className="text-destructive">{t(error)}</p>}
    {job.state === "completed" && result && <BacktestEvidence key={result.report.output_sha256} job={job} result={result} />}
  </section>;
}
