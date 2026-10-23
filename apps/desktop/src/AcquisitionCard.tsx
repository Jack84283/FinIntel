import { useEffect, useState, type FormEvent } from "react";
import { ArrowDownToLine, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { acquireMarketDaily, fetchAcquisitionStatus, type AcquisitionResult, type AcquisitionStatus, type OutputSize, type AcquisitionProvider, type AlpacaPeriod } from "./market-acquisition-api";
import { SymbolSelector } from "./SymbolSelector";
import type { AssetType } from "./symbol-catalog-api";

export function AcquisitionCard({
  selectedSymbol,
  onAcquired,
}: {
  selectedSymbol: string;
  onAcquired: (symbol: string, provider: AcquisitionProvider) => void;
}) {
  const [symbol, setSymbol] = useState(selectedSymbol);
  const [selectedType, setSelectedType] = useState<AssetType | null>(null);
  const [outputsize, setOutputsize] = useState<OutputSize>("compact");
  const [provider, setProvider] = useState<AcquisitionProvider>("alpaca_sip");
  const [alpacaPeriod, setAlpacaPeriod] = useState<AlpacaPeriod>("10y");
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

  const configured = provider === "alpaca_sip" ? Boolean(status?.alpaca_configured) : Boolean(status?.key_configured);

  const acquire = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (pending || selectedType === "Index" || selectedType === "Mutual Fund") return;
    setResult(null);
    setError("");
    setPending(true);
    try {
      const imported = await acquireMarketDaily(symbol, outputsize, provider, alpacaPeriod);
      setResult(imported);
      onAcquired(imported.symbol, provider);
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
          <Badge variant={configured ? "success" : "outline"}>
            {status === null ? "Checking provider" : configured ? "Provider configured" : "API key needed"}
          </Badge>
        </CardAction>
        <CardDescription className="col-span-2">
          Explicitly fetch daily unadjusted prices from Alpha Vantage and save them directly to local SQLite.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        <form onSubmit={(event) => void acquire(event)} className="flex flex-wrap items-end gap-2">
          <label className="grid min-w-[180px] flex-1 gap-1 text-xs text-muted-foreground">
            Data source
            <select
              aria-label="Historical market data provider"
              value={provider}
              onChange={(event) => setProvider(event.target.value as AcquisitionProvider)}
              disabled={pending}
              className="h-9 rounded-md border border-input bg-muted px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <option value="alpaca_sip">Alpaca · SIP daily (free delayed)</option>
              <option value="alpha_vantage">Alpha Vantage · daily</option>
            </select>
          </label>
          <SymbolSelector
            value={symbol}
            onChange={(ticker, type) => { setSymbol(ticker); setSelectedType(type); }}
            keyConfigured={Boolean(status?.key_configured)}
            disabled={pending}
          />
          <label className="grid min-w-[165px] flex-1 gap-1 text-xs text-muted-foreground">
            Download history
            {provider === "alpaca_sip" ? (
              <select
                aria-label="Alpaca history length"
                value={alpacaPeriod}
                onChange={(event) => setAlpacaPeriod(event.target.value as AlpacaPeriod)}
                disabled={pending}
                className="h-9 rounded-md border border-input bg-muted px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <option value="5y">Last 5 years</option>
                <option value="10y">Last 10 years</option>
                <option value="max">Available since 2016</option>
              </select>
            ) : (
              <select
                aria-label="Alpha Vantage history size"
                value={outputsize}
                onChange={(event) => setOutputsize(event.target.value as OutputSize)}
                disabled={pending}
                className="h-9 rounded-md border border-input bg-muted px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <option value="compact">Latest 100 sessions (free)</option>
                <option value="full">Full history (premium)</option>
              </select>
            )}
          </label>
          <Button type="submit" disabled={pending || !configured || !symbol.trim() || selectedType === "Index" || selectedType === "Mutual Fund"} size="sm">
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
        {configured && (
          <p className="text-xs text-muted-foreground">
            {provider === "alpaca_sip"
              ? "Alpaca requests SIP daily OHLCV up to yesterday; free Basic accounts may access delayed historical SIP data. Access depends on your Alpaca account and feed entitlement. Each click calls the API."
              : "Alpha Vantage's free key returns only 100 sessions; full history requires premium access."}
            {" "}Raw prices are not split/dividend-adjusted. Provider quotas and licensing apply.
          </p>
        )}
        {status && !configured && (
          <p role="status" className="text-xs text-muted-foreground">
            {provider === "alpaca_sip"
              ? "Configure FININTEL_ALPACA_API_KEY_ID and FININTEL_ALPACA_API_SECRET_KEY in the Python backend environment."
              : "Configure FININTEL_ALPHA_VANTAGE_API_KEY in the Python backend environment."}
            {" "}Restart the backend and reload this page. Credentials never enter the UI.
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
