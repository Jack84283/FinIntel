import { useEffect, useState, type FormEvent } from "react";
import { ArrowDownToLine, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { acquireMarketDaily, fetchAcquisitionStatus, type AcquisitionResult, type AcquisitionStatus, type OutputSize } from "./market-acquisition-api";
import { SymbolSelector } from "./SymbolSelector";
import type { AssetType } from "./symbol-catalog-api";

export function AcquisitionCard({
  selectedSymbol,
  onAcquired,
}: {
  selectedSymbol: string;
  onAcquired: (symbol: string) => void;
}) {
  const [symbol, setSymbol] = useState(selectedSymbol);
  const [selectedType, setSelectedType] = useState<AssetType | null>(null);
  const [outputsize, setOutputsize] = useState<OutputSize>("compact");
  const [status, setStatus] = useState<AcquisitionStatus | null>(null);
  const [statusError, setStatusError] = useState("");
  const [pending, setPending] = useState(false);
  const [result, setResult] = useState<AcquisitionResult | null>(null);
  const [error, setError] = useState("");
  const [statusTick, setStatusTick] = useState(0);

  useEffect(() => {
    if (selectedSymbol) {
      setSymbol(selectedSymbol);
      setSelectedType(null);
    }
  }, [selectedSymbol]);

  useEffect(() => {
    let cancelled = false;
    fetchAcquisitionStatus()
      .then((value) => {
        if (cancelled) return;
        setStatus(value);
        setStatusError("");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setStatus(null);
        setStatusError(err instanceof Error ? err.message : "Local backend unavailable");
      });
    return () => { cancelled = true; };
  }, [statusTick]);

  const acquire = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (pending || selectedType === "Index" || selectedType === "Mutual Fund") return;
    setResult(null);
    setError("");
    setPending(true);
    try {
      const imported = await acquireMarketDaily(symbol, outputsize);
      setResult(imported);
      onAcquired(imported.symbol);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(false);
    }
  };

  return (
    <Card aria-labelledby="acquisition-title">
      <CardHeader>
        <CardTitle><span id="acquisition-title" className="flex items-center gap-2"><ArrowDownToLine aria-hidden="true" className="size-4" /> Data acquisition</span></CardTitle>
        <CardAction>
          <Badge variant={status?.key_configured ? "success" : "outline"}>
            {status === null ? "Checking provider" : status.key_configured ? "Provider configured" : "API key needed"}
          </Badge>
        </CardAction>
        <CardDescription className="col-span-2">
          Explicitly fetch daily unadjusted prices from Alpha Vantage and save them directly to local SQLite.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        <form onSubmit={(event) => void acquire(event)} className="flex flex-wrap items-end gap-2">
          <SymbolSelector
            value={symbol}
            onChange={(ticker, type) => { setSymbol(ticker); setSelectedType(type); }}
            keyConfigured={Boolean(status?.key_configured)}
            disabled={pending}
          />
          <label className="grid min-w-[165px] flex-1 gap-1 text-xs text-muted-foreground">
            History
            <select
              aria-label="Acquisition history size"
              value={outputsize}
              onChange={(event) => setOutputsize(event.target.value as OutputSize)}
              className="h-9 rounded-md border border-input bg-muted px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <option value="compact">Latest 100 sessions (free)</option>
              <option value="full">Full 25+ years, if available (premium)</option>
            </select>
          </label>
          <Button type="submit" disabled={pending || !status?.key_configured || !symbol.trim() || selectedType === "Index" || selectedType === "Mutual Fund"} size="sm">
            {pending ? <RefreshCw aria-hidden="true" className="animate-spin" /> : <ArrowDownToLine aria-hidden="true" />}
            {pending ? "Fetching…" : "Fetch & store"}
          </Button>
          {statusError && <Button type="button" variant="secondary" size="sm" onClick={() => setStatusTick((value) => value + 1)}>Retry connection</Button>}
        </form>
        {(selectedType === "Index" || selectedType === "Mutual Fund") && (
          <p role="status" className="text-xs text-muted-foreground">
            {selectedType} instruments are saved in the local catalog, but this Alpha Vantage stock/ETF daily-price integration cannot fetch their history yet.
          </p>
        )}
        {status?.key_configured && (
          <p className="text-xs text-muted-foreground">Alpha Vantage offers 100 daily records on free keys or full history on premium keys. Choose 5 or 10 years in the Explorer after full data has been stored. One click may retry transient errors; provider quotas and license apply.</p>
        )}
        {status && !status.key_configured && (
          <p role="status" className="text-xs text-muted-foreground">
            Set <code>FININTEL_ALPHA_VANTAGE_API_KEY</code> in the Python backend environment, restart the backend, then reload the app. Your API key is never entered here.
          </p>
        )}
        {statusError && <p role="alert" className="text-xs text-destructive">{statusError}</p>}
        {error && <p role="alert" className="text-xs text-destructive">Acquisition failed: {error}. Existing stored data was not replaced.</p>}
        {result && (
          <p role="status" aria-live="polite" className="text-xs text-positive">
            Stored {result.symbol}: {result.observations} observations ({result.inserted} new, {result.updated} revised, {result.unchanged} unchanged, {result.stale} older). Through {result.last_session}. Local charts refreshed.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
