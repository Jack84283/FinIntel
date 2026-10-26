import { useLocale } from "./localization";
import { useEffect, useState, type FormEvent } from "react";
import { ArrowDownToLine, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { acquireMarketDaily, fetchAcquisitionStatus, type AcquisitionResult, type AcquisitionStatus, type OutputSize, type AcquisitionProvider, type AlpacaPeriod } from "./market-acquisition-api";
import { SymbolSelector } from "./SymbolSelector";
import type { AssetType } from "./symbol-catalog-api";
import { acquisitionBlockReason, preferredAcquisitionProvider } from "./acquisition-ui-state";

export function AcquisitionCard({
  selectedSymbol,
  onAcquired,
}: {
  selectedSymbol: string;
  onAcquired: (symbol: string, provider: AcquisitionProvider) => void;
}) {
  const { t } = useLocale();
  const [symbol, setSymbol] = useState(selectedSymbol);
  const [selectedType, setSelectedType] = useState<AssetType | null>(null);
  const [outputsize, setOutputsize] = useState<OutputSize>("compact");
  const [provider, setProvider] = useState<AcquisitionProvider>("alpaca_sip");
  const [providerTouched, setProviderTouched] = useState(false);
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
        setStatusError(err instanceof Error ? err.message : t("Local backend unavailable"));
      });
    return () => { cancelled = true; };
  }, [statusTick]);

  // Choose a configured provider on first successful status check, but do
  // not override the user's deliberate source selection.
  useEffect(() => {
    if (status && !providerTouched) {
      setProvider(preferredAcquisitionProvider(status));
    }
  }, [status, providerTouched]);

  const configured = provider === "tiingo" ? Boolean(status?.tiingo_configured) : provider === "alpaca_sip" ? Boolean(status?.alpaca_configured) : Boolean(status?.key_configured);
  const blockReason = acquisitionBlockReason({
    symbol, selectedType, provider, status, statusError,
  });

  const recheckStatus = () => {
    setStatus(null);
    setStatusError("");
    setStatusTick((value) => value + 1);
  };

  const acquire = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (pending) return;
    setResult(null);
    setError("");
    // Keep the button clickable: explain missing credentials, ticker or
    // backend availability, but do not call any provider in these cases.
    if (blockReason) {
      setError(blockReason);
      return;
    }
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
    <Card aria-labelledby="acquisition-title" className="min-w-0">
      <CardHeader className="min-w-0 gap-y-3 sm:px-6">
        <div className="col-span-2 flex min-w-0 flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 space-y-1">
            <CardTitle><span id="acquisition-title" className="flex items-center gap-2"><ArrowDownToLine aria-hidden="true" className="size-4 shrink-0" /> {t("Manual historical download")}</span></CardTitle>
            <CardDescription>{t("Choose an instrument, set the data source and history window, then fetch validated daily prices.")}</CardDescription>
          </div>
          <Badge variant={configured ? "success" : "outline"}>
            {statusError ? t("Backend unavailable") : status === null ? t("Checking provider") : configured ? t("Provider configured") : t("API key needed")}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="min-w-0 space-y-3">
        <form onSubmit={(event) => void acquire(event)} className="min-w-0 space-y-4">
          <div className="min-w-0 rounded-lg border border-border bg-background/40 p-3 sm:p-4">
            <div className="mb-3 space-y-1">
              <p className="text-sm font-semibold">{t("1. Select instrument")}</p>
              <p className="text-xs leading-5 text-muted-foreground">{t("Search your local catalog, filter by type, or enter a ticker manually.")}</p>
            </div>
            <SymbolSelector
              value={symbol}
              onChange={(ticker, type) => {
                setSymbol(ticker);
                setSelectedType(type);
                setError("");
                setResult(null);
              }}
              keyConfigured={Boolean(status?.key_configured)}
              disabled={pending}
            />
          </div>
          <div className="min-w-0 rounded-lg border border-border bg-background/40 p-4 sm:p-5">
            <div className="mb-4 space-y-1">
              <p className="text-sm font-semibold">{t("2. Configure download")}</p>
              <p className="text-xs leading-5 text-muted-foreground">{t("Select your provider and how much history to retrieve.")}</p>
            </div>
            <div className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="grid min-w-0 gap-2 text-xs text-muted-foreground">
            Data source
            <select
              aria-label={t("Historical market data provider")}
              value={provider}
              onChange={(event) => {
                setProvider(event.target.value as AcquisitionProvider);
                setProviderTouched(true);
                setError("");
                setResult(null);
              }}
              disabled={pending}
              className="h-10 w-full min-w-0 rounded-md border border-input bg-muted px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <option value="tiingo">{t("Tiingo · long-term daily history")}</option>
              <option value="alpaca_sip">{t("Alpaca · SIP daily (free delayed)")}</option>
              <option value="alpha_vantage">{t("Alpha Vantage · daily")}</option>
            </select>
          </label>
          <label className="grid min-w-0 gap-2 text-xs text-muted-foreground">
            Download history
            {provider !== "alpha_vantage" ? (
              <select
                aria-label={t("Historical data length")}
                value={alpacaPeriod}
                onChange={(event) => setAlpacaPeriod(event.target.value as AlpacaPeriod)}
                disabled={pending}
                className="h-10 w-full min-w-0 rounded-md border border-input bg-muted px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <option value="5y">{t("Last 5 years")}</option>
                <option value="10y">{t("Last 10 years")}</option>
                <option value="max">{t("Maximum available history")}</option>
              </select>
            ) : (
              <select
                aria-label={t("Alpha Vantage history size")}
                value={outputsize}
                onChange={(event) => setOutputsize(event.target.value as OutputSize)}
                disabled={pending}
                className="h-9 rounded-md border border-input bg-muted px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <option value="compact">{t("Latest 100 sessions (free)")}</option>
                <option value="full">{t("Full history (premium)")}</option>
              </select>
            )}
          </label>
            </div>
          </div>
          <div className="flex min-w-0 flex-wrap items-center justify-end gap-2 border-t border-border pt-3">
          <Button type="submit" disabled={pending} size="sm">
            {pending ? <RefreshCw aria-hidden="true" className="animate-spin" /> : <ArrowDownToLine aria-hidden="true" />}
            {pending ? t("Fetching…") : "Fetch & store"}
          </Button>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={recheckStatus}
            disabled={pending || status === null && !statusError}
          >
            <RefreshCw aria-hidden="true" />
            Recheck provider
          </Button>
          </div>
        </form>
        {(selectedType === "Index" || selectedType === "Mutual Fund") && (
          <p role="status" className="text-xs text-muted-foreground">
            {selectedType} instruments are reference entries only. Select a Stock or ETF to fetch daily prices from {provider === "tiingo" ? "Tiingo" : provider === "alpaca_sip" ? "Alpaca" : "Alpha Vantage"}.
          </p>
        )}
        {!symbol.trim() && (
          <p role="status" className="text-xs text-muted-foreground">
            Select a stock or ETF symbol above, or enter a ticker manually.
          </p>
        )}
        {configured && (
          <p className="text-xs text-muted-foreground">
            {provider === "tiingo"
              ? t("Tiingo requests raw daily OHLCV over the selected period. Coverage and access depend on your account and the instrument.")
              : provider === "alpaca_sip"
              ? t("Alpaca requests SIP daily OHLCV up to yesterday; free Basic accounts may access delayed historical SIP data. Access depends on your Alpaca account and feed entitlement. Each click calls the API.")
              : "Alpha Vantage's free key returns only 100 sessions; full history requires premium access."}
            {" "}{t("Raw prices are not split/dividend-adjusted. Provider quotas and licensing apply.")}
          </p>
        )}
        {status && !configured && (
          <p role="status" className="text-xs text-muted-foreground">
            {provider === "tiingo"
              ? t("To use Tiingo, set FININTEL_TIINGO_API_KEY in the Python backend environment.")
              : provider === "alpaca_sip"
              ? t("To use Alpaca, set FININTEL_ALPACA_API_KEY_ID and FININTEL_ALPACA_API_SECRET_KEY in the Python backend environment.")
              : t("To use Alpha Vantage, set FININTEL_ALPHA_VANTAGE_API_KEY in the Python backend environment.")}
            {" "}{t("Restart the backend, then select Recheck provider. Credentials never enter the UI.")}
            {provider === "alpaca_sip" && status.key_configured
              ? t(" Alpha Vantage is already configured; select it above to fetch now.")
              : provider === "alpha_vantage" && status.alpaca_configured
                ? t(" Alpaca is already configured; select it above to fetch now.")
                : ""}
          </p>
        )}
        {statusError && <p role="alert" className="text-xs text-destructive">{statusError}</p>}
        {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
        {result && (
          <p role="status" aria-live="polite" className="text-xs text-positive">
            {t("Stored")} {result.symbol}: {result.observations} {t("observations")} ({result.inserted} {t("new")}, {result.updated} {t("revised")}, {result.unchanged} {t("unchanged")}, {result.stale} {t("older")}). {t("Through")} {result.last_session}. {t("Local charts refreshed.")}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
