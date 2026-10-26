import { useLocale } from "./localization";
import { useEffect, useId, useState } from "react";
import { Check, RefreshCw, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { searchAcquirableSymbols, syncSymbols, type AssetFilter, type AssetType, type CatalogSymbol } from "./symbol-catalog-api";

interface SymbolSelectorProps {
  value: string;
  onChange: (next: string, type: AssetType | null) => void;
  keyConfigured: boolean;
  disabled: boolean;
}

const TICKER_PATTERN = /^[A-Z0-9][A-Z0-9.-]{0,14}$/;
const MAX_SUGGESTIONS = 8;

/** Debounced local-only typeahead; never loads the complete symbol directory. */
export function SymbolSelector({ value, onChange, keyConfigured, disabled }: SymbolSelectorProps) {
  const { t } = useLocale();
  const [query, setQuery] = useState(value);
  const [kind, setKind] = useState<AssetFilter>("All");
  const [results, setResults] = useState<CatalogSymbol[]>([]);
  const [catalogSource, setCatalogSource] = useState<"starter" | "alpha_vantage">("starter");
  const [catalogTotal, setCatalogTotal] = useState(0);
  const [error, setError] = useState("");
  const [syncError, setSyncError] = useState("");
  const [syncing, setSyncing] = useState(false);
  const [notice, setNotice] = useState("");
  const [revision, setRevision] = useState(0);
  const [focused, setFocused] = useState(false);
  const [active, setActive] = useState(-1);
  const [searching, setSearching] = useState(false);
  const listId = useId();

  useEffect(() => {
    setQuery(value);
  }, [value]);

  useEffect(() => {
    let ignore = false;
    const trimmed = query.trim();
    if (!trimmed) {
      setResults([]);
      setSearching(false);
      return;
    }
    setSearching(true);
    const timer = window.setTimeout(() => {
      searchAcquirableSymbols(trimmed, kind as "All" | "Stock" | "ETF", 20)
        .then((catalog) => {
          if (ignore) return;
          setResults(catalog.results.slice(0, MAX_SUGGESTIONS));
          setCatalogSource(catalog.source);
          setCatalogTotal(catalog.total);
          setError("");
          setSearching(false);
          setActive(-1);
        })
        .catch((reason: unknown) => {
          if (ignore) return;
          setResults([]);
          setSearching(false);
          setError(reason instanceof Error ? reason.message : "Local symbol search unavailable");
        });
    }, 180);
    return () => {
      ignore = true;
      window.clearTimeout(timer);
    };
  }, [query, kind, revision]);

  const choose = (item: CatalogSymbol) => {
    onChange(item.symbol, item.asset_type);
    setQuery(item.symbol);
    setFocused(false);
    setActive(-1);
  };

  const changeQuery = (next: string) => {
    setQuery(next);
    setActive(-1);
    const ticker = next.trim().toUpperCase();
    // Manual ticker entry remains possible even when the catalog has no match.
    // Names/partial free-text queries must never become invalid API symbols.
    onChange(TICKER_PATTERN.test(ticker) ? ticker : "", null);
  };

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

  const showSuggestions = focused && query.trim().length > 0 && !disabled;
  return (
    <div className="min-w-0 w-full space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <label htmlFor={listId + "-input"} className="text-xs font-medium text-muted-foreground">
          Instrument
        </label>
        <span className="text-[11px] text-muted-foreground">
          {catalogSource === "starter" ? t("Starter directory") : t("Local provider directory")}
          {catalogTotal > 0 ? " · " + catalogTotal.toLocaleString() + " symbols" : ""}
        </span>
      </div>
      <div className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <div className="relative min-w-0">
          <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-3 size-4 text-muted-foreground" />
          <input
            id={listId + "-input"}
            role="combobox"
            aria-autocomplete="list"
            aria-controls={listId + "-list"}
            aria-expanded={showSuggestions}
            aria-activedescendant={showSuggestions && active >= 0 ? listId + "-option-" + active : undefined}
            type="search"
            placeholder={t("Search ticker or company, e.g. AAPL or Apple")}
            autoComplete="off"
            spellCheck={false}
            className="h-10 w-full min-w-0 rounded-md border border-input bg-muted pl-9 pr-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            value={query}
            maxLength={80}
            onChange={(event) => changeQuery(event.target.value)}
            onFocus={() => setFocused(true)}
            onBlur={() => { setFocused(false); setActive(-1); }}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                setFocused(false);
                setActive(-1);
                event.currentTarget.blur();
              } else if (showSuggestions && event.key === "ArrowDown") {
                event.preventDefault();
                setActive((index) => Math.min(index + 1, results.length - 1));
              } else if (showSuggestions && event.key === "ArrowUp") {
                event.preventDefault();
                setActive((index) => Math.max(index - 1, 0));
              } else if (event.key === "Enter") {
                // Enter selects an autocomplete option; it must not submit
                // the outer acquisition form or unexpectedly call a provider.
                event.preventDefault();
                if (showSuggestions && active >= 0 && results[active]) {
                  choose(results[active]);
                }
              }
            }}
            disabled={disabled || syncing}
          />
          {showSuggestions && (
            <div
              id={listId + "-list"}
              role="listbox"
              aria-label={t("Matching instruments")}
              className="absolute inset-x-0 top-full z-30 mt-1 max-h-72 overflow-y-auto rounded-md border border-border bg-card p-1 shadow-lg"
            >
              {searching ? (
                <p className="px-3 py-2 text-xs text-muted-foreground">{t("Searching local instruments…")}</p>
              ) : results.length ? (
                results.map((item, index) => (
                  <button
                    key={item.symbol}
                    id={listId + "-option-" + index}
                    type="button"
                    role="option"
                    aria-selected={active === index}
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => choose(item)}
                    className={`flex w-full min-w-0 items-center gap-3 rounded px-3 py-2 text-left text-sm hover:bg-accent focus-visible:bg-accent ${active === index ? "bg-accent" : ""}`}
                  >
                    <span className="w-20 shrink-0 font-semibold">{item.symbol}</span>
                    <span className="min-w-0 flex-1 truncate text-muted-foreground">{item.name}</span>
                    <span className="shrink-0 text-[11px] text-muted-foreground">{item.asset_type}</span>
                    {value === item.symbol && <Check aria-hidden="true" className="size-4 shrink-0" />}
                  </button>
                ))
              ) : (
                <p className="px-3 py-2 text-xs text-muted-foreground">
                  {t("No catalog match. You can still enter a valid ticker manually.")}
                </p>
              )}
            </div>
          )}
        </div>
        <select
          aria-label={t("Filter by asset type")}
          value={kind}
          onChange={(event) => setKind(event.target.value as AssetFilter)}
          className="h-10 w-full min-w-0 rounded-md border border-input bg-muted px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          disabled={disabled || syncing}
        >
          <option value="All">{t("All stocks and ETFs")}</option>
          <option value="Stock">{t("Stocks")}</option>
          <option value="ETF">{t("ETFs")}</option>
        </select>
      </div>
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-3">
        <p className="text-xs leading-5 text-muted-foreground">
          {t("Type a symbol or company name to see suggestions. Select a match, or enter an exact ticker.")}
        </p>
        <Button type="button" size="sm" variant="secondary" onClick={() => void performSync()} disabled={disabled || syncing || !keyConfigured}>
          <RefreshCw aria-hidden="true" className={syncing ? "animate-spin" : ""} />
          {syncing ? t("Syncing…") : t("Sync symbols")}
        </Button>
      </div>
      {catalogSource === "starter" && (
        <p className="text-[11px] text-muted-foreground">{t("Sync adds active US stocks and ETFs. Index and mutual fund entries require separate price adapters.")}</p>
      )}
      {notice && <p role="status" className="text-xs text-positive">{notice}</p>}
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      {syncError && <p role="alert" className="text-xs text-destructive">{t("Symbol sync failed:")} {syncError}. {t("Existing directory retained.")}</p>}
      {!keyConfigured && <p className="text-[11px] text-muted-foreground">{t("Sync requires an Alpha Vantage backend key. Existing local symbols remain searchable.")}</p>}
    </div>
  );
}
