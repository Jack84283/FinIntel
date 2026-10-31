import { useEffect, useState } from "react";
import { Activity, Download, RefreshCw } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { useLocale } from "./localization";
import { useNotify } from "./notifications";
import {
  INDEX_CODES, acquireIndexHistory, listIndexHistory, readIndexHistory,
  type IndexCatalogRecord, type IndexCode, type IndexHistory,
} from "./index-history-api";

const display = (value: string) => new Intl.NumberFormat("en-US", { maximumFractionDigits: 3 }).format(Number(value));

export function IndexHistoryCard() {
  const { t } = useLocale();
  const notify = useNotify();
  const [catalog, setCatalog] = useState<IndexCatalogRecord[]>([]);
  const [selected, setSelected] = useState<IndexCode>("SP500");
  const [history, setHistory] = useState<IndexHistory | null>(null);
  const [loading, setLoading] = useState(false);
  const [reading, setReading] = useState(false);
  const [error, setError] = useState("");
  const [detailsError, setDetailsError] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");

  const reloadCatalog = async () => {
    setCatalog(await listIndexHistory());
  };

  useEffect(() => {
    let active = true;
    void listIndexHistory()
      .then(items => { if (active) setCatalog(items); })
      .catch(reason => { if (active) setError(String(reason)); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    setHistory(null);
    setDetailsError("");
    setReading(true);
    void readIndexHistory(selected, 250)
      .then(data => { if (active) setHistory(data); })
      .catch(reason => { if (active) setDetailsError(String(reason)); })
      .finally(() => { if (active) setReading(false); });
    return () => { active = false; };
  }, [selected, catalog]);

  const download = async (all: boolean) => {
    if (loading) return;
    if (startDate && endDate && startDate > endDate) {
      setError(t("Backfill start must not be after the end date."));
      return;
    }
    setLoading(true);
    setError("");
    let success = 0;
    const failures: string[] = [];
    for (const code of (all ? INDEX_CODES : [selected])) {
      try {
        await acquireIndexHistory(code, startDate || undefined, endDate || undefined);
        success += 1;
      } catch (reason) {
        failures.push(code + ": " + String(reason));
        // A busy or unavailable backend affects the entire batch; stop instead
        // of sending four more requests that will all fail with HTTP 409.
        if (!all || /HTTP 409|timed out|backend.*(unreachable|unavailable)/i.test(String(reason))) break;
      }
    }
    try {
      await reloadCatalog();
    } catch (reason) {
      failures.push(String(reason));
    }
    if (success) notify(t("Index history download completed."), "success");
    if (failures.length) {
      setError(failures.join(" · "));
      notify(t("Some index history downloads failed."), "error");
    }
    setLoading(false);
  };

  const selectedItem = catalog.find(item => item.code === selected);
  const observations = history?.observations ?? [];
  const last = observations.at(-1);
  const previous = observations.at(-2);
  const percent = last && previous
    ? (Number(last.close) / Number(previous.close) - 1) * 100
    : null;

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle><span className="flex items-center gap-2"><Activity className="size-4" aria-hidden="true" />{t("Index history acquisition")}</span></CardTitle>
        <CardDescription>{t("Fetch and store real daily closing levels for major U.S. indexes, separately from stock OHLCV and index constituents.")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid min-w-0 gap-2 sm:grid-cols-[120px_minmax(0,1fr)] sm:items-center">
          <label htmlFor="index-history-select" className="text-xs font-medium">{t("Market index")}</label>
          <select id="index-history-select" aria-label={t("Market index")} value={selected} disabled={loading}
            onChange={event => setSelected(event.target.value as IndexCode)}
            className="h-9 w-full min-w-0 rounded-md border border-input bg-background px-2 text-sm text-foreground">
            {INDEX_CODES.map(code => (
              <option key={code} value={code}>{catalog.find(item => item.code === code)?.name ?? code}</option>
            ))}
          </select>
          <div className="flex flex-wrap justify-end gap-2 sm:col-start-2">
            <Button size="sm" variant="outline" type="button" disabled={loading} onClick={() => void reloadCatalog().catch(reason => setError(String(reason)))}>
              <RefreshCw aria-hidden="true" />{t("Refresh")}
            </Button>
            <Button size="sm" variant="secondary" type="button" disabled={loading} onClick={() => void download(false)}>
              <Download aria-hidden="true" />{t("Fetch selected index")}
            </Button>
            <Button size="sm" type="button" disabled={loading} onClick={() => void download(true)}>
              <Download aria-hidden="true" />{t("Fetch all five indexes")}
            </Button>
          </div>
        </div>
        <div className="grid gap-2 sm:grid-cols-2">
          <label className="grid gap-1 text-xs font-medium">
            {t("Backfill start")}
            <input
              aria-label={t("Backfill start")}
              type="date"
              value={startDate}
              disabled={loading}
              onChange={event => setStartDate(event.target.value)}
              className="h-9 rounded-md border border-input bg-background px-2 text-sm font-normal text-foreground"
            />
          </label>
          <label className="grid gap-1 text-xs font-medium">
            {t("Backfill end")}
            <input
              aria-label={t("Backfill end")}
              type="date"
              value={endDate}
              disabled={loading}
              onChange={event => setEndDate(event.target.value)}
              className="h-9 rounded-md border border-input bg-background px-2 text-sm font-normal text-foreground"
            />
          </label>
        </div>
        <div className="grid gap-2 sm:grid-cols-3">
          <div className="rounded-md border border-border p-3">
            <p className="text-xs text-muted-foreground">{t("Stored sessions")}</p>
            <p className="text-lg font-semibold tabular-nums">{selectedItem?.sessions.toLocaleString() ?? "—"}</p>
          </div>
          <div className="rounded-md border border-border p-3">
            <p className="text-xs text-muted-foreground">{t("Latest close")}</p>
            <p className="text-lg font-semibold tabular-nums">{last ? display(last.close) : "—"}</p>
          </div>
          <div className="rounded-md border border-border p-3">
            <p className="text-xs text-muted-foreground">{t("Session change")}</p>
            <p className="text-lg font-semibold tabular-nums">
              {percent === null ? "—" : (percent >= 0 ? "+" : "") + percent.toFixed(2) + "%"}
            </p>
          </div>
        </div>
        <p className="text-xs text-muted-foreground">
          {t("Local coverage")}: {selectedItem?.local_first_session ?? "—"} – {selectedItem?.local_last_session ?? "—"}
          {" · "}{t("Last available")}: {selectedItem?.local_last_session ?? "—"}
        </p>
        <p className="text-xs text-muted-foreground">
          {t("Provider availability")}: {selectedItem?.provider_earliest_available ?? t("Unknown")}
          {" – "}{selectedItem?.provider_latest_available ?? t("Unknown")}
        </p>
        <p className="text-xs text-muted-foreground">
          {selectedItem
            ? `${t("Provider")}: ${selectedItem.provider} · ${t("Data type")}: ${selectedItem.series_type === "price" ? t("Price") : t("Volatility")} · ${t("Frequency")}: ${selectedItem.frequency} · ${t("Units")}: ${selectedItem.units}`
            : t("Loading index capability metadata…")}
        </p>
        <p className="text-xs text-muted-foreground">
          {selectedItem?.known_history_limit ?? t("Provider coverage is reported after a successful import.")}
        </p>
        {loading && <p role="status" className="text-xs text-muted-foreground">{t("Fetching index history…")}</p>}
        {reading && <p role="status" className="text-xs text-muted-foreground">{t("Reading locally stored index history…")}</p>}
        {detailsError && <p role="alert" className="text-xs text-destructive">{detailsError}</p>}
        {error && <p role="alert" className="text-xs text-destructive break-words">{error}</p>}
      </CardContent>
    </Card>
  );
}
