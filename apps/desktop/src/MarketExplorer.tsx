import { useEffect, useId, useMemo, useRef, useState, type FormEvent } from "react";
import { AlertCircle, BarChart3, Database, RefreshCw, Search, SlidersHorizontal } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader } from "./PageHeader";
import { boundedPage, LocalDataTable, TablePager, TABLE_PAGE_SIZE } from "./components/TablePagination";
import { useLocale } from "./localization";
import { useNotify } from "./notifications";
import { fetchMarketDaily, fetchMarketInstrumentDirectory } from "./market-api";
import { searchAcquirableSymbols } from "./symbol-catalog-api";
import { listIndexHistory, readIndexHistory, type IndexCatalogRecord, type IndexHistory } from "./index-history-api";
import type { MarketSelection } from "./market-selection";
import { MarketTabs } from "./MarketTabs";
import { MarketComparisonPanel } from "./MarketComparisonPanel";
import { comparisonSource, comparisonSourceFromSelection, sourceKey, type ComparisonSource } from "./market-comparison-api";
import { COMPARISON_LIMIT, COMPARISON_TAB_LIMIT, readComparisonWorkspace, saveComparisonWorkspace, updateComparisonTab, addComparisonTab, closeComparisonTab, type ComparisonPreferences } from "./market-comparison-preferences";
import { MARKET_TAB_LIMIT, marketTabKey, marketTabSymbol, parseMarketSelection, type MarketSort, type MarketView, type MarketWorkspace } from "./market-workspace";
import { IndexLineChart, MarketCharts } from "./market-chart";
import { TechnicalIndicatorsCard } from "./TechnicalIndicatorsCard";
import { MarketDatePicker } from "./components/MarketDatePicker";
import { yearsAgo, type HistoryPeriod } from "./history-range";
import { sortableBars, type DailySeries, type MarketInstrument } from "./market";

type LoadState = "loading" | "ready" | "error";
type SortColumn = MarketSort;
type ExplorerInstrument =
  | { kind: "equity"; item: MarketInstrument }
  | { kind: "index"; item: IndexCatalogRecord };

const pageSize = TABLE_PAGE_SIZE;
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

type HistoryResult =
  | { kind: "index"; data: IndexHistory }
  | { kind: "equity"; data: DailySeries }
  | { kind: "error" };

export function MarketExplorer({ workspace, limitReached, onActivateTab, onShowComparison, onCloseTab, onChooseInstrument, onUpdateView, onOpenAcquisition, pendingComparisonSource, onResolvePendingComparison }: {
  pendingComparisonSource?: ComparisonSource;
  onResolvePendingComparison?: () => void;
  workspace: MarketWorkspace;
  limitReached: boolean;
  onActivateTab: (key: string) => void;
  onShowComparison: () => void;
  onCloseTab: (key: string) => void;
  onChooseInstrument: (selection: MarketSelection, mode: "current" | "new") => void;
  onUpdateView: (key: string, patch: Partial<MarketView>) => void;
  onOpenAcquisition: () => void;
}) {
  const { t } = useLocale();
  const notify = useNotify();
  const { active } = workspace;
  const [historicalDetails, setHistoricalDetails] = useState(() => !workspace.comparing && !comparisonSourceFromSelection(workspace.tabs.find(tab => marketTabKey(tab.selection) === active)!.selection));
  const comparing = !historicalDetails;
  const [comparisonWorkspace, setComparisonWorkspace] = useState(() => {
    const source = comparisonSourceFromSelection(workspace.tabs.find(tab => marketTabKey(tab.selection) === active)!.selection);
    return readComparisonWorkspace(workspace.comparing ? undefined : source);
  });
  const comparison = comparisonWorkspace.tabs.find(tab => tab.id === comparisonWorkspace.active)!.settings;
  const activeComparisonId = comparisonWorkspace.active;
  const setComparison = (update: ComparisonPreferences | ((current: ComparisonPreferences) => ComparisonPreferences)) => {
    setComparisonWorkspace(current => updateComparisonTab(current, activeComparisonId, update));
  };
  const [comparisonPickerOpen, setComparisonPickerOpen] = useState(false);
  const [comparisonNotice, setComparisonNotice] = useState("");
  const [detailKey, setDetailKey] = useState("");
  const [replacementKey, setReplacementKey] = useState("");
  const replacementDialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    setReplacementKey("");
    if (!pendingComparisonSource) return;
    const dialog = replacementDialog.current;
    if (dialog && typeof dialog.showModal === "function" && !dialog.open) dialog.showModal();
    return () => { if (dialog?.open && typeof dialog.close === "function") dialog.close(); };
  }, [pendingComparisonSource]);
  useEffect(() => { saveComparisonWorkspace(comparisonWorkspace); }, [comparisonWorkspace]);
  useEffect(() => { if (comparing && !workspace.comparing) onShowComparison(); }, [comparing, workspace.comparing, onShowComparison]);
  const { selection, view } = workspace.tabs.find(tab => marketTabKey(tab.selection) === active)!;
  const { start, end, period, draftStart, draftEnd, sortColumn, descending, tableFilter, showSearch } = view;
  const update = (patch: Partial<MarketView>) => onUpdateView(active, patch);
  const [instruments, setInstruments] = useState<MarketInstrument[]>([]);
  const [indexCatalog, setIndexCatalog] = useState<IndexCatalogRecord[]>([]);
  const [directory, setDirectory] = useState<{ revision: number; status: LoadState }>();
  const [indexError, setIndexError] = useState("");
  const [directoryError, setDirectoryError] = useState("");
  const [validationError, setValidationError] = useState("");
  const [reload, setReload] = useState(0);
  const [addMode, setAddMode] = useState(false);
  const [pickerNotice, setPickerNotice] = useState(false);
  const [pendingInstrument, setPendingInstrument] = useState<ExplorerInstrument | null>(null);
  const [instrumentNames, setInstrumentNames] = useState<Record<string, string>>({});
  const [instrumentQuery, setInstrumentQuery] = useState(() => marketTabSymbol(selection));
  const [instrumentFocused, setInstrumentFocused] = useState(false);
  const [activeSuggestion, setActiveSuggestion] = useState(-1);
  const instrumentListId = useId();
  const panelId = `${instrumentListId}-market-panel`;
  const instrumentInput = useRef<HTMLInputElement>(null);
  const focusPicker = useRef(false);
  const indexState = directory?.revision === reload ? directory.status : "loading";
  const request = useMemo(() => ({ selection, start, end, period, reload, indexState, comparing }), [selection, start, end, period, reload, indexState, comparing]);
  const [result, setResult] = useState<{ request: typeof request; result: HistoryResult } | null>(null);
  const currentHistory = result?.request === request ? result.result : null;
  const series = currentHistory?.kind === "equity" ? currentHistory.data : null;
  const indexHistory = currentHistory?.kind === "index" ? currentHistory.data : null;

  useEffect(() => {
    setInstrumentQuery(marketTabSymbol(selection));
    setInstrumentFocused(false);
    setActiveSuggestion(-1);
    setPendingInstrument(null);
    setAddMode(false);
    setPickerNotice(false);
  }, [selection]);

  useEffect(() => {
    if (focusPicker.current && addMode && indexState === "ready") {
      focusPicker.current = false;
      instrumentInput.current?.focus();
    }
  }, [addMode, indexState]);

  useEffect(() => {
    let ignore = false;
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
        setDirectory({ revision: reload, status: "error" });
        notify(t("Market instruments could not be loaded. See details below."), "error");
      } else {
        setDirectory({ revision: reload, status: "ready" });
      }
    };
    void load();
    return () => { ignore = true; };
  }, [reload]);

  useEffect(() => {
    if (request.indexState !== "ready" || request.comparing) return;
    let ignore = false;
    const { selection, start, end, period } = request;
    const history: Promise<HistoryResult> = selection.kind === "index"
      ? readIndexHistory(selection.code, period === "recent" && !start && !end ? 100 : 10000)
        .then(data => ({ kind: "index", data: filterIndexHistory(data, start, end) }))
      : fetchMarketDaily({
        symbol: selection.symbol, provider: selection.provider,
        startDate: start || undefined, endDate: end || undefined,
        limit: period === "recent" ? 100 : 4000,
      }).then(data => ({ kind: "equity", data }));
    void history.then(history => {
      if (!ignore) setResult({ request, result: history });
    }).catch(() => {
      if (!ignore) setResult({ request, result: { kind: "error" } });
    });
    return () => { ignore = true; };
  }, [request]);

  useEffect(() => {
    const query = instrumentFocused ? instrumentQuery.trim() : "";
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
  }, [instrumentFocused, instrumentQuery]);

  const matchingInstruments = useMemo<ExplorerInstrument[]>(() => {
    const q = instrumentQuery.trim().toUpperCase();
    if (!q) return [];
    const indexes = indexCatalog
      .filter(item => item.sessions > 0 && (item.code.includes(q) || item.name.toUpperCase().includes(q) || item.fred_series.includes(q)))
      .map((item): ExplorerInstrument => ({ kind: "index", item }));
    const equities = instruments
      .filter(item => parseMarketSelection({ kind: "equity", ...item }))
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

  function stageInstrument(instrument: ExplorerInstrument) {
    setPendingInstrument(instrument);
    setInstrumentFocused(false);
    setActiveSuggestion(-1);
    setInstrumentQuery(instrument.kind === "index" ? instrument.item.code : instrument.item.symbol);
  }

  function openInstrument(mode: "current" | "new") {
    if (!pendingInstrument) return;
    const next = pendingInstrument.kind === "index"
      ? { kind: "index", code: pendingInstrument.item.code }
      : { kind: "equity", symbol: pendingInstrument.item.symbol, provider: pendingInstrument.item.provider };
    const validated = parseMarketSelection(next);
    if (!validated) return;
    const existing = workspace.tabs.some(tab => marketTabKey(tab.selection) === marketTabKey(validated));
    if (mode === "current" || existing || workspace.tabs.length < MARKET_TAB_LIMIT) {
      setPendingInstrument(null);
      setAddMode(false);
      setPickerNotice(false);
    }
    onChooseInstrument(validated, mode);
  }

  const comparisonChoices = useMemo(() => {
    const choices = new Map<string, ComparisonSource>();
    for (const item of indexCatalog) {
      const source = item.sessions > 0 && comparisonSource({ kind: "index", code: item.code, provider: item.provider });
      if (source) choices.set(sourceKey(source), source);
    }
    for (const item of instruments) {
      const source = item.adjustment === "raw_unadjusted" && item.frequency === "1d"
        && comparisonSource({ kind: "equity", code: item.symbol, provider: item.provider });
      if (source) choices.set(sourceKey(source), source);
    }
    return [...choices.values()];
  }, [indexCatalog, instruments]);

  const comparisonNames = useMemo(() => ({ ...instrumentNames, ...Object.fromEntries(indexCatalog.map(item => [item.code, item.name])) }), [instrumentNames, indexCatalog]);

  function addComparison(source: ComparisonSource) {
    if (comparison.sources.some(item => sourceKey(item) === sourceKey(source))) {
      setComparisonNotice("");
      return true;
    }
    if (comparison.sources.length >= COMPARISON_LIMIT) {
      setComparisonNotice("Four comparison sources are selected.");
      return false;
    }
    setComparison(current => ({ ...current, sources: [...current.sources, source] }));
    setComparisonNotice("");
    return true;
  }

  function compareCurrent() {
    const source = comparisonSourceFromSelection(selection);
    if (!source || !comparisonChoices.some(item => sourceKey(item) === sourceKey(source))) {
      setComparisonNotice(selection.kind === "index" && selection.code === "VIX"
        ? "VIX measures volatility and is available only in individual Market tabs."
        : "Choose a supported instrument and its exact local provider.");
      return;
    }
    if (addComparison(source)) { setHistoricalDetails(false); onShowComparison(); }
  }

  function showHistoricalDetails() {
    if (!comparing) { setHistoricalDetails(false); return; }
    const source = comparison.sources.find(item => sourceKey(item) === detailKey) ?? comparison.sources[0];
    if (source) {
      const target: MarketSelection = source.kind === "index"
        ? { kind: "index", code: source.code }
        : { kind: "equity", symbol: source.code, provider: source.provider };
      const key = marketTabKey(target);
      if (workspace.tabs.some(tab => marketTabKey(tab.selection) === key)) onActivateTab(key);
      else if (workspace.tabs.length < MARKET_TAB_LIMIT) onChooseInstrument(target, "new");
      else { setComparisonNotice("Close a historical tab before opening another instrument."); return; }
    }
    setComparisonNotice("");
    setHistoricalDetails(true);
  }

  function addInstrument() {
    if (comparing) { setComparisonPickerOpen(true); return; }
    if (workspace.tabs.length >= MARKET_TAB_LIMIT) {
      setPickerNotice(true);
      return;
    }
    setInstrumentQuery("");
    setPendingInstrument(null);
    setActiveSuggestion(-1);
    if (addMode) instrumentInput.current?.focus();
    else {
      focusPicker.current = true;
      setAddMode(true);
    }
  }

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
  const filteredRows = sorted.filter(row => !tableFilter.trim() ||
    [row.session_date, row.open, row.high, row.low, row.close, String(row.volume)]
      .some(value => String(value).toLowerCase().includes(tableFilter.trim().toLowerCase())));
  const page = boundedPage(view.page, filteredRows.length);
  const visibleRows = filteredRows.slice(page * pageSize, (page + 1) * pageSize);
  const last = bars[bars.length - 1];
  const first = bars[0];
  const prior = bars.length > 1 ? bars[bars.length - 2] : null;
  const closeChange = last && prior ? Number(last.close) - Number(prior.close) : null;

  const selectPeriod = (selected: HistoryPeriod) => {
    setValidationError("");
    if (selected === "custom") {
      update({ period: selected, page: 0 });
      return;
    }
    const start = selected === "recent" ? "" : yearsAgo(selected === "1y" ? 1 : selected === "5y" ? 5 : 10);
    update({ period: selected, draftStart: start, draftEnd: "", start, end: "", page: 0 });
  };

  const applyRange = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (draftStart && draftEnd && draftStart > draftEnd) {
      setValidationError(active);
      notify(t("Correct the date range below."), "warning");
      return;
    }
    setValidationError("");
    update({ period: "custom", start: draftStart, end: draftEnd, page: 0 });
  };

  const sortBy = (column: SortColumn) => update({
    page: 0, sortColumn: column, descending: sortColumn === column ? !descending : true,
  });
  const sortLabel = (column: SortColumn, label: string) => t(label) + (sortColumn === column ? (descending ? " ↓" : " ↑") : "");
  const hasAnyInstrument = instruments.length > 0 || indexCatalog.some(index => index.sessions > 0);
  const pickerVisible = showSearch || addMode;
  const tableControls = {
    filter: tableFilter, page: view.page,
    onFilterChange: (filter: string) => update({ tableFilter: filter.slice(0, 80), page: 0 }),
    onPageChange: (page: number) => update({ page }),
  };

  return (
    <section className="space-y-2" aria-labelledby="markets-title">
      <PageHeader
        id="markets-title"
        breadcrumb={t("FinIntel / Markets")}
        title={t("Market Data Explorer")}
        subtitle={comparing ? t("Compare price levels from local historical observations.") : `${t("Historical daily prices stored in local SQLite. No live quotes.")} · ${t("Selected instrument")}: ${selectedLabel}`}
        actions={<>
          {!comparing && <Badge variant="secondary" aria-label={t("Selected instrument")}>{selectedLabel}</Badge>}
          <Badge variant="outline">{t("Local SQLite")}</Badge>
          <Button type="button" variant="secondary" size="sm" onClick={() => setReload((value) => value + 1)}><RefreshCw aria-hidden="true" /> {t("Reload local data")}</Button>
          <Button type="button" size="sm" onClick={onOpenAcquisition}><Database aria-hidden="true" /> {t("Data Acquisition")}</Button>
          {comparing && comparison.sources.length > 1 && <select aria-label={t("Historical detail instrument")} value={comparison.sources.some(item => sourceKey(item) === detailKey) ? detailKey : sourceKey(comparison.sources[0])}
            onChange={event => setDetailKey(event.target.value)} className="h-8 rounded-md border border-input bg-background px-2 text-xs">
            {comparison.sources.map(source => <option key={sourceKey(source)} value={sourceKey(source)}>{source.code} · {source.provider}</option>)}
          </select>}
          <Button type="button" variant="outline" size="sm" onClick={showHistoricalDetails}>{t(comparing ? "Historical price details" : "Price comparison")}</Button>
          {!comparing && <><Button type="button" variant="outline" size="sm" onClick={compareCurrent}>{t("Add to comparison")}</Button>
          <Button type="button" variant={pickerVisible ? "secondary" : "outline"} size="sm" aria-expanded={pickerVisible} aria-controls="market-search-panel" onClick={() => { setAddMode(false); update({ showSearch: !pickerVisible }); }}><SlidersHorizontal aria-hidden="true" /> {pickerVisible ? t("Hide filters") : t("Show filters")}</Button></>}
        </>}
      />
      {!comparing && <MarketTabs workspace={{ ...workspace, comparing: undefined }} panelId={panelId} onCompare={() => { setComparisonNotice(""); setHistoricalDetails(false); onShowComparison(); }}
        onActivate={key => { setComparisonPickerOpen(false); setPickerNotice(false); setComparisonNotice(""); onActivateTab(key); }}
        onClose={key => { setPickerNotice(false); onCloseTab(key); }} onAdd={addInstrument} describe={tab => {
          const item = tab.selection;
          if (item.kind === "index") {
            const name = indexCatalog.find(index => index.code === item.code)?.name;
            return `${name ? `${name} (${item.code})` : item.code} · FRED`;
          }
          const name = instrumentNames[item.symbol];
          return `${name ? `${name} (${item.symbol})` : item.symbol} · ${providerName(item.provider)}`;
        }} />}
      {(limitReached || pickerNotice) && <p role="status" className="px-1 text-xs text-muted-foreground">{t("Eight instrument tabs are open. Close one to open another.")}</p>}
      {comparisonNotice && <div role="status" className="flex items-center gap-2 px-1 text-xs text-muted-foreground">{t(comparisonNotice)}
        {comparing && comparisonNotice === "Close a historical tab before opening another instrument." && <Button size="sm" variant="outline" onClick={() => { setComparisonNotice(""); setHistoricalDetails(true); }}>{t("Manage historical tabs")}</Button>}
      </div>}
      {pendingComparisonSource && <dialog ref={replacementDialog} aria-label={t("Replace comparison instrument")}
        className="fixed inset-0 m-auto w-[min(28rem,90vw)] rounded-lg border bg-background p-4 text-foreground shadow-xl backdrop:bg-black/50"
        onCancel={event => { event.preventDefault(); onResolvePendingComparison?.(); }}
        onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); onResolvePendingComparison?.(); } }}>
        <div className="space-y-3">
          <h3 className="font-semibold">{t("Replace comparison instrument")}</h3>
          <p className="text-sm">{t("Four comparison sources are selected.")} {t("Choose an instrument to replace with")} {pendingComparisonSource.code} · {pendingComparisonSource.provider === "fred" ? "FRED" : providerName(pendingComparisonSource.provider)}.</p>
          <label className="grid gap-1 text-sm">{t("Instrument to replace")}
            <select value={replacementKey} onChange={event => setReplacementKey(event.target.value)} className="h-9 rounded-md border border-input bg-background px-2">
              <option value="">{t("Select an instrument")}</option>
              {comparison.sources.map(source => <option key={sourceKey(source)} value={sourceKey(source)}>{source.code} · {source.provider}</option>)}
            </select>
          </label>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => onResolvePendingComparison?.()}>{t("Cancel")}</Button>
            <Button disabled={!comparison.sources.some(source => sourceKey(source) === replacementKey)} onClick={() => {
              setComparison(current => ({ ...current, sources: current.sources.map(source => sourceKey(source) === replacementKey ? pendingComparisonSource : source),
                hidden: current.hidden.filter(key => key !== replacementKey) }));
              onResolvePendingComparison?.();
            }}>{t("Replace instrument")}</Button>
          </div>
        </div>
      </dialog>}
      {comparing ? <div className="min-w-0 space-y-2">
        <div className="flex min-w-0 items-center gap-1 border-b border-border">
          <div role="tablist" aria-label={t("Comparison charts")} className="flex min-w-0 gap-1 overflow-x-auto">
            {comparisonWorkspace.tabs.map((tab, index) => <div key={tab.id} className={`flex shrink-0 items-center rounded-t-md ${tab.id === activeComparisonId ? "bg-secondary" : ""}`}>
              <Button type="button" role="tab" id={`${panelId}-${tab.id}`} aria-controls={`${panelId}-comparison-panel`}
                aria-selected={tab.id === activeComparisonId} tabIndex={tab.id === activeComparisonId ? 0 : -1}
                size="sm" variant="ghost" onClick={() => { setComparisonPickerOpen(false); setComparisonNotice(""); setDetailKey(""); setComparisonWorkspace(current => ({ ...current, active: tab.id })); }}
                onKeyDown={event => {
                  const tabs = comparisonWorkspace.tabs;
                  const next = event.key === "ArrowRight" ? (index + 1) % tabs.length : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : -1;
                  if (next < 0) return;
                  event.preventDefault();
                  setComparisonPickerOpen(false); setComparisonNotice(""); setDetailKey("");
                  setComparisonWorkspace(current => ({ ...current, active: tabs[next].id }));
                  document.getElementById(`${panelId}-${tabs[next].id}`)?.focus();
                }}>
                {t("Chart")} {tab.id.slice(6)}{tab.settings.sources.length > 0 && <span className="text-xs text-muted-foreground">{tab.settings.sources.map(source => source.code).join(" · ")}</span>}
              </Button>
              {comparisonWorkspace.tabs.length > 1 && <Button type="button" size="icon-sm" variant="ghost" aria-label={`${t("Close chart")} ${tab.id.slice(6)}`}
                onClick={() => { setComparisonPickerOpen(false); setComparisonNotice(""); setDetailKey(""); setComparisonWorkspace(current => closeComparisonTab(current, tab.id)); }}>×</Button>}
            </div>)}
          </div>
          <Button type="button" variant="ghost" size="icon-sm" className="shrink-0" disabled={comparisonWorkspace.tabs.length >= COMPARISON_TAB_LIMIT}
            aria-label={t("Add chart tab")} title={t("Add chart tab")} onClick={() => {
              setComparisonNotice(""); setDetailKey(""); setComparisonWorkspace(addComparisonTab); setComparisonPickerOpen(true);
            }}>+</Button>
        </div>
        <div id={`${panelId}-comparison-panel`} role="tabpanel" aria-labelledby={`${panelId}-${activeComparisonId}`} className="min-w-0">
        <MarketComparisonPanel key={activeComparisonId} settings={comparison} choices={comparisonChoices} directoryLoading={indexState === "loading"}
          directoryFailed={indexState === "error"} refreshRevision={reload} onChange={setComparison} pickerOpen={comparisonPickerOpen} onPickerOpen={setComparisonPickerOpen} names={comparisonNames} onOpenAcquisition={onOpenAcquisition} />
        </div>
      </div> : <div id={panelId} role="tabpanel" aria-labelledby={`${panelId}-${active}`} className="min-w-0 space-y-2">
      {indexState === "loading" && <Card><CardContent><Skeleton className="h-8 w-64" aria-label={t("Loading local instruments")} /></CardContent></Card>}
      {indexState === "error" && <Card role="alert"><CardHeader><CardTitle>{t("Local database unavailable")}</CardTitle></CardHeader><CardContent className="space-y-3 text-sm text-muted-foreground"><p>{indexError || directoryError}</p><p>{t("Start the Python service on 127.0.0.1:8765 and try again.")}</p><Button onClick={() => setReload((value) => value + 1)} variant="secondary">{t("Retry")}</Button></CardContent></Card>}
      {indexState === "ready" && !hasAnyInstrument && <Card><CardHeader><CardTitle>{t("No stored market data yet")}</CardTitle><CardDescription>{t("FinIntel reads your local SQLite database. Open Data Acquisition to fetch and store your first stock or ETF, or use Index History to import an index.")}</CardDescription></CardHeader><CardContent className="space-y-3"><div className="flex items-center gap-2 text-muted-foreground"><Database aria-hidden="true" className="size-5" /><span className="text-sm">{t("No price series have been imported.")}</span></div><Button type="button" onClick={onOpenAcquisition}><Database aria-hidden="true" /> {t("Open Data Acquisition")}</Button></CardContent></Card>}
      {indexState === "ready" && hasAnyInstrument && (
        <>
          {indexError && <p role="status" className="text-xs text-muted-foreground">{t("Index history service unavailable. Stock and ETF history remains available.")} · {indexError}</p>}
          {pickerVisible && <div id="market-search-panel"><Card><CardHeader><CardTitle>{t("Instrument & time range")}</CardTitle><CardAction><Badge variant="secondary">{instruments.length + indexCatalog.filter(index => index.sessions > 0).length} {t("local instruments")}</Badge></CardAction></CardHeader><CardContent>
            <form onSubmit={applyRange} className="grid min-w-0 grid-cols-1 items-end gap-4 sm:grid-cols-2 2xl:grid-cols-[minmax(220px,1.7fr)_minmax(170px,1fr)_minmax(190px,1fr)_minmax(190px,1fr)]">
              <div className="grid min-w-0 gap-2 self-start text-sm font-medium text-muted-foreground">
                <label htmlFor={instrumentListId + "-input"}>{t("Instrument")}</label>
                <div className="relative min-w-0">
                  <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-3 size-4" />
                  <input ref={instrumentInput} id={instrumentListId + "-input"} role="combobox" aria-autocomplete="list"
                    aria-expanded={instrumentFocused && !!instrumentQuery.trim()} aria-controls={instrumentListId + "-list"}
                    aria-activedescendant={instrumentFocused && activeSuggestion >= 0 ? instrumentListId + "-option-" + activeSuggestion : undefined}
                    type="search" value={instrumentFocused || addMode || pendingInstrument ? instrumentQuery : selectedLabel}
                    onChange={event => { setInstrumentQuery(event.target.value); setPendingInstrument(null); setActiveSuggestion(-1); }}
                    onFocus={() => setInstrumentFocused(true)}
                    onBlur={() => { setInstrumentFocused(false); setActiveSuggestion(-1); }}
                    onKeyDown={event => {
                      if (event.key === "Escape") event.currentTarget.blur();
                      else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                        event.preventDefault();
                        const delta = event.key === "ArrowDown" ? 1 : -1;
                        setActiveSuggestion(previous => matchingInstruments.length
                          ? Math.max(0, Math.min(previous + delta, matchingInstruments.length - 1)) : -1);
                      } else if (event.key === "Enter") {
                        event.preventDefault();
                        const match = matchingInstruments[activeSuggestion] ?? matchingInstruments[0];
                        if (match) stageInstrument(match);
                      }
                    }}
                    placeholder={t("Search ticker, company, ETF or index")} maxLength={80} autoComplete="off"
                    className="h-12 w-full min-w-0 rounded-md border border-input bg-muted pl-9 pr-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
                  {instrumentFocused && instrumentQuery.trim() && <div id={instrumentListId + "-list"} role="listbox"
                    aria-label={t("Stored instrument matches")} className="absolute inset-x-0 top-full z-30 mt-1 max-h-72 overflow-y-auto rounded-md border border-border bg-card p-1 shadow-lg">
                    {matchingInstruments.length ? matchingInstruments.map((item, index) => {
                      const code = item.kind === "index" ? item.item.code : item.item.symbol;
                      return <button key={item.kind === "index" ? code : code + "|" + item.item.provider}
                        id={instrumentListId + "-option-" + index} type="button" role="option" tabIndex={-1}
                        aria-selected={activeSuggestion === index} onMouseDown={event => event.preventDefault()}
                        onClick={() => stageInstrument(item)}
                        className={`flex w-full min-w-0 items-center gap-2 rounded px-3 py-2 text-left text-sm hover:bg-accent ${activeSuggestion === index ? "bg-accent" : ""}`}>
                        <span className="font-semibold text-foreground">{code}</span>
                        <span className="min-w-0 flex-1 truncate">{item.kind === "index" ? item.item.name : instrumentNames[code]}</span>
                        <span className="shrink-0 text-[11px]">{item.kind === "index" ? "FRED" : providerName(item.item.provider)}</span>
                      </button>;
                    }) : <p className="px-3 py-2 text-xs">{t("No stored instruments match. Try a different ticker or use Data Acquisition.")}</p>}
                  </div>}
                </div>
                {pendingInstrument && <div className="flex flex-wrap gap-1">
                  <Button type="button" size="sm" variant={addMode ? "outline" : "secondary"} onClick={() => openInstrument("current")}>{t("Open in Current Tab")}</Button>
                  <Button type="button" size="sm" variant={addMode ? "secondary" : "outline"} onClick={() => openInstrument("new")}>{t("Open in New Tab")}</Button>
                </div>}
              </div>
              <label className="grid min-w-0 gap-2 self-start text-sm font-medium text-muted-foreground">{t("Display period")}<select aria-label={t("Historical time range")} value={period} onChange={(event) => selectPeriod(event.target.value as HistoryPeriod)} className="h-12 w-full min-w-0 rounded-md border border-input bg-muted px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><option value="recent">{t("Latest 100 sessions")}</option><option value="1y">{t("Last 1 year")}</option><option value="5y">{t("Last 5 years")}</option><option value="10y">{t("Last 10 years")}</option><option value="custom">{t("Custom dates")}</option></select></label>
              <MarketDatePicker label={t("From (optional)")} value={draftStart} onChange={draftStart => update({ draftStart })} /><MarketDatePicker label={t("To (optional)")} value={draftEnd} onChange={draftEnd => update({ draftEnd })} />
              <div className="flex min-w-0 flex-wrap items-center justify-between gap-3 sm:col-span-2 2xl:col-span-4"><p className="min-w-0 flex-1 text-xs leading-5 text-muted-foreground">{t("Select a stored instrument, then choose where to open it.")}</p><div className="flex shrink-0 flex-wrap items-center gap-2"><Button type="submit" size="lg">{t("Apply range")}</Button><Button type="button" size="lg" variant="ghost" onClick={() => { update({ draftStart: "", draftEnd: "", start: "", end: "", period: "recent", page: 0 }); setValidationError(""); }}>{t("Clear")}</Button></div></div>
            </form>
            {validationError === active && <p role="alert" className="mt-3 text-sm text-destructive">{t("Start date must not exceed end date.")}</p>}
          </CardContent></Card></div>}
          {!currentHistory && <Card><CardContent className="space-y-4"><Skeleton className="h-7 w-44" aria-label={t("Loading historical prices")} /><Skeleton className="h-72 w-full" /></CardContent></Card>}
          {currentHistory?.kind === "error" && <Card role="alert"><CardContent className="space-y-3"><p className="flex items-center gap-2 text-sm text-destructive"><AlertCircle aria-hidden="true" className="size-4" />{t("Could not load stored prices for")} {selectedLabel}.</p><p className="text-xs text-muted-foreground">{t("Check that this instrument and provider have local history.")}</p><Button variant="secondary" onClick={() => setReload((value) => value + 1)}>{t("Retry local query")}</Button><Button variant="outline" onClick={onOpenAcquisition}>{t("Open Data Acquisition")}</Button></CardContent></Card>}
          {indexHistory && <IndexMarketContent history={indexHistory} selectedLabel={selectedLabel} period={period} onOpenAcquisition={onOpenAcquisition} tableControls={tableControls} sort={{ column: sortColumn, descending, select: sortBy, label: sortLabel }} t={t} />}
          {series && <EquityMarketContent bars={bars} series={series} selectedLabel={selectedLabel} period={period} first={first} last={last} closeChange={closeChange} sorted={sorted} visibleRows={visibleRows} page={page} totalRows={filteredRows.length} sortBy={sortBy} sortLabel={sortLabel} setPage={tableControls.onPageChange} tableFilter={tableFilter} setTableFilter={tableControls.onFilterChange} t={t} />}
        </>
      )}
      </div>}
      <footer className="text-xs text-muted-foreground">{t("Local research workstation · Charts display local SQLite data only · Fetches and action history live in Data Acquisition · Not a live market feed")}</footer>
    </section>
  );
}

function IndexMarketContent({ history, selectedLabel, period, onOpenAcquisition, tableControls, sort, t }: { history: IndexHistory; selectedLabel: string; period: HistoryPeriod; onOpenAcquisition: () => void; tableControls: { filter: string; page: number; onFilterChange: (filter: string) => void; onPageChange: (page: number) => void };
  sort: { column: SortColumn; descending: boolean; select: (column: SortColumn) => void; label: (column: SortColumn, label: string) => string };
  t: (key: string) => string }) {
  const observations = history.observations;
  const latest = observations.at(-1);
  const previous = observations.at(-2);
  const pointChange = latest && previous ? Number(latest.close) - Number(previous.close) : null;
  const percentChange = latest && previous ? (Number(latest.close) / Number(previous.close) - 1) * 100 : null;
  const sorted = [...observations].sort((left, right) => {
    const order = sort.column === "close" ? Number(left.close) - Number(right.close) : left.session_date.localeCompare(right.session_date);
    return sort.descending ? -order : order;
  });
  if (observations.length === 0) return <Card><CardHeader><CardTitle>{t("No local history for")} {selectedLabel}</CardTitle><CardDescription>{t("This index has no locally stored closing levels. Open Data Acquisition and use Index History to fetch it explicitly.")}</CardDescription></CardHeader><CardContent><Button type="button" onClick={onOpenAcquisition}><Database aria-hidden="true" /> {t("Open Data Acquisition")}</Button></CardContent></Card>;
  return <>
    <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4"><Metric label={t("Selected index")} value={selectedLabel} note={history.provider + " · " + formatInteger(observations.length) + " " + t("loaded sessions")} /><Metric label={t("Latest close")} value={price(latest!.close)} note={latest!.session_date} /><Metric label={t("Index-point change")} value={pointChange === null ? "—" : (pointChange >= 0 ? "+" : "") + price(String(pointChange))} note={percentChange === null ? t("Compared with prior loaded session") : `${percentChange >= 0 ? "+" : ""}${percentChange.toFixed(2)}% ${t("from prior observation")}`} /><Metric label={t("Latest available observation")} value={latest!.session_date} note={t("Local close history")} /></div>
    {period !== "recent" && <p role="status" className="text-xs text-muted-foreground">{t("Index periods use locally stored closing levels only; this selector never downloads data automatically.")}</p>}
    <Card><CardHeader><CardTitle><span className="flex items-center gap-2"><BarChart3 aria-hidden="true" className="size-5" /> {t("Index closing-level history")}</span></CardTitle><CardAction><Badge variant="outline">{t("FRED · daily close · index points")}</Badge></CardAction><CardDescription className="mt-2">{observations[0].session_date} – {latest!.session_date} · {t("actual stored observations")}</CardDescription></CardHeader><CardContent><IndexLineChart observations={observations} /></CardContent></Card>
    <Card><CardHeader><CardTitle>{t("Historical index table")}</CardTitle><CardAction><Badge variant="secondary">{formatInteger(observations.length)} {t("loaded rows")}</Badge></CardAction><CardDescription className="mt-2">{t("Date and closing level from local SQLite. No synthetic OHLC or volume values are generated.")}</CardDescription></CardHeader><CardContent><LocalDataTable rows={sorted} label={t("Historical index table")} controls={tableControls}
      searchable={row => [row.session_date, row.close].join(" ")}>{visible =>
      <div className="overflow-x-auto rounded-md border border-border"><table className="w-full min-w-[420px] border-collapse text-right text-xs tabular-nums"><thead className="bg-muted text-muted-foreground"><tr><th scope="col" className="px-3 py-3 text-left font-medium" aria-sort={sort.column === "session_date" ? sort.descending ? "descending" : "ascending" : undefined}>
        <button type="button" onClick={() => sort.select("session_date")}>{sort.label("session_date", "Date")}</button>
      </th><th scope="col" className="px-3 py-3 font-medium" aria-sort={sort.column === "close" ? sort.descending ? "descending" : "ascending" : undefined}>
        <button type="button" onClick={() => sort.select("close")}>{sort.label("close", "Close")}</button>
      </th></tr></thead><tbody>{visible.map((row) => <tr key={row.session_date} className="border-t border-border/80 hover:bg-muted/65"><td className="px-3 py-2.5 text-left font-medium text-foreground">{row.session_date}</td><td className="px-3 py-2.5 font-semibold">{price(row.close)}</td></tr>)}</tbody></table></div>}</LocalDataTable></CardContent></Card>
    <Card><CardHeader><CardTitle>{t("Source & provenance")}</CardTitle><CardAction><Badge variant="outline">{t("Recorded at import")}</Badge></CardAction></CardHeader><CardContent className="grid gap-3 text-xs text-muted-foreground sm:grid-cols-2"><p>{t("Provider:")} <span className="text-foreground">{history.provider}</span></p><p>{t("Data kind:")} <span className="text-foreground">{history.data_kind}</span></p><p>{t("Unit:")} <span className="text-foreground">{history.unit}</span></p><p>{t("Observations:")} <span className="text-foreground">{formatInteger(observations.length)}{history.has_more ? " (partial)" : ""}</span></p><p className="sm:col-span-2 break-all">{t("Source URL:")} <span className="text-foreground">{history.source_url}</span></p></CardContent></Card>
  </>;
}

function EquityMarketContent({ bars, series, selectedLabel, period, first, last, closeChange, sorted, visibleRows, page, totalRows, sortBy, sortLabel, setPage, tableFilter, setTableFilter, t }: { bars: DailySeries["observations"]; series: DailySeries; selectedLabel: string; period: HistoryPeriod; first: DailySeries["observations"][number] | undefined; last: DailySeries["observations"][number] | undefined; closeChange: number | null; sorted: DailySeries["observations"]; visibleRows: DailySeries["observations"]; page: number; totalRows: number; sortBy: (column: SortColumn) => void; sortLabel: (column: SortColumn, label: string) => string; setPage: (value: number) => void; tableFilter: string; setTableFilter: (value: string) => void; t: (key: string) => string }) {
  return <>
    <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4"><Metric label={t("Instrument")} value={selectedLabel} note={series.provider + " · " + formatInteger(bars.length) + " " + t("loaded sessions")} /><Metric label={t("Last close")} value={last ? price(last.close) : "—"} note={last?.session_date ?? t("No trading sessions")} /><Metric label={t("Daily change")} value={closeChange === null ? "—" : (closeChange >= 0 ? "+" : "") + price(String(closeChange))} note={t("Compared with prior loaded session")} /><Metric label={t("Loaded observations")} value={formatInteger(bars.length)} note={t(series.has_more ? "Bounded result · more stored" : "Available stored data")} /></div>
    {period !== "recent" && bars.length > 0 && bars.length < (period === "10y" ? 1200 : period === "5y" ? 600 : 120) && !series.has_more && <p role="status" className="text-xs text-muted-foreground">{t("Only")} {formatInteger(bars.length)} {t("sessions are stored for this selection. More history may require a premium full-history import, or the instrument may have a shorter trading history. This selector never downloads data automatically.")}</p>}
    {series.has_more && <p role="status" className="text-xs text-muted-foreground">{t("Display is limited to 4,000 latest records. Use date filters to examine older stored sessions.")}</p>}
    <TechnicalIndicatorsCard bars={bars} />
    <Card><CardHeader><CardTitle><span className="flex items-center gap-2"><BarChart3 aria-hidden="true" className="size-5" /> {t("OHLCV history")}</span></CardTitle><CardAction><Badge variant="outline">{t(series.price_adjustment.replaceAll("_", " "))}</Badge></CardAction><CardDescription className="mt-2">{first && last ? first.session_date + " – " + last.session_date : t("No sessions in selected range")} · {t("historical end-of-day data")}</CardDescription></CardHeader><CardContent><MarketCharts bars={bars} period={period} /></CardContent></Card>
    <Card><CardHeader><CardTitle>{t("Historical price table")}</CardTitle><CardAction><Badge variant="secondary">{formatInteger(sorted.length)} {t("loaded rows")}</Badge></CardAction><CardDescription className="mt-2">{t("Sortable local data. Prices are displayed without assuming a currency.")}</CardDescription></CardHeader><CardContent className="space-y-4"><label className="flex max-w-md flex-col gap-1 text-xs text-muted-foreground">{t("Filter table")} — {t("Historical price table")}<input type="search" value={tableFilter} onChange={event => setTableFilter(event.target.value)} aria-label={t("Filter table") + " — " + t("Historical price table")} placeholder={t("Search table values")} className="h-9 rounded-md border border-input bg-background px-3 text-sm text-foreground" /></label><div className="overflow-x-auto rounded-md border border-border"><table className="w-full min-w-[690px] border-collapse text-right text-xs tabular-nums"><thead className="bg-muted text-muted-foreground"><tr><th scope="col" className="px-3 py-3 text-left"><button type="button" onClick={() => sortBy("session_date")} className="font-medium hover:text-foreground">{sortLabel("session_date", "Session")}</button></th><th scope="col" className="px-3 py-3 font-medium">{t("Open")}</th><th scope="col" className="px-3 py-3 font-medium">{t("High")}</th><th scope="col" className="px-3 py-3 font-medium">{t("Low")}</th><th scope="col" className="px-3 py-3"><button type="button" onClick={() => sortBy("close")} className="font-medium hover:text-foreground">{sortLabel("close", "Close")}</button></th><th scope="col" className="px-3 py-3"><button type="button" onClick={() => sortBy("volume")} className="font-medium hover:text-foreground">{sortLabel("volume", "Volume")}</button></th></tr></thead><tbody>{visibleRows.map((bar) => <tr key={bar.session_date} className="border-t border-border/80 hover:bg-muted/65"><td className="px-3 py-2.5 text-left font-medium text-foreground">{bar.session_date}</td><td className="px-3 py-2.5">{price(bar.open)}</td><td className="px-3 py-2.5">{price(bar.high)}</td><td className="px-3 py-2.5">{price(bar.low)}</td><td className="px-3 py-2.5 font-semibold">{price(bar.close)}</td><td className="px-3 py-2.5">{formatInteger(bar.volume)}</td></tr>)}</tbody></table>{visibleRows.length === 0 && <p className="p-8 text-center text-sm text-muted-foreground">{t("No observations in this date range.")}</p>}</div><TablePager page={page} total={totalRows} onPageChange={setPage} /></CardContent></Card>
    <Card><CardHeader><CardTitle>{t("Source & provenance")}</CardTitle><CardAction><Badge variant="outline">{t("Recorded at import")}</Badge></CardAction></CardHeader><CardContent className="grid gap-3 text-xs text-muted-foreground sm:grid-cols-2"><p>{t("Provider:")} <span className="text-foreground">{series.provider}</span></p><p>{t("Frequency:")} <span className="text-foreground">{series.frequency}</span></p><p>{t("Data kind:")} <span className="text-foreground">{series.data_kind}</span></p><p>{t("Retrieved at:")} <span className="text-foreground">{series.retrieved_at ?? t("Unavailable")}</span></p><p>{t("Provider last refreshed:")} <span className="text-foreground">{series.provider_last_refreshed ?? t("Unavailable")}</span></p><p>{t("Stored sessions:")} <span className="text-foreground">{formatInteger(bars.length) + (series.has_more ? " (partial)" : "")}</span></p><p className="sm:col-span-2 break-all">{t("Source URL:")} <span className="text-foreground">{series.source_url ?? t("Unavailable")}</span></p></CardContent></Card>
  </>;
}

function Metric({ label, value, note }: { label: string; value: string; note: string }) {
  return <Card className="gap-2 py-4"><CardContent className="space-y-2"><p className="text-xs font-medium text-muted-foreground">{label}</p><p className="break-words text-2xl font-semibold tabular-nums">{value}</p><p className="text-[11px] leading-5 text-muted-foreground">{note}</p></CardContent></Card>;
}
