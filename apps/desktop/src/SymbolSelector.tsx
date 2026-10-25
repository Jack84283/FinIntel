import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { searchAcquirableSymbols, syncSymbols, type AssetFilter, type AssetType, type CatalogSymbol } from "./symbol-catalog-api";

interface SymbolSelectorProps {
  value: string;
  onChange: (next: string, type: AssetType | null) => void;
  keyConfigured: boolean;
  disabled: boolean;
}

/** Search a local catalog, not remote markets; selection never downloads prices. */
export function SymbolSelector({ value, onChange, keyConfigured, disabled }: SymbolSelectorProps) {
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<AssetFilter>("All");
  const [results, setResults] = useState<CatalogSymbol[]>([]);
  const [catalogSource, setCatalogSource] = useState<"starter" | "alpha_vantage">("starter");
  const [catalogTotal, setCatalogTotal] = useState(0);
  const [error, setError] = useState("");
  const [syncError, setSyncError] = useState("");
  const [syncing, setSyncing] = useState(false);
  const [notice, setNotice] = useState("");
  const [revision, setRevision] = useState(0);
  const [customTicker, setCustomTicker] = useState(false);

  useEffect(() => {
    let ignore = false;
    const timer = window.setTimeout(() => {
      searchAcquirableSymbols(query, kind as "All" | "Stock" | "ETF")
        .then((catalog) => {
          if (ignore) return;
          setResults(catalog.results);
          setCatalogSource(catalog.source);
          setCatalogTotal(catalog.total);
          setError("");
        })
        .catch((reason: unknown) => {
          if (ignore) return;
          setError(reason instanceof Error ? reason.message : "Local symbol search unavailable");
        });
    }, 160);
    return () => { ignore = true; window.clearTimeout(timer); };
  }, [query, kind, revision]);

  const performSync = async () => {
    if (syncing || disabled || !keyConfigured) return;
    setSyncing(true);
    setSyncError("");
    setNotice("");
    try {
      const updated = await syncSymbols();
      setNotice(updated.total.toLocaleString() + " active stock/ETF symbols saved locally.");
      setRevision((old) => old + 1);
    } catch (reason: unknown) {
      setSyncError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSyncing(false);
    }
  };

  const selectedInResults = results.some((entry) => entry.symbol === value);
  return (
    <div className="min-w-0 w-full space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs font-medium text-muted-foreground">Stock or ETF (supported by the chosen API)</span>
        <span className="text-[11px] text-muted-foreground">
          {catalogSource === "starter" ? "Starter directory" : "Provider directory"} · {catalogTotal.toLocaleString()} symbols
        </span>
      </div>
      <div className="grid min-w-0 gap-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <input
          aria-label="Search instruments by symbol or name"
          type="search"
          placeholder="Search stocks and ETFs"
          className="h-10 w-full min-w-0 rounded-md border border-input bg-muted px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          value={query}
          maxLength={80}
          onChange={(event) => setQuery(event.target.value)}
          disabled={disabled || syncing}
        />
        <select
          aria-label="Filter by asset type"
          value={kind}
          onChange={(event) => setKind(event.target.value as AssetFilter)}
          className="h-10 w-full min-w-0 rounded-md border border-input bg-muted px-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          disabled={disabled || syncing}
        >
          <option value="All">All</option>
          <option value="Stock">Stocks</option>
          <option value="ETF">ETFs</option>
        </select>
      </div>
      {customTicker ? (
        <input
          aria-label="Custom ticker symbol"
          type="text"
          value={value}
          onChange={(event) => onChange(event.target.value.toUpperCase(), null)}
          placeholder="Enter ticker"
          maxLength={15}
          autoComplete="off"
          spellCheck={false}
          disabled={disabled || syncing}
          className="h-9 w-full min-w-0 rounded-md border-input bg-muted px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      ) : (
        <select
          aria-label="Select a stock or ETF for historical prices"
          value={value}
          onChange={(event) => {
            const ticker = event.target.value;
            const chosen = results.find((entry) => entry.symbol === ticker);
            onChange(ticker, chosen?.asset_type ?? null);
          }}
          disabled={disabled || syncing}
          className="h-9 w-full min-w-0 rounded-md border border-input bg-muted px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <option value="">Select an instrument…</option>
          {value && !selectedInResults && <option value={value}>{value} · Selected</option>}
          {results.map((entry) => (
            <option key={entry.symbol} value={entry.symbol}>
              {entry.symbol} — {entry.name} ({entry.asset_type})
            </option>
          ))}
        </select>
      )}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <button
          type="button"
          className="text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          onClick={() => setCustomTicker((old) => !old)}
        >
          {customTicker ? "Choose from catalog" : "Ticker not listed? Enter manually"}
        </button>
        <Button type="button" size="sm" variant="secondary" onClick={() => void performSync()} disabled={disabled || syncing || !keyConfigured}>
          <RefreshCw aria-hidden="true" className={syncing ? "animate-spin" : ""} />
          {syncing ? "Syncing…" : "Sync symbols"}
        </Button>
      </div>
      {catalogSource === "starter" && (
        <p className="text-[11px] text-muted-foreground">Search and select Stock/ETF prices here. Index and mutual fund records stay in the master directory but require separate historical-price adapters. Sync adds active US stocks and ETFs.</p>
      )}
      {notice && <p role="status" className="text-xs text-positive">{notice}</p>}
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      {syncError && <p role="alert" className="text-xs text-destructive">Symbol sync failed: {syncError}. Existing directory retained.</p>}
      {!keyConfigured && <p className="text-[11px] text-muted-foreground">Sync requires an Alpha Vantage backend key. Offline starter stocks and ETFs remain selectable.</p>}
    </div>
  );
}
