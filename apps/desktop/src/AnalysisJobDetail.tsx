import { useLayoutEffect, useRef, useState, type RefObject } from "react";
import { Button } from "@/components/ui/button";
import { AnalysisResultVisualization } from "./AnalysisResultVisualization";
import { useLocale } from "./localization";
import {
  cancelAnalysisJob, getAnalysisJob, retryAnalysisJob, type AnalysisJobSummary, type AnalysisResult,
} from "./analysis-jobs-api";
import { analysisErrorMessage, MODELS, PARAMETERS, SOURCE_NAMES } from "./analysis-workflow";

const isActive = (job: AnalysisJobSummary) => job.state === "queued" || job.state === "running";

function Hash({ label, value }: { label: string; value: string }) {
  const { t } = useLocale();
  const [status, setStatus] = useState("");
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setStatus("Copied");
    } catch {
      setStatus("Clipboard unavailable. Select the hash to copy it.");
    }
  };
  return <div className="grid gap-1 border-t py-2">
    <span>{label}</span>
    <code className="select-all break-all">{value}</code>
    <div><Button type="button" size="sm" variant="outline" onClick={() => void copy()}>{t("Copy hash")}</Button>
      <span className="ml-2" role="status">{status && t(status)}</span></div>
  </div>;
}

function qualityWarningLabel(code: string): string {
  switch (code) {
    case "quality_scan_not_run": return "A data quality scan has not been run.";
    case "quality_scan_partial": return "The data quality scan is incomplete.";
    case "quality_scan_stale": return "The data quality scan is out of date.";
    case "unresolved_quality_findings": return "The source has unresolved data quality findings.";
    default: return "The saved result includes an additional data quality warning.";
  }
}

function Evidence({ result }: { result: AnalysisResult }) {
  const { t, language } = useLocale();
  const { evidence } = result;
  return <section aria-label={t("Verified research result")} className="space-y-3 border-t pt-3">
    <h3 className="font-semibold">{t("Verified research result")}</h3>
    <p className="text-muted-foreground">{t("Research only. Archived results retain their original evidence even if local data has changed.")}</p>
    {evidence.warnings.filter(warning => warning !== "quality_override").map(warning =>
      <p key={warning} className="text-muted-foreground">{t(qualityWarningLabel(warning))}</p>)}
    <dl className="grid gap-2 sm:grid-cols-2">
      <div><dt className="text-muted-foreground">{t("Model")}</dt><dd>{evidence.model.model_id} · {evidence.model.model_version}</dd></div>
      <div><dt className="text-muted-foreground">{t("Implementation revision")}</dt><dd className="break-words">{evidence.model.implementation_revision}</dd></div>
      <div><dt className="text-muted-foreground">{t("Calculation version")}</dt><dd>{evidence.calculation_version}</dd></div>
      <div><dt className="text-muted-foreground">{t("Evaluated at (UTC)")}</dt><dd>{result.evaluated_at_utc}</dd></div>
      <div><dt className="text-muted-foreground">{t("Requested window")}</dt><dd>{evidence.requested.start} – {evidence.requested.end}</dd></div>
      <div><dt className="text-muted-foreground">{t("Observed sessions")}</dt><dd>{evidence.observed_sessions[0]} – {evidence.observed_sessions.at(-1)} · {evidence.observed_sessions.length.toLocaleString(language)}</dd></div>
      <div><dt className="text-muted-foreground">{t("Data quality policy")}</dt><dd>{t(evidence.quality_policy)}</dd></div>
      <div><dt className="text-muted-foreground">{t("Parameters")}</dt><dd>{Object.entries(evidence.parameters).map(([key, value]) => <div key={key}>{t(PARAMETERS[key])} · {value}</div>)}</dd></div>
    </dl>
    <ol className="list-decimal space-y-1 pl-5">
      {evidence.sources.map(source => <li key={source.identity.code}>
        {t(SOURCE_NAMES[source.identity.code])} · {source.identity.code} · FRED · {source.observed.start} – {source.observed.end} · {t(source.quality.status)}
        <div className="text-muted-foreground">{t("Observations")} {source.observed.count.toLocaleString(language)} · {t("Aligned observations")} {source.aligned_count.toLocaleString(language)} · {t("Excluded observations")} {source.excluded_count.toLocaleString(language)}
          {source.quality.warnings.map(warning => <span key={warning}> · {t(qualityWarningLabel(warning))}</span>)}</div>
      </li>)}
    </ol>
    <details className="rounded-md border px-3 py-2">
      <summary className="cursor-pointer font-medium">{t("Evidence hashes")}</summary>
      {evidence.sources.map(source => <Hash key={source.identity.code} label={source.identity.code + " · " + t("Source snapshot SHA-256")} value={source.snapshot_sha256} />)}
      <Hash label={t("Output SHA-256")} value={result.output_sha256} />
      <Hash label={t("Reproducibility SHA-256")} value={result.reproducibility_sha256} />
    </details>

  </section>;
}

export function AnalysisJobDetail({ id, initial, onClose, onChanged, onOpenJob, selectionGeneration }: {
  id: string;
  initial?: AnalysisJobSummary;
  onClose?: () => void;
  onChanged?: () => void;
  selectionGeneration?: RefObject<number>;
  onOpenJob?: (id: string) => void;
}) {
  const { t } = useLocale();
  const [job, setJob] = useState<AnalysisJobSummary | undefined>(initial);
  const [result, setResult] = useState<AnalysisResult>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const gate = useRef({ alive: false, revision: 0, pending: null as "poll" | "action" | null });
  const canCancel = job && isActive(job) && job.status !== "cancel_requested";
  const canRetry = job && (job.state === "failed" || job.state === "cancelled") && job.attempts < 3;

  function run(action: "refresh" | "result" | "cancel" | "retry", automatic = false) {
    const scope = gate.current;
    if (!scope.alive || scope.pending === "action" || (automatic && scope.pending)) return;
    const revision = ++scope.revision;
    scope.pending = automatic ? "poll" : "action";
    const selection = selectionGeneration?.current;
    const current = () => scope.alive && scope.revision === revision && selectionGeneration?.current === selection;
    if (!automatic) {
      setError("");
      setBusy(true);
      setResult(undefined);
    }
    void (async () => {
      try {
        if (action === "cancel") await cancelAnalysisJob(id);
        if (action === "retry") await retryAnalysisJob(id);
        if (!current()) return;
        if (action === "cancel" || action === "retry") onChanged?.();
        if (action === "result") {
          const detail = await getAnalysisJob(id, true);
          if (!current()) return;
          if (detail.state !== "completed" || detail.id !== id || detail.request_sha256 !== job?.request_sha256 || !detail.result) {
            setError("Result or source evidence failed integrity validation.");
            return;
          }
          setJob(detail);
          setResult(detail.result);
        } else {
          const latest = await getAnalysisJob(id);
          if (current()) {
            if (latest.id !== id || (job && latest.request_sha256 !== job.request_sha256)) {
              setResult(undefined);
              setError("Result or source evidence failed integrity validation.");
              return;
            }
            setJob(latest);
            setError("");
          }
        }
      } catch (reason) {
        if (current()) setError(analysisErrorMessage(reason));
      } finally {
        if (current()) {
          scope.pending = null;
          setBusy(false);
        }
      }
    })();
  }

  useLayoutEffect(() => {
    const scope = { alive: true, revision: 0, pending: null };
    gate.current = scope;
    run("refresh", true);
    return () => { scope.alive = false; };
    // A detail is keyed to its job selection. Actions capture the current request.
  }, [id]);

  const watching = job && isActive(job);
  useLayoutEffect(() => {
    if (!watching) return;
    const timer = window.setInterval(() => run("refresh", true), 5000);
    return () => window.clearInterval(timer);
    // Only active jobs are polled. Each poll reads a summary for the fixed ID.
  }, [id, watching]);

  return <section aria-label={t("Analysis Library job details")} className="min-w-0 space-y-3 rounded-md border p-3 text-sm">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="font-semibold">{t("Analysis Library job details")}</h3>
      <div className="flex flex-wrap gap-2">
        {onOpenJob && <Button type="button" variant="outline" size="sm" onClick={() => onOpenJob(id)}>{t("Open job details")}</Button>}
        <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => run("refresh")}>{t("Refresh details")}</Button>
        {onClose && <Button type="button" variant="ghost" size="sm" onClick={() => {
          gate.current.alive = false;
          onClose();
        }}>{t("Close details")}</Button>}
      </div>
    </div>
    <p className="break-all font-mono text-xs text-muted-foreground">{id}</p>
    {!job && !error && <p role="status">{t("Loading job details…")}</p>}
    {job && <>
      <div role="status" className="flex flex-wrap gap-x-3 gap-y-1 tabular-nums">
        <span>{t(MODELS[job.model_id][0])} · {job.model_version}</span>
        <strong>{t(job.status)}</strong>
        <span>{t("Progress")} {job.progress}%</span>
        <span>{t("Attempts")} {job.attempts}/3</span>
      </div>
      <p>{job.request.sources.map(source => source.identity.code).join(" → ")} · {job.request.window.start} – {job.request.window.end}</p>
      {job.error_code && <p role="status">{job.error_code === "source_changed"
        ? t("The original source snapshot has changed. Review current sources before submitting a new job.") : t(job.error_code)}</p>}
      <div className="flex flex-wrap gap-2">
        {job.state === "completed" && <Button type="button" size="sm" disabled={busy} onClick={() => run("result")}>{t("View result")}</Button>}
        {canCancel && <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => run("cancel")}>{t("Cancel job")}</Button>}
        {canRetry && <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => run("retry")}>{t("Retry original request")}</Button>}
      </div>
    </>}
    {error && <p role="alert" className="text-destructive">{t(error)}</p>}
    {result && job?.id === id && job.state === "completed" && <div className="min-w-0 space-y-4" key={id + ":" + job.request_sha256 + ":" + result.output_sha256}>
      <AnalysisResultVisualization result={result} />
      <Evidence result={result} />
    </div>}
  </section>;
}
