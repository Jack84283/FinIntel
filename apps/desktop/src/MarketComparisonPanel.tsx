import { useEffect, useMemo, useRef, useState } from "react";
import { Plus } from "lucide-react";
import { searchAcquirableSymbols } from "./symbol-catalog-api";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { MarketComparisonChart, comparisonLabel } from "./MarketComparisonChart";
import { useLocale } from "./localization";
import { comparisonError, fetchMarketComparison, sourceKey, validComparisonWindow,
  type ComparisonError, type ComparisonResult, type ComparisonSource,
} from "./market-comparison-api";
import { COMPARISON_LIMIT, presetWindow, type ComparisonPeriod, type ComparisonPreferences } from "./market-comparison-preferences";

const warnings = {
  price_level_only: "This comparison uses historical price levels. Returns may exclude dividends, and unadjusted prices can reflect splits.",
  mixed_semantics: "Raw equity prices and index levels use different source conventions. Index levels are not directly tradable.",
  quality_unverified: "The local quality scan does not verify every selected source.",
  coverage_gaps: "Coverage differs from the requested bounds or across sources. Missing observations remain gaps.",
} as const;
const errors: Record<ComparisonError, string> = {
  invalid_input: "Choose valid dates and two to four different sources.",
  invalid_source: "Choose a supported instrument and its exact local provider.",
  unsupported_vix: "VIX measures volatility and is available only in individual Market tabs.",
  missing_history: "A selected source has no local history in this window. Change the dates or open Data Acquisition.",
  insufficient_overlap: "The selected sources need at least two shared observation dates. Try different dates or sources.",
  incompatible_source: "A selected source uses unsupported price history. Choose raw daily equity prices or a supported price index.",
  invalid_close: "A stored close is invalid for price comparison. Check Data Validation.",
  resource_limit: "The comparison exceeds the supported history limit. Select a shorter window.",
  backend_unavailable: "Local comparison is unavailable. Check the backend and retry.",
  invalid_response: "The comparison response could not be verified. Retry the local query.",
};

export function MarketComparisonPanel({ settings, choices, directoryLoading, directoryFailed, refreshRevision, onChange, pickerOpen, onPickerOpen, names, onOpenAcquisition }: {
  settings: ComparisonPreferences;
  choices: ComparisonSource[];
  directoryLoading: boolean;
  directoryFailed: boolean;
  refreshRevision: number;
  onChange: (settings: ComparisonPreferences) => void;
  pickerOpen: boolean;
  onPickerOpen: (open: boolean) => void;
  names: Record<string, string>;
  onOpenAcquisition: () => void;
}) {
  const { t } = useLocale();
  const { sources, start, end, period, mode, hidden } = settings;
  const [query, setQuery] = useState("");
  const [pending, setPending] = useState<ComparisonSource[]>(sources);
  const [matches, setMatches] = useState<Record<string, string>>({});
  const [wasOpen, setWasOpen] = useState(false);
  if (wasOpen !== pickerOpen) {
    setWasOpen(pickerOpen);
    if (pickerOpen) { setPending(sources); setQuery(""); setMatches({}); }
  }
  const dialog = useRef<HTMLDialogElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const [draft, setDraft] = useState({ start, end });
  const [dateError, setDateError] = useState(false);
  const [retry, setRetry] = useState(0);
  const request = useMemo(() => ({ sources, start, end, retry, refreshRevision }), [sources, start, end, retry, refreshRevision]);
  const [state, setState] = useState<{ request: typeof request; result?: ComparisonResult; error?: ComparisonError }>();
  const current = state?.request === request ? state : undefined;

  useEffect(() => {
    if (request.sources.length < 2) return;
    const abort = new AbortController();
    void fetchMarketComparison(request.sources, request.start, request.end, abort.signal)
      .then(result => { if (!abort.signal.aborted) setState({ request, result }); })
      .catch(error => { if (!abort.signal.aborted) setState({ request, error: comparisonError(error) }); });
    return () => abort.abort();
  }, [request]);

  useEffect(() => {
    if (!pickerOpen) return;
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const node = dialog.current;
    if (node && typeof node.showModal === "function" && !node.open) node.showModal();
    searchInput.current?.focus();
    return () => {
      if (node?.open && typeof node.close === "function") node.close();
      opener.current?.focus();
    };
  }, [pickerOpen]);

  useEffect(() => {
    if (!pickerOpen || !query.trim()) return;
    let active = true;
    const timer = setTimeout(() => {
      void searchAcquirableSymbols(query.trim(), "All", 100).then(result => {
        if (active) setMatches(Object.fromEntries(result.results.map(item => [item.symbol, item.name])));
      }).catch(() => { /* Symbol/provider search remains available offline. */ });
    }, 150);
    return () => { active = false; clearTimeout(timer); };
  }, [pickerOpen, query]);

  function openPicker() {
    onPickerOpen(true);
  }
  function closePicker() {
    onPickerOpen(false);
  }

  // Mount a fresh draft each time the parent opens the picker.
  const selected = new Set(pending.map(sourceKey));
  const q = query.trim().toLowerCase();
  const options = choices.filter(source => [comparisonLabel(source), names[source.code], matches[source.code]]
    .some(value => value?.toLowerCase().includes(q)));
  const additions = options.slice(0, 100);
  const atLimit = sources.length >= COMPARISON_LIMIT;
  const showVix = query.trim().toUpperCase() === "VIX";
  const changeDraft = (next: { start: string; end: string }) => {
    setDraft(next);
    const valid = validComparisonWindow(next.start, next.end);
    setDateError(!valid);
    if (valid && (next.start !== start || next.end !== end)) onChange({ ...settings, ...next, period: "custom" });
  };
  const pickPeriod = (period: ComparisonPeriod) => {
    setDateError(false);
    if (period === "custom") {
      setDraft({ start, end });
      onChange({ ...settings, period });
      return;
    }
    const window = presetWindow(period);
    setDraft(window);
    onChange({ ...settings, ...window, period });
  };

  return <div className="min-w-0 space-y-2">
    <Card className="gap-2 py-3">
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold">{t("Compare local instruments")}</h2>
          {sources.length > 0 && <Button type="button" size="sm" variant="ghost"
            onClick={() => onChange({ ...settings, sources: [], hidden: [] })}>{t("Clear comparison")}</Button>}
        </div>
        <div className="flex flex-wrap gap-2">
          {sources.map(source => <Button key={sourceKey(source)} type="button" variant="outline" size="sm"
            aria-label={`${t("Remove from comparison")} · ${comparisonLabel(source)}`}
            onClick={() => onChange({ ...settings, sources: sources.filter(item => sourceKey(item) !== sourceKey(source)),
              hidden: hidden.filter(key => key !== sourceKey(source)) })}>
            {comparisonLabel(source)} <span aria-hidden="true">×</span>
          </Button>)}
          <Button type="button" variant="outline" size="sm" onClick={openPicker}>
            <Plus aria-hidden="true" /> {t("Add to comparison")}
          </Button>
          {atLimit && <span role="status" className="text-xs text-muted-foreground">{t("Four comparison sources are selected.")}</span>}
        </div>
        {pickerOpen && <dialog ref={dialog} aria-label={t("Select comparison instruments")}
          className="fixed inset-0 m-auto max-h-[85vh] w-[min(36rem,90vw)] overflow-auto rounded-lg border bg-background p-4 text-foreground shadow-xl backdrop:bg-black/50"
          onCancel={event => { event.preventDefault(); closePicker(); }}
          onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); closePicker(); } }}>
          <div className="space-y-3">
            <h3 className="text-base font-semibold">{t("Select comparison instruments")}</h3>
            <label className="grid gap-1 text-sm">{t("Search local instruments")}
              <input ref={searchInput} type="search" value={query} maxLength={80}
                onChange={event => { setQuery(event.target.value); setMatches({}); }}
                className="h-9 rounded-md border border-input bg-background px-2" />
            </label>
            <div className="flex flex-wrap gap-1">
              {pending.map(source => <Button key={sourceKey(source)} type="button" size="sm" variant="outline"
                onClick={() => setPending(items => items.filter(item => sourceKey(item) !== sourceKey(source)))}>
                {comparisonLabel(source)} ×
              </Button>)}
            </div>
            <div className="max-h-72 overflow-y-auto rounded-md border">
              {additions.map(source => <label key={sourceKey(source)} className="flex items-center gap-2 border-b px-3 py-2 text-sm last:border-b-0">
                <input type="checkbox" value={sourceKey(source)} checked={selected.has(sourceKey(source))}
                  disabled={!selected.has(sourceKey(source)) && pending.length >= COMPARISON_LIMIT}
                  onChange={event => setPending(items => event.target.checked
                    ? items.length < COMPARISON_LIMIT && !items.some(item => sourceKey(item) === sourceKey(source)) ? [...items, source] : items
                    : items.filter(item => sourceKey(item) !== sourceKey(source)))} />
                <span>{comparisonLabel(source)}</span>
                <span className="text-xs text-muted-foreground">{names[source.code] || matches[source.code]}</span>
              </label>)}
            </div>
            {directoryLoading && <p role="status" className="text-xs text-muted-foreground">{t("Loading local instruments")}</p>}
            {!directoryLoading && (directoryFailed || !choices.length || showVix || !additions.length || options.length > additions.length) &&
              <p role="status" className="text-xs text-muted-foreground">{t(directoryFailed ? errors.backend_unavailable
                : showVix ? errors.unsupported_vix : !choices.length ? "No stored market data yet"
                  : !additions.length ? "No matching local comparison sources." : "Refine the search to see more local sources.")}</p>}
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs text-muted-foreground">{pending.length} / {COMPARISON_LIMIT}</span>
              <div className="flex gap-2">
                <Button type="button" variant="outline" onClick={closePicker}>{t("Cancel")}</Button>
                <Button type="button" onClick={() => {
                  onChange({ ...settings, sources: pending, hidden: hidden.filter(key => selected.has(key)) });
                  closePicker();
                }}>{t("Apply selection")}</Button>
              </div>
            </div>
          </div>
        </dialog>}
        <div className="flex flex-wrap items-end gap-2">
          <label className="grid gap-1 text-xs text-muted-foreground">{t("Comparison period")}
            <select value={period} onChange={event => pickPeriod(event.target.value as ComparisonPeriod)}
              className="h-8 rounded-md border border-input bg-background px-2 text-foreground">
              <option value="1y">{t("Last 1 year")}</option><option value="5y">{t("Last 5 years")}</option>
              <option value="10y">{t("Last 10 years")}</option><option value="custom">{t("Custom dates")}</option>
            </select>
          </label>
          {period === "custom" && <div className="flex flex-wrap items-end gap-2">
            <label className="grid gap-1 text-xs text-muted-foreground">{t("Start date")}
              <input type="date" required value={draft.start} onChange={event => changeDraft({ ...draft, start: event.target.value })}
                className="h-8 rounded-md border border-input bg-background px-2 text-foreground" />
            </label>
            <label className="grid gap-1 text-xs text-muted-foreground">{t("End date")}
              <input type="date" required value={draft.end} onChange={event => changeDraft({ ...draft, end: event.target.value })}
                className="h-8 rounded-md border border-input bg-background px-2 text-foreground" />
            </label>
          </div>}
          <label className="grid gap-1 text-xs text-muted-foreground">{t("Comparison display")}
            <select value={mode} onChange={event => onChange({ ...settings, mode: event.target.value === "cumulative_return_pct"
              ? "cumulative_return_pct" : "normalized_100" })}
              className="h-8 rounded-md border border-input bg-background px-2 text-foreground">
              <option value="normalized_100">{t("Base 100")}</option>
              <option value="cumulative_return_pct">{t("Cumulative price return")}</option>
            </select>
          </label>
        </div>
        {dateError && <p role="alert" className="text-xs text-destructive">{t("Choose an ordered date window of up to ten years ending no later than today.")}</p>}
        <p className="text-xs text-muted-foreground">{t("Requested window")} · {start} – {end}</p>
        {sources.length < 2 && <p role="status" className="text-sm text-muted-foreground">{t("Select at least two local instruments to compare.")}</p>}
        {sources.length >= 2 && !current && <p role="status" className="text-sm text-muted-foreground">{t("Loading local comparison…")}</p>}
        {current?.error && <div role="alert" className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-destructive">{t(errors[current.error])}</span>
          <Button type="button" variant="outline" size="sm" onClick={() => setRetry(value => value + 1)}>{t("Retry local query")}</Button>
          {current.error === "missing_history" && <Button type="button" size="sm" variant="ghost" onClick={onOpenAcquisition}>{t("Open Data Acquisition")}</Button>}
        </div>}
      </CardContent>
    </Card>
    {current?.result && <ComparisonResultContent key={current.result.output_sha256} result={current.result}
      settings={settings} onChange={onChange} />}
  </div>;
}

function ComparisonResultContent({ result, settings, onChange }: {
  result: ComparisonResult; settings: ComparisonPreferences; onChange: (settings: ComparisonPreferences) => void;
}) {
  const { t } = useLocale();
  const { hidden, mode } = settings;
  const headers = ["Instrument", "Comparable price return", "Stored window", "Baseline close", "Common endpoint close",
    "Latest source close", "Observations", "Missing dates", "Nonshared dates", "Price semantics"];
  return <Card className="gap-2 py-3">
    <CardContent className="space-y-3">
      <div className="space-y-1 text-xs text-muted-foreground">
        <p>{t("Covered plot window")} · {result.baseline_date} – {result.points.at(-1)?.session_date}</p>
        <p>{t("Shared baseline and endpoint")} · {result.baseline_date} – {result.last_common_date} · {result.shared_sessions} {t("shared sessions")}</p>
        <ul className="space-y-1">{result.warnings.map(warning => <li key={warning}>{t(warnings[warning])}</li>)}</ul>
      </div>
      <div className="overflow-x-auto rounded-md border">
        <table aria-label={t("Comparison summary")} className="w-full min-w-max text-right text-xs tabular-nums">
          <thead className="bg-muted text-muted-foreground"><tr>{headers.map((header, i) =>
            <th scope="col" key={header} className={`px-3 py-2 ${i === 0 ? "text-left" : ""}`}>{t(header)}</th>)}</tr></thead>
          <tbody>{result.sources.map(source => <tr key={source.key} className="border-t">
            <th scope="row" className="px-3 py-2 text-left">{comparisonLabel(source.identity)}</th>
            {[source.common_end_return_pct + "%", source.first_date + " – " + source.latest_date, source.baseline_close,
              source.common_end_close, source.latest_close, source.observations, source.missing_sessions,
              source.nonshared_sessions, t(source.identity.kind === "index" ? "Price index · index points" : "Raw unadjusted price · USD per share")]
              .map((value, i) => <td key={headers[i + 1]} className="px-3 py-2">{value}</td>)}
          </tr>)}</tbody>
        </table>
      </div>
      <MarketComparisonChart result={result} mode={mode} hidden={hidden} onVisibility={key => onChange({ ...settings,
        hidden: hidden.includes(key) ? hidden.filter(item => item !== key) : [...hidden, key] })} />
      <details className="text-xs text-muted-foreground">
        <summary className="cursor-pointer font-medium focus-visible:ring-2 focus-visible:ring-ring">{t("Comparison source evidence")}</summary>
        <dl className="mt-2 grid gap-1 break-all">
          <dt>{t("Calculation revision")}</dt><dd>{result.calculation_revision}</dd>
          <dt>{t("Comparison fingerprint")}</dt><dd>{result.fingerprint_sha256}</dd>
          <dt>{t("Output SHA-256")}</dt><dd>{result.output_sha256}</dd>
          {result.sources.map(source => <div key={source.key}><dt>{comparisonLabel(source.identity)} · {t("Snapshot SHA-256")}</dt>
            <dd>{source.snapshot_sha256}</dd></div>)}
        </dl>
      </details>
    </CardContent>
  </Card>;
}
