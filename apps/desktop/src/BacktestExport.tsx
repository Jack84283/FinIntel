import { invoke } from "@tauri-apps/api/core";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import type { BacktestJobResult, BacktestJobSummary } from "./backtest-jobs-api";
import { useLocale } from "./localization";
import { useNotify } from "./notifications";

const formats = {
  json: "Verified JSON report",
  equity: "Equity history CSV",
  transactions: "Transaction history CSV",
} as const;

export function BacktestExport({ job, result }: { job: BacktestJobSummary; result: BacktestJobResult }) {
  const { t, language } = useLocale();
  const notify = useNotify();
  const [format, setFormat] = useState<keyof typeof formats>("json");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  async function save() {
    if (!confirmed || pending.current) return;
    pending.current = true;
    setBusy(true);
    setConfirmed(false);
    try {
      const status = await invoke<unknown>("backtest_export", {
        jobId: job.id, format, locale: language, requestSha256: result.request_sha256, outputSha256: result.report.output_sha256,
      });
      if (status === "saved") notify(t("Backtest report saved locally."), "success");
      else if (status === "cancelled") notify(t("Backtest export cancelled."));
      else throw new Error("invalid_response");
    } catch (error) {
      const message = error === "invalid_response" ? "The saved backtest failed integrity verification." :
        error === "invalid_destination" ? "Choose a regular local file with the correct extension." :
          "The backtest report could not be saved.";
      notify(t(message), "error");
    } finally {
      pending.current = false;
      if (mounted.current) setBusy(false);
    }
  }

  return <section aria-label={t("Local export")} className="space-y-3 rounded-md border p-3">
    <h3 className="font-semibold">{t("Local export")}</h3>
    <p>{t("Exported reports contain financial research data and will be saved locally as plain-text JSON or CSV without encryption. Anyone with access to the file may read its contents. Keep it in a secure location and delete it when no longer needed. Exported files are for review and cannot be reimported into FinIntel through this feature.")}</p>
    <div className="flex flex-wrap items-center gap-2">
      <label className="flex items-center gap-2">{t("Report format")}
        <select className="h-9 rounded-md border border-input bg-background px-2 focus-visible:ring-2 focus-visible:ring-ring" value={format} disabled={busy}
          onChange={event => { setFormat(event.target.value as keyof typeof formats); setConfirmed(false); }}>
          {Object.entries(formats).map(([value, name]) => <option key={value} value={value}>{t(name)}</option>)}
        </select>
      </label>
      <label className="flex items-center gap-2"><input type="checkbox" checked={confirmed} disabled={busy}
        onChange={event => setConfirmed(event.target.checked)} />{t("I understand the report will be saved as plain text.")}</label>
      <Button type="button" disabled={!confirmed || busy} onClick={() => void save()}>{t("Choose save location")}</Button>
    </div>
  </section>;
}
