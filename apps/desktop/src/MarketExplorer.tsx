import { useEffect, useId, useMemo, useState, type FormEvent } from "react";
import { AlertCircle, BarChart3, Database, RefreshCw, Search, SlidersHorizontal } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader } from "./PageHeader";
import { useLocale } from "./localization";
import { useNotify } from "./notifications";
import { fetchMarketDaily, fetchMarketInstrumentDirectory } from "./market-api";
import { searchAcquirableSymbols } from "./symbol-catalog-api";
import { listIndexHistory, readIndexHistory, type IndexCatalogRecord, type IndexHistory } from "./index-history-api";
import type { MarketSelection } from "./market-selection";
import { DEFAULT_MARKET_SELECTION } from "./market-selection";
import { IndexLineChart, MarketCharts } from "./market-chart";
import { TechnicalIndicatorsCard } from "./TechnicalIndicatorsCard";
import { MarketDatePicker } from "./components/MarketDatePicker";
import { yearsAgo, type HistoryPeriod } from "./history-range";
import { sortableBars, type DailySeries, type MarketInstrument } from "./market";

type LoadState = "loading" | "ready" | "error";
type SortColumn = "session_date" | "close" | "volume";
type EquityProvider = "alpha_vantage" | "alpaca_sip" | "tiingo";
type ExplorerInstrument =
  | { kind: "equity"; item: MarketInstrument }
  | { kind: "index"; item: IndexCatalogRecord };

const pageSize = 25;
const formatInteger = (value: number) => new Intl.NumberFormat("en-US").format(value);
const price = (raw: string) => new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 }).format(Number(raw));
const providerName = (provider: string) => provider === "alpaca_sip" ? "Alpaca SIP" : provider === "tiingo" ? "Tiingo" : "Alpha Vantage";

function instrumentLabel(instrument: ExplorerInstrument, names: Record<string, string>): string {
  if (instrument.kind === "index") return `${instrument.item.name} (${instrument.item.code})`;
  return names[instrument.item.symbol] ? `${instrument.item.symbol} — ${names[instrument.item.symbol]}` : instrument.item.symbol;
}

function filterIndexHistory(history: IndexHistory, start: string, end: string): IndexHistory {
  return {
    ...history,
    observations: history.observations.filter((row) => (!start || row.session_date >= start) && (!end || row.session_date <= end)),
  };
}

export function MarketExplorer({ initialSelection, onSelectionChange, onOpenAcquisition }: {
  initialSelection?: MarketSelection | null;
  onSelectionChange?: (selection: MarketSelection) => void;
  onOpenAcquisition: () => void;
}) {
  const { t } = useLocale();
  const notify = useNotify();
  const startingSelection = initialSelection ?? DEFAULT_MARKET_SELECTION;
  const [instruments, setInstruments] = useState<MarketInstrument[]>([]);
  const [indexCatalog, setIndexCatalog] = useState<IndexCatalogRecord[]>([]);
  const [indexState, setIndexState] = useState<LoadState>("loading");
  const [indexError, setIndexError] = useState("");
  const [directoryError, setDirectoryError] = useState("");
  const [selection, setSelection] = useState<MarketSelection>(startingSelection);
  const [draftStart, setDraftStart] = useState("");
  const [draftEnd, setDraftEnd] = useState("");
  const [range, setRange] = useState({ start: "", end: "" });
  const [period, setPeriod] = useState<HistoryPeriod>("recent");
  const [validationError, setValidationError] = useState("");
  const [series, setSeries] = useState<DailySeries | null>(null);
  const [indexHistory, setIndexHistory] = useState<IndexHistory | null>(null);
  const [seriesState, setSeriesState] = useState<LoadState>("loading");
  const [seriesError, setSeriesError] = useState("");
  const [sortColumn, setSortColumn] = useState<SortColumn>("session_date");
  const [descending, setDescending] = useState(true);
  const [page, setPage] = useState(0);
  const [reload, setReload] = useState(0);
  const [showSearch, setShowSearch] = useState(false);
  const [instrumentNames, setInstrumentNames] = useState<Record<string, string>>({});
  const [instrumentQuery, setInstrumentQuery] = useState(startingSelection.kind === "index" ? startingSelection.code : startingSelection.symbol);
  const [instrumentFocused, setInstrumentFocused] = useState(false);
  const [activeSuggestion, setActiveSuggestion] = useState(-1);
  const instrumentListId = useId();

  useEffect(() => {
    const next = initialSelection ?? DEFAULT_MARKET_SELECTION;
    setSelection(next);
    if (next.kind === "index") {
      setInstrumentQuery(next.code);
    } else {
      setInstrumentQuery(next.symbol);
    }
  }, [initialSelection]);

  useEffect(() => {
    let ignore = false;
    setIndexState("loading");
    const load = async () => {
      const [directory, indexes] = await Promise.allSettled([
        fetchMarketInstrumentDirectory(),
        listIndexHistory(),
      ]);
      if (ignore) return;
      if (directory.status === "fulfilled") {
        setInstruments(directory.value);
        setDirectoryError("");
      } else {
        setDirectoryError(directory.reason instanceof Error ? directory.reason.message : String(directory.reason));
      }
      if (indexes.status === "fulfilled") {
        setIndexCatalog(indexes.value);
        setIndexError("");
      } else {
        setIndexError(indexes.reason instanceof Error ? indexes.reason.message : String(indexes.reason));
      }
      if (directory.status === "rejected" && indexes.status === "rejected") {
        setIndexState("error");
        notify(t("Market instruments could not be loaded. See details below."), "error");
      } else {
        setIndexState("ready");
      }
    };
    void load();
    return () => { ignore = true; };
  }, [reload]);

  useEffect(() => {
    if (indexState !== "ready") return;
    let ignore = false;
    setSeries(null);
    setIndexHistory(null);
    setSeriesState("loading");
    setPage(0);
    if (selection.kind === "index") {
      void readIndexHistory(selection.code, period === "recent" && !range.start && !range.end ? 100 : 10000)
        .then((data) => {
          if (ignore) return;
          setIndexHistory(filterIndexHistory(data, range.start, range.end));
          setSeriesState("ready");
          setSeriesError("");
        })
        .catch((error: unknown) => {
          if (ignore) return;
          setSeriesState("error");
          setSeriesError(error instanceof Error ? error.message : String(error));
          notify(t("Market index history could not be loaded. See details below."), "error");
        });
    } else {
      void fetchMarketDaily({
        symbol: selection.symbol,
        provider: selection.provider,
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
    }
    return () => { ignore = true; };
  }, [selection, range, reload, indexState, period]);

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
      }).catch(() => { /* A missing equity catalog must not hide local history. */ });
    }, 180);
    return () => { cancelled = true; window.clearTimeout(timeout); };
  }, [instrumentQuery]);

  const matchingInstruments = useMemo<ExplorerInstrument[]>(() => {
    const q = instrumentQuery.trim().toUpperCase();
    if (!q) return [];
    const indexes = indexCatalog
      .filter((item) => item.code.includes(q) || item.name.toUpperCase().includes(q) || item.fred_series.includes(q))
      .map((item): ExplorerInstrument => ({ kind: "index", item }));
    const equities = instruments
      .filter((item) => item.symbol.toUpperCase().includes(q) || item.provider.toUpperCase().includes(q) || (instrumentNames[item.symbol] ?? "").toUpperCase().includes(q))
      .map((item): ExplorerInstrument => ({ kind: "equity", item }));
    return [...indexes, ...equities]
      .sort((a, b) => {
        const aValue = a.kind === "index" ? a.item.code : a.item.symbol;
        const bValue = b.kind === "index" ? b.item.code : b.item.symbol;
        return Number(!aValue.toUpperCase().startsWith(q)) - Number(!bValue.toUpperCase().startsWith(q)) || aValue.localeCompare(bValue);
      })
      .slice(0, 8);
  }, [instrumentQuery, instruments, indexCatalog, instrumentNames]);

  const chooseInstrument = (instrument: ExplorerInstrument) => {
    const next: MarketSelection = instrument.kind === "index"
      ? { kind: "index", code: instrument.item.code }
      : { kind: "equity", symbol: instrument.item.symbol, provider: instrument.item.provider as EquityProvider };
    setSelection(next);
    onSelectionChange?.(next);
    if (next.kind === "index") {
      setInstrumentQuery(next.code);
    } else {
      setInstrumentQuery(next.symbol);
    }
    setInstrumentFocused(false);
    setActiveSuggestion(-1);
  };

  const selectedInstrument: ExplorerInstrument = selection.kind === "index"
    ? { kind: "index", item: indexCatalog.find((item) => item.code === selection.code) ?? {
      code: selection.code,
      name: selection.code === "SP500" ? "S&P 500" : selection.code,
      fred_series: selection.code,
      provider: "fred",
      source_url: "",
      series_type: selection.code === "VIX" ? "volatility" : "price",
      frequency: "daily",
      currency: null,
      units: "index_points",
      licensing_note: "",
      known_history_limit: "",
      sessions: 0,
      local_first_session: null,
      local_last_session: null,
      provider_earliest_available: null,
      provider_latest_available: null,
      first_session: null,
      last_session: null,
      coverage_updated_at: null,
      data_kind: "daily_close_only",
    } }
    : { kind: "equity", item: { symbol: selection.symbol, provider: selection.provider, frequency: "1d", adjustment: "raw_unadjusted", data_kind: "historical_eod", sessions: 0, first_session: null, last_session: null } };
  const selectedLabel = instrumentLabel(selectedInstrument, instrumentNames);
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
    const start = selected === "recent" ? "" : yearsAgo(selected === "1y" ? 1 : selected === "5y" ? 5 : 10);
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
    if (sortColumn === column) setDescending((previous) => !previous);
    else { setSortColumn(column); setDescending(true); }
  };
  const sortLabel = (column: SortColumn, label: string) => t(label) + (sortColumn === column ? (descending ? " ↓" : " ↑") : "");
  const hasAnyInstrument = instruments.length > 0 || indexCatalog.length > 0;

  return (
    <section className="space-y-2" aria-labelledby="markets-title">
      <PageHeader
        id="markets-title"
        breadcrumb={t("FinIntel / Markets")}
        title={t("Market Data Explorer")}
        subtitle={`${t("Historical daily prices stored in local SQLite. No live quotes.")} · ${t("Selected instrument")}: ${selectedLabel}`}
        actions={<>
          <Badge variant="secondary" aria-label={t("Selected instrument")}>{selectedLabel}</Badge>
          <Badge variant="outline">{t("Local SQLite")}</Badge>
          <Button type="button" variant="secondary" size="sm" onClick={() => setReload((value) => value + 1)}><RefreshCw aria-hidden="true" /> {t("Reload local data")}</Button>
          <Button type="button" size="sm" onClick={onOpenAcquisition}><Database aria-hidden="true" /> {t("Data Acquisition")}</Button>
          <Button type="button" variant={showSearch ? "secondary" : "outline"} size="sm" aria-expanded={showSearch} aria-controls="market-search-panel" onClick={() => setShowSearch((current) => !current)}><SlidersHorizontal aria-hidden="true" /> {showSearch ? t("Hide filters") : t("Show filters")}</Button>
        </>}
      />
      {indexState === "loading" && <Card><CardContent><Skeleton className="h-8 w-64" aria-label={t("Loading local instruments")} /></CardContent></Card>}
      {indexState === "error" && <Card role="alert"><CardHeader><CardTitle>{t("Local database unavailable")}</CardTitle></CardHeader><CardContent className="space-y-3 text-sm text-muted-foreground"><p>{indexError || directoryError}</p><p>{t("Start the Python service on 127.0.0.1:8765 and try again.")}</p><Button onClick={() => setReload((value) => value + 1)} variant="secondary">{t("Retry")}</Button></CardContent></Card>}
      {indexState === "ready" && !hasAnyInstrument && <Card><CardHeader><CardTitle>{t("No stored market data yet")}</CardTitle><CardDescription>{t("FinIntel reads your local SQLite database. Open Data Acquisition to fetch and store your first stock or ETF, or use Index History to import an index.")}</CardDescription></CardHeader><CardContent className="space-y-3"><div className="flex items-center gap-2 text-muted-foreground"><Database aria-hidden="true" className="size-5" /><span className="text-sm">{t("No price series have been imported.")}</span></div><Button type="button" onClick={onOpenAcquisition}><Database aria-hidden="true" /> {t("Open Data Acquisition")}</Button></CardContent></Card>}
      {indexState === "ready" && hasAnyInstrument && (
        <>
          {indexError && <p role="status" className="text-xs text-muted-foreground">{t("Index history service unavailable. Stock and ETF history remains available.")} · {indexError}</p>}
          {showSearch && <div id="market-search-panel"><Card><CardHeader><CardTitle>{t("Instrument & time range")}</CardTitle><CardAction><Badge variant="secondary">{instruments.length + indexCatalog.length} {t("local instruments")}</Badge></CardAction></CardHeader><CardContent>
            <form onSubmit={applyRange} className="grid min-w-0 grid-cols-1 items-end gap-4 sm:grid-cols-2 2xl:grid-cols-[minmax(220px,1.7fr)_minmax(170px,1fr)_minmax(190px,1fr)_minmax(190px,1fr)]">
              <div className="grid min-w-0 gap-2 self-start text-sm font-medium text-muted-foreground"><label htmlFor={instrumentListId + "-input"}>{t("Instrument")}</label><div className="relative min-w-0"><Search aria-hidden="true" className="pointer-events-none absolute left-3 top-3 size-4" /><input id={instrumentListId + "-input"} role="combobox" aria-autocomplete="list" aria-expanded={instrumentFocused && instrumentQuery.trim().length > 0} aria-controls={instrumentListId + "-list"} aria-activedescendant={instrumentFocused && activeSuggestion >= 0 ? instrumentListId + "-option-" + activeSuggestion : undefined} type="search" value={instrumentFocused ? instrumentQuery : selectedLabel} onChange={(event) => { setInstrumentQuery(event.target.value); setActiveSuggestion(-1); }} onFocus={() => setInstrumentFocused(true)} onBlur={() => { setInstrumentFocused(false); setActiveSuggestion(-1); }} onKeyDown={(event) => { if (event.key === "Escape") { setInstrumentFocused(false); setActiveSuggestion(-1); event.currentTarget.blur(); } else if (event.key === "ArrowDown") { event.preventDefault(); setActiveSuggestion((previous) => Math.min(previous + 1, matchingInstruments.length - 1)); } else if (event.key === "ArrowUp") { event.preventDefault(); setActiveSuggestion((previous) => Math.max(previous - 1, 0)); } else if (event.key === "Enter") { event.preventDefault(); const match = matchingInstruments[activeSuggestion] ?? matchingInstruments.find((item) => (item.kind === "index" ? item.item.code : item.item.symbol).toUpperCase() === instrumentQuery.trim().toUpperCase()); if (match) chooseInstrument(match); } }} placeholder={t("Search ticker, company, ETF or index")} maxLength={80} autoComplete="off" className="h-12 w-full min-w-0 rounded-md border border-input bg-muted pl-9 pr-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />{instrumentFocused && instrumentQuery.trim() && <div id={instrumentListId + "-list"} role="listbox" aria-label={t("Stored instrument matches")} className="absolute inset-x-0 top-full z-30 mt-1 max-h-72 overflow-y-auto rounded-md border border-border bg-card p-1 shadow-lg">{matchingInstruments.length ? matchingInstruments.map((item, index) => <button key={item.kind === "index" ? item.item.code : item.item.symbol + "|" + item.item.provider} id={instrumentListId + "-option-" + index} type="button" role="option" aria-selected={activeSuggestion === index} onMouseDown={(event) => event.preventDefault()} onClick={() => chooseInstrument(item)} className={`flex w-full min-w-0 items-center gap-2 rounded px-3 py-2 text-left text-sm hover:bg-accent ${activeSuggestion === index ? "bg-accent" : ""}`}><span className="font-semibold text-foreground">{item.kind === "index" ? item.item.code : item.item.symbol}</span><span className="min-w-0 flex-1 truncate">{item.kind === "index" ? item.item.name : instrumentNames[item.item.symbol] ?? providerName(item.item.provider)}</span><span className="shrink-0 text-[11px]">{item.kind === "index" ? t("Index") : t("Stored locally")}</span></button>) : <p className="px-3 py-2 text-xs">{t("No stored instruments match. Try a different ticker or use Data Acquisition.")}</p>}</div>}</div></div>
              <label className="grid min-w-0 gap-2 self-start text-sm font-medium text-muted-foreground">{t("Display period")}<select aria-label={t("Historical time range")} value={period} onChange={(event) => selectPeriod(event.target.value as HistoryPeriod)} className="h-12 w-full min-w-0 rounded-md border border-input bg-muted px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><option value="recent">{t("Latest 100 sessions")}</option><option value="1y">{t("Last 1 year")}</option><option value="5y">{t("Last 5 years")}</option><option value="10y">{t("Last 10 years")}</option><option value="custom">{t("Custom dates")}</option></select></label>
              <MarketDatePicker label={t("From (optional)")} value={draftStart} onChange={setDraftStart} /><MarketDatePicker label={t("To (optional)")} value={draftEnd} onChange={setDraftEnd} />
              <div className="flex min-w-0 flex-wrap items-center justify-between gap-3 sm:col-span-2 2xl:col-span-4"><p className="min-w-0 flex-1 text-xs leading-5 text-muted-foreground">{t("Search stocks, ETFs, and locally stored indexes; select a result to update the chart.")}</p><div className="flex shrink-0 flex-wrap items-center gap-2"><Button type="submit" size="lg">{t("Apply range")}</Button><Button type="button" size="lg" variant="ghost" onClick={() => { setDraftStart(""); setDraftEnd(""); setRange({ start: "", end: "" }); setValidationError(""); setPeriod("recent"); }}>{t("Clear")}</Button></div></div>
            </form>
            {validationError && <p role="alert" className="mt-3 text-sm text-destructive">{validationError}</p>}
          </CardContent></Card></div>}
          {seriesState === "loading" && <Card><CardContent className="space-y-4"><Skeleton className="h-7 w-44" aria-label={t("Loading historical prices")} /><Skeleton className="h-72 w-full" /></CardContent></Card>}
          {seriesState === "error" && <Card role="alert"><CardContent className="space-y-3"><p className="flex items-center gap-2 text-sm text-destructive"><AlertCircle aria-hidden="true" className="size-4" />{t("Could not load stored prices for")} {selectedLabel}.</p><p className="text-xs text-muted-foreground">{seriesError}</p><Button variant="secondary" onClick={() => setReload((value) => value + 1)}>{t("Retry local query")}</Button></CardContent></Card>}
          {seriesState === "ready" && selection.kind === "index" && indexHistory && <IndexMarketContent history={indexHistory} selectedLabel={selectedLabel} period={period} onOpenAcquisition={onOpenAcquisition} t={t} />}
          {seriesState === "ready" && selection.kind === "equity" && series && <EquityMarketContent bars={bars} series={series} selectedLabel={selectedLabel} period={period} first={first} last={last} closeChange={closeChange} sorted={sorted} visibleRows={visibleRows} page={page} pageCount={pageCount} sortBy={sortBy} sortLabel={sortLabel} setPage={setPage} t={t} />}
        </>
      )}
      <footer className="text-xs text-muted-foreground">{t("Local research workstation · Charts display local SQLite data only · Fetches and action history live in Data Acquisition · Not a live market feed")}</footer>
    </section>
  );
}

function IndexMarketContent({ history, selectedLabel, period, onOpenAcquisition, t }: { history: IndexHistory; selectedLabel: string; period: HistoryPeriod; onOpenAcquisition: () => void; t: (key: string) => string }) {
  const observations = history.observations;
  const latest = observations.at(-1);
  const previous = observations.at(-2);
  const pointChange = latest && previous ? Number(latest.close) - Number(previous.close) : null;
  const percentChange = latest && previous ? (Number(latest.close) / Number(previous.close) - 1) * 100 : null;
  if (observations.length === 0) return <Card><CardHeader><CardTitle>{t("No local history for")} {selectedLabel}</CardTitle><CardDescription>{t("This index has no locally stored closing levels. Open Data Acquisition and use Index History to fetch it explicitly.")}</CardDescription></CardHeader><CardContent><Button type="button" onClick={onOpenAcquisition}><Database aria-hidden="true" /> {t("Open Data Acquisition")}</Button></CardContent></Card>;
  return <>
    <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4"><Metric label={t("Selected index")} value={selectedLabel} note={history.provider + " · " + formatInteger(observations.length) + " " + t("loaded sessions")} /><Metric label={t("Latest close")} value={price(latest!.close)} note={latest!.session_date} /><Metric label={t("Index-point change")} value={pointChange === null ? "—" : (pointChange >= 0 ? "+" : "") + price(String(pointChange))} note={percentChange === null ? t("Compared with prior loaded session") : `${percentChange >= 0 ? "+" : ""}${percentChange.toFixed(2)}% ${t("from prior observation")}`} /><Metric label={t("Latest available observation")} value={latest!.session_date} note={t("Local close history")} /></div>
    {period !== "recent" && <p role="status" className="text-xs text-muted-foreground">{t("Index periods use locally stored closing levels only; this selector never downloads data automatically.")}</p>}
    <Card><CardHeader><CardTitle><span className="flex items-center gap-2"><BarChart3 aria-hidden="true" className="size-5" /> {t("Index closing-level history")}</span></CardTitle><CardAction><Badge variant="outline">{t("FRED · daily close · index points")}</Badge></CardAction><CardDescription className="mt-2">{observations[0].session_date} – {latest!.session_date} · {t("actual stored observations")}</CardDescription></CardHeader><CardContent><IndexLineChart observations={observations} /></CardContent></Card>
    <Card><CardHeader><CardTitle>{t("Historical index table")}</CardTitle><CardAction><Badge variant="secondary">{formatInteger(observations.length)} {t("loaded rows")}</Badge></CardAction><CardDescription className="mt-2">{t("Date and closing level from local SQLite. No synthetic OHLC or volume values are generated.")}</CardDescription></CardHeader><CardContent><div className="overflow-x-auto rounded-md border border-border"><table className="w-full min-w-[420px] border-collapse text-right text-xs tabular-nums"><thead className="bg-muted text-muted-foreground"><tr><th scope="col" className="px-3 py-3 text-left font-medium">{t("Date")}</th><th scope="col" className="px-3 py-3 font-medium">{t("Close")}</th></tr></thead><tbody>{[...observations].reverse().map((row) => <tr key={row.session_date} className="border-t border-border/80 hover:bg-muted/65"><td className="px-3 py-2.5 text-left font-medium text-foreground">{row.session_date}</td><td className="px-3 py-2.5 font-semibold">{price(row.close)}</td></tr>)}</tbody></table></div></CardContent></Card>
    <Card><CardHeader><CardTitle>{t("Source & provenance")}</CardTitle><CardAction><Badge variant="outline">{t("Recorded at import")}</Badge></CardAction></CardHeader><CardContent className="grid gap-3 text-xs text-muted-foreground sm:grid-cols-2"><p>{t("Provider:")} <span className="text-foreground">{history.provider}</span></p><p>{t("Data kind:")} <span className="text-foreground">{history.data_kind}</span></p><p>{t("Unit:")} <span className="text-foreground">{history.unit}</span></p><p>{t("Observations:")} <span className="text-foreground">{formatInteger(observations.length)}{history.has_more ? " (partial)" : ""}</span></p><p className="sm:col-span-2 break-all">{t("Source URL:")} <span className="text-foreground">{history.source_url}</span></p></CardContent></Card>
  </>;
}

function EquityMarketContent({ bars, series, selectedLabel, period, first, last, closeChange, sorted, visibleRows, page, pageCount, sortBy, sortLabel, setPage, t }: { bars: DailySeries["observations"]; series: DailySeries; selectedLabel: string; period: HistoryPeriod; first: DailySeries["observations"][number] | undefined; last: DailySeries["observations"][number] | undefined; closeChange: number | null; sorted: DailySeries["observations"]; visibleRows: DailySeries["observations"]; page: number; pageCount: number; sortBy: (column: SortColumn) => void; sortLabel: (column: SortColumn, label: string) => string; setPage: (value: number) => void; t: (key: string) => string }) {
  return <>
    <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4"><Metric label={t("Instrument")} value={selectedLabel} note={series.provider + " · " + formatInteger(bars.length) + " " + t("loaded sessions")} /><Metric label={t("Last close")} value={last ? price(last.close) : "—"} note={last?.session_date ?? t("No trading sessions")} /><Metric label={t("Daily change")} value={closeChange === null ? "—" : (closeChange >= 0 ? "+" : "") + price(String(closeChange))} note={t("Compared with prior loaded session")} /><Metric label={t("Loaded observations")} value={formatInteger(bars.length)} note={t(series.has_more ? "Bounded result · more stored" : "Available stored data")} /></div>
    {period !== "recent" && bars.length > 0 && bars.length < (period === "10y" ? 1200 : period === "5y" ? 600 : 120) && !series.has_more && <p role="status" className="text-xs text-muted-foreground">{t("Only")} {formatInteger(bars.length)} {t("sessions are stored for this selection. More history may require a premium full-history import, or the instrument may have a shorter trading history. This selector never downloads data automatically.")}</p>}
    {series.has_more && <p role="status" className="text-xs text-muted-foreground">{t("Display is limited to 4,000 latest records. Use date filters to examine older stored sessions.")}</p>}
    <TechnicalIndicatorsCard bars={bars} />
    <Card><CardHeader><CardTitle><span className="flex items-center gap-2"><BarChart3 aria-hidden="true" className="size-5" /> {t("OHLCV history")}</span></CardTitle><CardAction><Badge variant="outline">{t(series.price_adjustment.replaceAll("_", " "))}</Badge></CardAction><CardDescription className="mt-2">{first && last ? first.session_date + " – " + last.session_date : t("No sessions in selected range")} · {t("historical end-of-day data")}</CardDescription></CardHeader><CardContent><MarketCharts bars={bars} period={period} /></CardContent></Card>
    <Card><CardHeader><CardTitle>{t("Historical price table")}</CardTitle><CardAction><Badge variant="secondary">{formatInteger(sorted.length)} {t("loaded rows")}</Badge></CardAction><CardDescription className="mt-2">{t("Sortable local data. Prices are displayed without assuming a currency.")}</CardDescription></CardHeader><CardContent className="space-y-4"><div className="overflow-x-auto rounded-md border border-border"><table className="w-full min-w-[690px] border-collapse text-right text-xs tabular-nums"><thead className="bg-muted text-muted-foreground"><tr><th scope="col" className="px-3 py-3 text-left"><button type="button" onClick={() => sortBy("session_date")} className="font-medium hover:text-foreground">{sortLabel("session_date", "Session")}</button></th><th scope="col" className="px-3 py-3 font-medium">{t("Open")}</th><th scope="col" className="px-3 py-3 font-medium">{t("High")}</th><th scope="col" className="px-3 py-3 font-medium">{t("Low")}</th><th scope="col" className="px-3 py-3"><button type="button" onClick={() => sortBy("close")} className="font-medium hover:text-foreground">{sortLabel("close", "Close")}</button></th><th scope="col" className="px-3 py-3"><button type="button" onClick={() => sortBy("volume")} className="font-medium hover:text-foreground">{sortLabel("volume", "Volume")}</button></th></tr></thead><tbody>{visibleRows.map((bar) => <tr key={bar.session_date} className="border-t border-border/80 hover:bg-muted/65"><td className="px-3 py-2.5 text-left font-medium text-foreground">{bar.session_date}</td><td className="px-3 py-2.5">{price(bar.open)}</td><td className="px-3 py-2.5">{price(bar.high)}</td><td className="px-3 py-2.5">{price(bar.low)}</td><td className="px-3 py-2.5 font-semibold">{price(bar.close)}</td><td className="px-3 py-2.5">{formatInteger(bar.volume)}</td></tr>)}</tbody></table>{visibleRows.length === 0 && <p className="p-8 text-center text-sm text-muted-foreground">{t("No observations in this date range.")}</p>}</div><div className="flex flex-wrap items-center justify-between gap-2"><p className="text-xs text-muted-foreground" aria-live="polite">{t("Page")} {page + 1} {t("of")} {pageCount}</p><div className="flex gap-2"><Button size="sm" variant="secondary" disabled={page <= 0} onClick={() => setPage(Math.max(0, page - 1))}>{t("Previous")}</Button><Button size="sm" variant="secondary" disabled={page >= pageCount - 1} onClick={() => setPage(Math.min(pageCount - 1, page + 1))}>{t("Next")}</Button></div></div></CardContent></Card>
    <Card><CardHeader><CardTitle>{t("Source & provenance")}</CardTitle><CardAction><Badge variant="outline">{t("Recorded at import")}</Badge></CardAction></CardHeader><CardContent className="grid gap-3 text-xs text-muted-foreground sm:grid-cols-2"><p>{t("Provider:")} <span className="text-foreground">{series.provider}</span></p><p>{t("Frequency:")} <span className="text-foreground">{series.frequency}</span></p><p>{t("Data kind:")} <span className="text-foreground">{series.data_kind}</span></p><p>{t("Retrieved at:")} <span className="text-foreground">{series.retrieved_at ?? t("Unavailable")}</span></p><p>{t("Provider last refreshed:")} <span className="text-foreground">{series.provider_last_refreshed ?? t("Unavailable")}</span></p><p>{t("Stored sessions:")} <span className="text-foreground">{formatInteger(bars.length) + (series.has_more ? " (partial)" : "")}</span></p><p className="sm:col-span-2 break-all">{t("Source URL:")} <span className="text-foreground">{series.source_url ?? t("Unavailable")}</span></p></CardContent></Card>
  </>;
}

function Metric({ label, value, note }: { label: string; value: string; note: string }) {
  return <Card className="gap-2 py-4"><CardContent className="space-y-2"><p className="text-xs font-medium text-muted-foreground">{label}</p><p className="break-words text-2xl font-semibold tabular-nums">{value}</p><p className="text-[11px] leading-5 text-muted-foreground">{note}</p></CardContent></Card>;
}
