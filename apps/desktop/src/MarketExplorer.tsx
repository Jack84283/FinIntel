import { PageHeader } from "./PageHeader";
import { useLocale } from "./localization";
import { useNotify } from "./notifications";
import { useEffect, useMemo, useState, useId, type FormEvent } from "react";
import { AlertCircle, BarChart3, Database, RefreshCw, Search, SlidersHorizontal } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { fetchMarketDaily, fetchMarketInstrumentDirectory } from "./market-api";
import { searchAcquirableSymbols } from "./symbol-catalog-api";
import type { MarketSelection } from "./DataAcquisitionPage";
import { MarketCharts } from "./market-chart";
import { TechnicalIndicatorsCard } from "./TechnicalIndicatorsCard";
import { MarketDatePicker } from "./components/MarketDatePicker";
import { yearsAgo, type HistoryPeriod } from "./history-range";
import { sortableBars, type DailySeries, type MarketInstrument } from "./market";

type LoadState = "loading" | "ready" | "error";
type SortColumn = "session_date" | "close" | "volume";

const pageSize = 25;
const formatInteger = (value: number) => new Intl.NumberFormat("en-US").format(value);
const price = (raw: string) => new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 }).format(Number(raw));

export function MarketExplorer({ initialSelection, onOpenAcquisition }: {
  initialSelection?: MarketSelection | null;
  onOpenAcquisition: () => void;
}) {
  const { t } = useLocale();
  const notify = useNotify();
  const [instruments, setInstruments] = useState<MarketInstrument[]>([]);
  const [indexState, setIndexState] = useState<LoadState>("loading");
  const [indexError, setIndexError] = useState("");
  const [symbol, setSymbol] = useState(initialSelection?.symbol ?? "");
  const [priceProvider, setPriceProvider] = useState<"alpha_vantage" | "alpaca_sip" | "tiingo">(initialSelection?.provider ?? "alpha_vantage");
  const [draftStart, setDraftStart] = useState("");
  const [draftEnd, setDraftEnd] = useState("");
  const [range, setRange] = useState({ start: "", end: "" });
  const [period, setPeriod] = useState<HistoryPeriod>("recent");
  const [validationError, setValidationError] = useState("");
  const [series, setSeries] = useState<DailySeries | null>(null);
  const [seriesState, setSeriesState] = useState<LoadState>("loading");
  const [seriesError, setSeriesError] = useState("");
  const [sortColumn, setSortColumn] = useState<SortColumn>("session_date");
  const [descending, setDescending] = useState(true);
  const [page, setPage] = useState(0);
  const [reload, setReload] = useState(0);
  const [showSearch, setShowSearch] = useState(false);
  const [instrumentNames, setInstrumentNames] = useState<Record<string, string>>({});
  const [instrumentQuery, setInstrumentQuery] = useState(initialSelection?.symbol ?? "");
  const [instrumentFocused, setInstrumentFocused] = useState(false);
  const [activeSuggestion, setActiveSuggestion] = useState(-1);
  const instrumentListId = useId();

  useEffect(() => {
    let ignore = false;
    setIndexState("loading");
    const fetchWithRetry = async () => {
      let lastError: unknown;
      for (let attempt = 0; attempt < 3; attempt += 1) {
        try {
          return await fetchMarketInstrumentDirectory();
        } catch (error: unknown) {
          lastError = error;
          if (attempt < 2) {
            await new Promise<void>((resolve) => window.setTimeout(resolve, 400 * (attempt + 1)));
          }
        }
      }
      throw lastError;
    };
    fetchWithRetry()
      .then((catalog) => {
        if (ignore) return;
        setInstruments(catalog);
        const matched = catalog.find((item) => item.symbol === symbol && item.provider === priceProvider)
          ?? catalog.find((item) => item.symbol === symbol)
          ?? catalog[0];
        setSymbol(matched?.symbol ?? "");
        setInstrumentQuery(matched?.symbol ?? "");
        setPriceProvider((matched?.provider ?? "alpha_vantage") as "alpha_vantage" | "alpaca_sip" | "tiingo");
        setIndexState("ready");
        setIndexError("");
      })
      .catch((error: unknown) => {
        if (ignore) return;
        setIndexState("error");
        setIndexError(error instanceof Error ? error.message : String(error));
        notify(t("Market instruments could not be loaded. See details below."), "error");
      });
    return () => { ignore = true; };
  }, [reload]);

  useEffect(() => {
    if (!symbol || indexState !== "ready") return;
    let ignore = false;
    setSeries(null);
    setSeriesState("loading");
    setPage(0);
    fetchMarketDaily({
      symbol,
      provider: priceProvider,
      startDate: range.start || undefined,
      endDate: range.end || undefined,
      limit: period === "recent" ? 100 : 4000,
    })
      .then((data) => {
        if (ignore) return;
        setSeries(data);
        setSeriesState("ready");
        setSeriesError("");
      })
      .catch((error: unknown) => {
        if (ignore) return;
        setSeriesState("error");
        setSeriesError(error instanceof Error ? error.message : String(error));
        notify(t("Market prices could not be loaded. See details below."), "error");
      });
    return () => { ignore = true; };
  }, [symbol, priceProvider, range, reload, indexState, period]);

  useEffect(() => {
    const query = instrumentQuery.trim();
    if (!query) return;
    let cancelled = false;
    const timeout = window.setTimeout(() => {
      searchAcquirableSymbols(query, "All", 40).then((result) => {
        if (cancelled) return;
        setInstrumentNames((previous) => {
          const next = { ...previous };
          result.results.forEach((item) => { next[item.symbol] = item.name; });
          return next;
        });
      }).catch(() => { /* A missing catalog must not hide locally stored prices. */ });
    }, 180);
    return () => { cancelled = true; window.clearTimeout(timeout); };
  }, [instrumentQuery]);

  const matchingInstruments = useMemo(() => {
    const q = instrumentQuery.trim().toUpperCase();
    if (!q) return [];
    return instruments
      .filter((item) => item.symbol.toUpperCase().includes(q) || item.provider.toUpperCase().includes(q) || (instrumentNames[item.symbol] ?? "").toUpperCase().includes(q))
      .sort((a, b) => Number(!a.symbol.toUpperCase().startsWith(q)) - Number(!b.symbol.toUpperCase().startsWith(q)) || a.symbol.localeCompare(b.symbol))
      .slice(0, 8);
  }, [instrumentQuery, instruments, instrumentNames]);
  const chooseInstrument = (item: MarketInstrument) => {
    setSymbol(item.symbol);
    setPriceProvider(item.provider as "alpha_vantage" | "alpaca_sip" | "tiingo");
    setInstrumentQuery(item.symbol);
    setInstrumentFocused(false);
    setActiveSuggestion(-1);
  };

  const bars = series?.observations ?? [];
  const sorted = useMemo(() => sortableBars(bars, sortColumn, descending), [bars, sortColumn, descending]);
  const pageCount = Math.max(1, Math.ceil(sorted.length / pageSize));
  const visibleRows = sorted.slice(page * pageSize, (page + 1) * pageSize);
  const last = bars[bars.length - 1];
  const first = bars[0];
  const prior = bars.length > 1 ? bars[bars.length - 2] : null;
  const closeChange = last && prior ? Number(last.close) - Number(prior.close) : null;

  const selectPeriod = (selected: HistoryPeriod) => {
    setPeriod(selected);
    setValidationError("");
    if (selected === "custom") return;
    const start = selected === "recent" ? "" : yearsAgo(
      selected === "1y" ? 1 : selected === "5y" ? 5 : 10,
    );
    setDraftStart(start);
    setDraftEnd("");
    setRange({ start, end: "" });
  };

  const applyRange = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (draftStart && draftEnd && draftStart > draftEnd) {
      setValidationError(t("Start date must not exceed end date."));
      notify(t("Correct the date range below."), "warning");
      return;
    }
    setValidationError("");
    setPeriod("custom");
    setRange({ start: draftStart, end: draftEnd });
  };

  const sortBy = (column: SortColumn) => {
    setPage(0);
    if (sortColumn === column) {
      setDescending((previous) => !previous);
    } else {
      setSortColumn(column);
      setDescending(true);
    }
  };

  const sortLabel = (column: SortColumn, label: string) =>
    t(label) + (sortColumn === column ? (descending ? " ↓" : " ↑") : "");

  return (
    <section className="space-y-2" aria-labelledby="markets-title">
      <PageHeader id="markets-title" breadcrumb={t("FinIntel / Markets")} title={t("Market Data Explorer")} subtitle={t("Historical daily prices stored in local SQLite. No live quotes.")} actions={<><Badge variant="outline">{t("Local SQLite")}</Badge>
            <Button type="button" variant="secondary" size="sm" onClick={() => setReload((v) => v + 1)}>
              <RefreshCw aria-hidden="true" /> {t("Reload local data")}
            </Button>
            <Button type="button" size="sm" onClick={onOpenAcquisition}>
              <Database aria-hidden="true" /> {t("Data Acquisition")}
            </Button>
            <Button type="button" variant={showSearch ? "secondary" : "outline"} size="sm" aria-expanded={showSearch} aria-controls="market-search-panel" onClick={() => setShowSearch((current) => !current)}>
              <SlidersHorizontal aria-hidden="true" /> {showSearch ? t("Hide filters") : t("Show filters")}
            </Button></>} />    {indexState === "loading" && (
        <Card><CardContent><Skeleton className="h-8 w-64" aria-label={t("Loading local instruments")} /></CardContent></Card>
      )}
      {indexState === "error" && (
        <Card role="alert"><CardHeader><CardTitle>{t("Local database unavailable")}</CardTitle></CardHeader>
          <CardContent className="space-y-3 text-sm text-muted-foreground">
            <p>{indexError}</p><p>{t("Start the Python service on 127.0.0.1:8765 and try again.")}</p>
            <Button onClick={() => setReload((v) => v + 1)} variant="secondary">{t("Retry")}</Button>
          </CardContent>
        </Card>
      )}
      {indexState === "ready" && instruments.length === 0 && (
        <Card>
          <CardHeader><CardTitle>{t("No stored market data yet")}</CardTitle>
            <CardDescription>{t("FinIntel reads your local SQLite database. Open Data Acquisition to fetch and store your first stock or ETF.")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex items-center gap-2 text-muted-foreground"><Database aria-hidden="true" className="size-5" /><span className="text-sm">{t("No price series have been imported.")}</span></div>
            <Button type="button" onClick={onOpenAcquisition}><Database aria-hidden="true" /> {t("Open Data Acquisition")}</Button>
            <code className="block overflow-x-auto rounded-md bg-muted px-3 py-3 text-xs">python -m finintel.storage import data/raw/AAPL.json</code>
            <p className="text-xs text-muted-foreground">{t("Alternatively, import a previously downloaded snapshot using the command above. All new data acquisition and completed import history are managed in Data Acquisition.")}</p>
          </CardContent>
        </Card>
      )}

      {indexState === "ready" && instruments.length > 0 && (
        <>
          {showSearch && (
          <div id="market-search-panel">
          <Card>
            <CardHeader>
              <CardTitle>{t("Instrument & time range")}</CardTitle>
              <CardAction><Badge variant="secondary">{instruments.length} {t("local series")}</Badge></CardAction>
            </CardHeader>
            <CardContent>
              <form onSubmit={applyRange} className="grid min-w-0 grid-cols-1 items-end gap-4 sm:grid-cols-2 2xl:grid-cols-[minmax(220px,1.7fr)_minmax(170px,1fr)_minmax(190px,1fr)_minmax(190px,1fr)]">
                <div className="grid min-w-0 gap-2 self-start text-sm font-medium text-muted-foreground">
                  <label htmlFor={instrumentListId + "-input"}>{t("Instrument")}</label>
                  <div className="relative min-w-0">
                    <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-3 size-4" />
                    <input
                      id={instrumentListId + "-input"}
                      role="combobox"
                      aria-autocomplete="list"
                      aria-expanded={instrumentFocused && instrumentQuery.trim().length > 0}
                      aria-controls={instrumentListId + "-list"}
                      aria-activedescendant={instrumentFocused && activeSuggestion >= 0 ? instrumentListId + "-option-" + activeSuggestion : undefined}
                      type="search"
                      value={instrumentFocused ? instrumentQuery : (instrumentNames[symbol] ? `${symbol} — ${instrumentNames[symbol]}` : instrumentQuery)}
                      onChange={(event) => { setInstrumentQuery(event.target.value); setActiveSuggestion(-1); }}
                      onFocus={() => setInstrumentFocused(true)}
                      onBlur={() => { setInstrumentFocused(false); setActiveSuggestion(-1); }}
                      onKeyDown={(event) => {
                        if (event.key === "Escape") {
                          setInstrumentFocused(false);
                          setActiveSuggestion(-1);
                          event.currentTarget.blur();
                        } else if (event.key === "ArrowDown") {
                          event.preventDefault();
                          setActiveSuggestion((previous) => Math.min(previous + 1, matchingInstruments.length - 1));
                        } else if (event.key === "ArrowUp") {
                          event.preventDefault();
                          setActiveSuggestion((previous) => Math.max(previous - 1, 0));
                        } else if (event.key === "Enter") {
                          event.preventDefault();
                          const match = matchingInstruments[activeSuggestion] ?? matchingInstruments.find((item) => item.symbol.toUpperCase() === instrumentQuery.trim().toUpperCase());
                          if (match) chooseInstrument(match);
                        }
                      }}
                      placeholder={t("Search ticker, company or ETF name")}
                      maxLength={80}
                      autoComplete="off"
                      className="h-12 w-full min-w-0 rounded-md border border-input bg-muted pl-9 pr-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    />
                    {instrumentFocused && instrumentQuery.trim() && (
                      <div id={instrumentListId + "-list"} role="listbox" aria-label={t("Stored instrument matches")} className="absolute inset-x-0 top-full z-30 mt-1 max-h-72 overflow-y-auto rounded-md border border-border bg-card p-1 shadow-lg">
                        {matchingInstruments.length ? matchingInstruments.map((item, index) => (
                          <button
                            key={item.symbol + "|" + item.provider}
                            id={instrumentListId + "-option-" + index}
                            type="button"
                            role="option"
                            aria-selected={activeSuggestion === index}
                            onMouseDown={(event) => event.preventDefault()}
                            onClick={() => chooseInstrument(item)}
                            className={`flex w-full min-w-0 items-center gap-2 rounded px-3 py-2 text-left text-sm hover:bg-accent ${activeSuggestion === index ? "bg-accent" : ""}`}
                          >
                            <span className="font-semibold text-foreground">{item.symbol}</span>
                            <span className="min-w-0 flex-1 truncate">{instrumentNames[item.symbol] ?? (item.provider === "tiingo" ? "Tiingo" : item.provider === "alpaca_sip" ? "Alpaca SIP" : "Alpha Vantage")}</span>
                            <span className="shrink-0 text-[11px]">{t("Stored locally")}</span>
                          </button>
                        )) : <p className="px-3 py-2 text-xs">{t("No stored instruments match. Try a different ticker or use Data Acquisition.")}</p>}
                      </div>
                    )}
                  </div>
                </div>
                <label className="grid min-w-0 gap-2 self-start text-sm font-medium text-muted-foreground">
                  {t("Display period")}
                  <select
                    aria-label={t("Historical time range")}
                    value={period}
                    onChange={(event) => selectPeriod(event.target.value as HistoryPeriod)}
                    className="h-12 w-full min-w-0 rounded-md border border-input bg-muted px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <option value="recent">{t("Latest 100 sessions")}</option>
                    <option value="1y">{t("Last 1 year")}</option>
                    <option value="5y">{t("Last 5 years")}</option>
                    <option value="10y">{t("Last 10 years")}</option>
                    <option value="custom">{t("Custom dates")}</option>
                  </select>
                </label>
                <MarketDatePicker label={t("From (optional)")} value={draftStart} onChange={setDraftStart} />
                <MarketDatePicker label={t("To (optional)")} value={draftEnd} onChange={setDraftEnd} />
                <div className="flex min-w-0 flex-wrap items-center justify-between gap-3 sm:col-span-2 2xl:col-span-4">
                  <p className="min-w-0 flex-1 text-xs leading-5 text-muted-foreground">{t("Type to filter stored instruments; select a result to update the chart.")}</p>
                  <div className="flex shrink-0 flex-wrap items-center gap-2">
                  <Button type="submit" size="lg">{t("Apply range")}</Button>
                <Button type="button" size="lg" variant="ghost" onClick={() => {
                  setDraftStart(""); setDraftEnd(""); setRange({ start: "", end: "" }); setValidationError(""); setPeriod("recent");
                }}>{t("Clear")}</Button>
                  </div>
                </div>
              </form>
              {validationError && <p role="alert" className="mt-3 text-sm text-destructive">{validationError}</p>}
            </CardContent>
          </Card>
          </div>
          )}

          {seriesState === "loading" && (
            <Card><CardContent className="space-y-4"><Skeleton className="h-7 w-44" aria-label={t("Loading historical prices")} /><Skeleton className="h-72 w-full" /></CardContent></Card>
          )}
          {seriesState === "error" && (
            <Card role="alert"><CardContent className="space-y-3">
              <p className="flex items-center gap-2 text-sm text-destructive"><AlertCircle aria-hidden="true" className="size-4" />{t("Could not load stored prices for")} {symbol}.</p>
              <p className="text-xs text-muted-foreground">{seriesError}</p>
              <Button variant="secondary" onClick={() => setReload((v) => v + 1)}>{t("Retry local query")}</Button>
            </CardContent></Card>
          )}
          {seriesState === "ready" && series && (
            <>
              <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
                <Metric label={t("Instrument")} value={series.symbol} note={priceProvider + " · " + formatInteger(bars.length) + " " + t("loaded sessions")} />
                <Metric label={t("Last close")} value={last ? price(last.close) : "—"} note={last?.session_date ?? t("No trading sessions")} />
                <Metric label={t("Daily change")} value={closeChange === null ? "—" : (closeChange >= 0 ? "+" : "") + price(String(closeChange))} note={t("Compared with prior loaded session")} />
                <Metric label={t("Loaded observations")} value={formatInteger(bars.length)} note={t(series.has_more ? "Bounded result · more stored" : "Available stored data")} />
              </div>

              {period !== "recent" && bars.length > 0 && bars.length < (period === "10y" ? 1200 : period === "5y" ? 600 : 120) && !series.has_more && (
                <p role="status" className="text-xs text-muted-foreground">
                  {t("Only")} {formatInteger(bars.length)} {t("sessions are stored for this selection. More history may require a premium full-history import, or the instrument may have a shorter trading history. This selector never downloads data automatically.")}
                </p>
              )}
              {series.has_more && (
                <p role="status" className="text-xs text-muted-foreground">
                  {t("Display is limited to 4,000 latest records. Use date filters to examine older stored sessions.")}
                </p>
              )}

              <TechnicalIndicatorsCard bars={bars} />

              <Card>
                <CardHeader>
                  <CardTitle><span className="flex items-center gap-2"><BarChart3 aria-hidden="true" className="size-5" /> {t("OHLCV history")}</span></CardTitle>
                  <CardAction><Badge variant="outline">{t(series.price_adjustment.replaceAll("_", " "))}</Badge></CardAction>
                  <CardDescription className="mt-2">{first && last ? first.session_date + " – " + last.session_date : t("No sessions in selected range")} · {t("historical end-of-day data")}</CardDescription>
                </CardHeader>
                <CardContent><MarketCharts bars={bars} period={period} /></CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle>{t("Historical price table")}</CardTitle>
                  <CardAction><Badge variant="secondary">{formatInteger(sorted.length)} {t("loaded rows")}</Badge></CardAction>
                  <CardDescription className="mt-2">{t("Sortable local data. Prices are displayed without assuming a currency.")}</CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="overflow-x-auto rounded-md border border-border">
                    <table className="w-full min-w-[690px] border-collapse text-right text-xs tabular-nums">
                      <thead className="bg-muted text-muted-foreground">
                        <tr>
                          <th scope="col" className="px-3 py-3 text-left"><button type="button" onClick={() => sortBy("session_date")} className="font-medium hover:text-foreground">{sortLabel("session_date", "Session")}</button></th>
                          <th scope="col" className="px-3 py-3 font-medium">{t("Open")}</th>
                          <th scope="col" className="px-3 py-3 font-medium">{t("High")}</th>
                          <th scope="col" className="px-3 py-3 font-medium">{t("Low")}</th>
                          <th scope="col" className="px-3 py-3"><button type="button" onClick={() => sortBy("close")} className="font-medium hover:text-foreground">{sortLabel("close", "Close")}</button></th>
                          <th scope="col" className="px-3 py-3"><button type="button" onClick={() => sortBy("volume")} className="font-medium hover:text-foreground">{sortLabel("volume", "Volume")}</button></th>
                        </tr>
                      </thead>
                      <tbody>
                        {visibleRows.map((bar) => (
                          <tr key={bar.session_date} className="border-t border-border/80 hover:bg-muted/65">
                            <td className="px-3 py-2.5 text-left font-medium text-foreground">{bar.session_date}</td>
                            <td className="px-3 py-2.5">{price(bar.open)}</td>
                            <td className="px-3 py-2.5">{price(bar.high)}</td>
                            <td className="px-3 py-2.5">{price(bar.low)}</td>
                            <td className="px-3 py-2.5 font-semibold">{price(bar.close)}</td>
                            <td className="px-3 py-2.5">{formatInteger(bar.volume)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {visibleRows.length === 0 && <p className="p-8 text-center text-sm text-muted-foreground">{t("No observations in this date range.")}</p>}
                  </div>
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="text-xs text-muted-foreground" aria-live="polite">Page {page + 1} of {pageCount}</p>
                    <div className="flex gap-2">
                      <Button size="sm" variant="secondary" disabled={page <= 0} onClick={() => setPage((value) => Math.max(0, value - 1))}>{t("Previous")}</Button>
                      <Button size="sm" variant="secondary" disabled={page >= pageCount - 1} onClick={() => setPage((value) => Math.min(pageCount - 1, value + 1))}>{t("Next")}</Button>
                    </div>
                  </div>
                </CardContent>
              </Card>

              <Card>
                <CardHeader><CardTitle>{t("Source & provenance")}</CardTitle>
                  <CardAction><Badge variant="outline">{t("Recorded at import")}</Badge></CardAction>
                </CardHeader>
                <CardContent className="grid gap-3 text-xs text-muted-foreground sm:grid-cols-2">
                  <p>{t("Provider:")} <span className="text-foreground">{series.provider}</span></p>
                  <p>{t("Frequency:")} <span className="text-foreground">{series.frequency}</span></p>
                  <p>{t("Data kind:")} <span className="text-foreground">{series.data_kind}</span></p>
                  <p>{t("Retrieved at:")} <span className="text-foreground">{series.retrieved_at ?? t("Unavailable")}</span></p>
                  <p>{t("Provider last refreshed:")} <span className="text-foreground">{series.provider_last_refreshed ?? t("Unavailable")}</span></p>
                  <p>{t("Stored sessions:")} <span className="text-foreground">{formatInteger(bars.length) + (series.has_more ? " (partial)" : "")}</span></p>
                  <p className="sm:col-span-2 break-all">{t("Source URL:")} <span className="text-foreground">{series.source_url ?? t("Unavailable")}</span></p>
                </CardContent>
              </Card>
            </>
          )}

        </>
      )}
      <footer className="text-xs text-muted-foreground">{t("Local research workstation · Charts display local SQLite data only · Fetches and action history live in Data Acquisition · Not a live market feed")}</footer>
    </section>
  );
}

function Metric({ label, value, note }: { label: string; value: string; note: string }) {
  return <Card className="gap-2 py-4">
    <CardContent className="space-y-2">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <p className="break-words text-2xl font-semibold tabular-nums">{value}</p>
      <p className="text-[11px] leading-5 text-muted-foreground">{note}</p>
    </CardContent>
  </Card>;
}
