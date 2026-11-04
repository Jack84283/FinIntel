import { useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useLocale } from "./localization";
import { type IndexCode } from "./index-history-api";
import { type QualityPolicy } from "./index-intelligence-api";
import {
  BUILTIN_MODEL_ID, BUILTIN_MODEL_VERSION, fetchModelResearch, validateModelResearchQuery,
  type ModelResearchQuery, type ModelResearchResult,
} from "./model-research-api";

const inputClass = "h-9 w-full min-w-0 rounded-md border border-input bg-background px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

/** No automatic execution or cloud calls. This is research, not a trading backtest. */
export function ModelResearchPanel({
  code, start, end, qualityPolicy,
}: {
  code: IndexCode;
  start: string;
  end: string;
  qualityPolicy: QualityPolicy;
}) {
  const { t, language } = useLocale();
  const [expanded, setExpanded] = useState(false);
  const [fastWindow, setFastWindow] = useState(5);
  const [slowWindow, setSlowWindow] = useState(20);
  const [result, setResult] = useState<ModelResearchResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const canRun = (() => {
    try {
      validateModelResearchQuery({
        code, start, end, qualityPolicy, fastWindow, slowWindow,
      });
      return true;
    } catch {
      return false;
    }
  })();

  useEffect(() => {
    generation.current++;
    setResult(null);
    setError("");
    setBusy(false);
  }, [code, start, end, qualityPolicy, fastWindow, slowWindow]);

  function run() {
    const requestId = ++generation.current;
    const query: ModelResearchQuery = {
      code, start, end, qualityPolicy, fastWindow, slowWindow,
    };
    setResult(null);
    setError("");
    setBusy(true);
    fetchModelResearch(query).then(payload => {
      if (requestId === generation.current) setResult(payload);
    }).catch(reason => {
      if (requestId === generation.current) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    }).finally(() => {
      if (requestId === generation.current) setBusy(false);
    });
  }

  const count = (value: number) => value.toLocaleString(language);
  const latest = result?.points.slice(-25).reverse() ?? [];
  return (
    <Card>
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
        <div>
          <CardTitle>{t("Local model research")}</CardTitle>
          <CardDescription>
            {t("Evaluate a versioned built-in indicator on stored index observations. No orders or backtests.")}
          </CardDescription>
        </div>
        <Button type="button" variant="outline" size="sm" aria-expanded={expanded}
          onClick={() => setExpanded(value => !value)}>
          {expanded ? t("Close model research") : t("Explore model research")}
        </Button>
      </CardHeader>
      {expanded && <CardContent className="space-y-3">
        <div className="flex flex-wrap gap-2">
          <Badge variant="outline">{BUILTIN_MODEL_ID} v{BUILTIN_MODEL_VERSION}</Badge>
          <Badge variant="outline">{t("Research signals only")}</Badge>
        </div>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            {t("Fast SMA window")}
            <select className={inputClass} aria-label={t("Fast SMA window")}
              value={fastWindow} onChange={event => setFastWindow(Number(event.target.value))}>
              {[2, 5, 10, 20, 50, 100].map(value =>
                <option key={value} value={value}>{value} {t("sessions")}</option>)}
            </select>
          </label>
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            {t("Slow SMA window")}
            <select className={inputClass} aria-label={t("Slow SMA window")}
              value={slowWindow} onChange={event => setSlowWindow(Number(event.target.value))}>
              {[10, 20, 50, 100, 200, 252].map(value =>
                <option key={value} value={value}>{value} {t("sessions")}</option>)}
            </select>
          </label>
          <div className="min-w-0 space-y-1 text-xs text-muted-foreground">
            <p>{t("Source index")}</p>
            <strong className="block pt-2 text-sm text-foreground">{code} · FRED</strong>
          </div>
          <div className="min-w-0 space-y-1 text-xs text-muted-foreground">
            <p>{t("Selected analysis window")}</p>
            <strong className="block pt-2 text-sm text-foreground">{start || "—"} – {end || "—"}</strong>
          </div>
        </div>
        <p className="text-xs text-muted-foreground">
          {t("SMA signals are generated after the observed close and cannot be traded at that same close. Dividends, slippage and membership backtests are not modeled.")}
        </p>
        {!canRun && <p role="status" className="text-sm text-muted-foreground">
          {t("Choose a price index, valid dates and a fast window shorter than the slow window. VIX is excluded.")}
        </p>}
        <Button type="button" size="sm" disabled={!canRun || busy} onClick={run}>
          {busy ? t("Evaluating local indicator…") : t("Evaluate local indicator")}
        </Button>
        {error && <p role="alert" className="text-sm text-destructive">
          {t("Local model evaluation unavailable")}: {error}
        </p>}
        {result && <>
          {result.quality_warning && <p role="status" className="text-sm text-muted-foreground">
            {t("Unverified model input: research only; not valid for backtesting.")}
          </p>}
          <div className="grid gap-2 sm:grid-cols-3">
            <div className="rounded-md border border-border p-3">
              <p className="text-xs text-muted-foreground">{t("Observed sessions")}</p>
              <strong className="text-lg tabular-nums">{count(result.observations)}</strong>
            </div>
            <div className="rounded-md border border-border p-3">
              <p className="text-xs text-muted-foreground">{t("Latest research signal")}</p>
              <strong className="text-lg">{t(result.points.at(-1)?.signal_after_close ?? "Warm-up")}</strong>
            </div>
            <div className="rounded-md border border-border p-3">
              <p className="text-xs text-muted-foreground">{t("Calculation version")}</p>
              <strong className="text-sm">{result.lineage.implementation_revision}</strong>
            </div>
          </div>
          <div className="max-h-[430px] overflow-auto rounded-md border border-border">
            <table className="w-full border-collapse text-left text-xs tabular-nums">
              <caption className="px-3 py-2 text-left text-muted-foreground">
                {t("Latest 25 observed model signals; blanks are warm-up sessions.")}
              </caption>
              <thead className="sticky top-0 bg-muted text-muted-foreground">
                <tr>
                  <th scope="col" className="p-2">{t("Session date")}</th>
                  <th scope="col" className="p-2 text-right">{t("Close")}</th>
                  <th scope="col" className="p-2 text-right">{t("Fast SMA")}</th>
                  <th scope="col" className="p-2 text-right">{t("Slow SMA")}</th>
                  <th scope="col" className="p-2">{t("Signal after close")}</th>
                </tr>
              </thead>
              <tbody>
                {latest.map(point => <tr key={point.session_date} className="border-t border-border">
                  <th scope="row" className="p-2 font-medium">{point.session_date}</th>
                  <td className="p-2 text-right">{point.close}</td>
                  <td className="p-2 text-right">{point.fast_sma ?? "—"}</td>
                  <td className="p-2 text-right">{point.slow_sma ?? "—"}</td>
                  <td className="p-2">{t(point.signal_after_close ?? "Warm-up")}</td>
                </tr>)}
              </tbody>
            </table>
          </div>
          <div className="space-y-1 break-all text-xs text-muted-foreground">
            <p>{t("Source snapshot SHA-256")}: {result.lineage.source_snapshot_sha256}</p>
            <p>{t("Reproducibility hash")}: {result.reproducibility_sha256}</p>
            <p>{t("Output SHA-256")}: {result.output_sha256}</p>
            <p>{t("Quality status")}: {result.lineage.quality_status}</p>
          </div>
          <p className="text-xs text-muted-foreground">
            {t("This evaluation is not a durable job or backtest; no equity curve or trading performance is claimed.")}
          </p>
        </>}
      </CardContent>}
    </Card>
  );
}
