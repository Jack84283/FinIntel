import { useEffect, useMemo, useState } from "react";
import { CalendarClock, Pause, Play, RefreshCw, RotateCcw, Square, Zap } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useLocale } from "./localization";
import { useNotify } from "./notifications";
import {
  controlDailyAcquisition,
  fetchDailyStatus,
  runDailyAcquisition,
  saveDailySettings,
  type DailyProvider,
  type DailyStatus,
} from "./daily-acquisition-api";

const providerName: Record<DailyProvider, string> = {
  alpaca_sip: "Alpaca SIP",
  tiingo: "Tiingo",
  alpha_vantage: "Alpha Vantage",
};

export function DailyAcquisitionPanel() {
  const { t } = useLocale();
  const notify = useNotify();
  const [status, setStatus] = useState<DailyStatus | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const refresh = async () => {
    try {
      setStatus(await fetchDailyStatus());
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15000);
    return () => window.clearInterval(timer);
  }, []);

  const update = async (patch: Record<string, unknown>) => {
    setSaving(true);
    try {
      await saveDailySettings(patch);
      await refresh();
      notify(t("Daily acquisition settings saved."), "success");
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(message);
      notify(t("Daily acquisition settings could not be saved."), "error");
    } finally {
      setSaving(false);
    }
  };

  const active = status?.status === "running" || status?.status === "paused" || status?.status === "scheduled";
  const jobId = status?.job_id;
  const progress = useMemo(() => {
    if (!status || status.total_instruments === 0) return 0;
    return Math.round(((status.completed_instruments + status.failed_instruments + status.incomplete_instruments) / status.total_instruments) * 100);
  }, [status]);

  const action = async (kind: "run" | "pause" | "resume" | "cancel" | "retry-failed") => {
    try {
      if (kind === "run") {
        await runDailyAcquisition();
        notify(t("Daily acquisition started."), "success");
      } else if (jobId) {
        await controlDailyAcquisition(jobId, kind);
        notify(t("Daily acquisition control updated."), "success");
      }
      await refresh();
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(message);
      notify(t("Daily acquisition action failed. See details below."), "error");
    }
  };

  return (
    <Card aria-labelledby="daily-acquisition-title" className="min-w-0">
      <CardHeader className="gap-y-2">
        <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 space-y-1">
            <CardTitle><span id="daily-acquisition-title" className="flex items-center gap-2"><CalendarClock aria-hidden="true" className="size-4" />{t("Daily Acquisition")}</span></CardTitle>
            <CardDescription>{t("Run incremental stock, ETF, and index refreshes after the U.S. market close, with durable progress and recovery.")}</CardDescription>
          </div>
          <Badge variant={status?.status === "completed" ? "success" : status?.status === "failed" || status?.status === "partial" ? "destructive" : "outline"}>
            {status?.status ?? t("Checking")}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            <input type="checkbox" checked={status?.enabled ?? false} disabled={!status || saving} onChange={(event) => void update({ enabled: event.target.checked })} />
            {t("Automatic acquisition")}
          </label>
          <label className="grid gap-1 text-xs text-muted-foreground">
            {t("Schedule")}
            <input type="time" value={status?.scheduled_execution_time ?? "20:00"} disabled={!status || saving} onChange={(event) => void update({ schedule_time: event.target.value })} className="h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground" />
          </label>
          <label className="grid gap-1 text-xs text-muted-foreground">
            {t("Primary provider")}
            <select value={status?.primary_provider ?? "alpaca_sip"} disabled={!status || saving} onChange={(event) => void update({ primary_provider: event.target.value })} className="h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground">
              {(Object.keys(providerName) as DailyProvider[]).map((provider) => <option key={provider} value={provider}>{providerName[provider]}</option>)}
            </select>
          </label>
          <label className="grid gap-1 text-xs text-muted-foreground">
            {t("Secondary provider")}
            <select value={status?.secondary_provider ?? ""} disabled={!status || saving} onChange={(event) => void update({ secondary_provider: event.target.value || null })} className="h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground">
              <option value="">{t("None")}</option>
              {(Object.keys(providerName) as DailyProvider[]).map((provider) => <option key={provider} value={provider}>{providerName[provider]}</option>)}
            </select>
          </label>
          <label className="grid gap-1 text-xs text-muted-foreground">
            {t("Catch-up limit")}
            <input type="number" min={1} max={30} value={status?.catch_up_limit ?? 5} disabled={!status || saving} onChange={(event) => void update({ catch_up_limit: Number(event.target.value) })} className="h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground" />
          </label>
        </div>
        <div className="rounded-md border border-border bg-background/40 p-3 text-xs">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <span className="text-muted-foreground">{t("Progress")}</span>
            <span className="tabular-nums">{status?.completed_instruments ?? 0} / {status?.total_instruments ?? 0} · {progress}%</span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-muted" aria-label={t("Daily acquisition progress")} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}>
            <div className="h-full rounded-full bg-positive transition-[width]" style={{ width: `${progress}%` }} />
          </div>
          <div className="mt-2 grid gap-1 text-muted-foreground sm:grid-cols-3">
            <span>{t("Successful")}: {status?.successful_imports ?? 0}</span>
            <span>{t("Failed")}: {status?.failed_instruments ?? 0}</span>
            <span>{t("Incomplete")}: {status?.incomplete_instruments ?? 0}</span>
            <span>{t("Pending")}: {status?.pending_instruments ?? 0}</span>
          </div>
        </div>
        <div className="grid gap-1 text-xs text-muted-foreground sm:grid-cols-2">
          <span>{t("Last run")}: {status?.last_execution_time ?? "—"}</span>
          <span>{t("Next run")}: {status?.next_scheduled_execution ?? "—"}</span>
          <span>{t("Current instrument")}: {status?.current_instrument ?? "—"}</span>
          <span>{t("Last updated trading session")}: {status?.last_updated_trading_session ?? "—"}</span>
        </div>
        <div className="flex flex-wrap justify-end gap-2 border-t border-border pt-3">
          <Button type="button" size="sm" onClick={() => void action("run")} disabled={active || !status}><Zap aria-hidden="true" />{t("Run Now")}</Button>
          <Button type="button" size="sm" variant="secondary" onClick={() => void action("pause")} disabled={!active || status?.status === "paused" || !jobId}><Pause aria-hidden="true" />{t("Pause")}</Button>
          <Button type="button" size="sm" variant="secondary" onClick={() => void action("resume")} disabled={!jobId || status?.status !== "paused"}><Play aria-hidden="true" />{t("Resume")}</Button>
          <Button type="button" size="sm" variant="secondary" onClick={() => void action("cancel")} disabled={!active || !jobId}><Square aria-hidden="true" />{t("Cancel")}</Button>
          <Button type="button" size="sm" variant="outline" onClick={() => void action("retry-failed")} disabled={!jobId || (status?.failed_instruments ?? 0) + (status?.incomplete_instruments ?? 0) === 0}><RotateCcw aria-hidden="true" />{t("Retry Failed")}</Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => void refresh()} disabled={!status}><RefreshCw aria-hidden="true" />{t("Refresh Status")}</Button>
        </div>
        {status?.status === "running" && status.current_instrument && <p role="status" className="text-xs text-muted-foreground">{t("Processing")} {status.current_instrument}…</p>}
        {error && <p role="alert" className="break-words text-xs text-destructive">{error}</p>}
      </CardContent>
    </Card>
  );
}
