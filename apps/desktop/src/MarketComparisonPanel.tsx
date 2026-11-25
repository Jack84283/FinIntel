import { useEffect, useMemo, useState } from "react";
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

export function MarketComparisonPanel({ settings, choices, directoryLoading, directoryFailed, refreshRevision, onChange, onAdd, onOpenAcquisition }: {
  settings: ComparisonPreferences;
  choices: ComparisonSource[];
  directoryLoading: boolean;
  directoryFailed: boolean;
  refreshRevision: number;
  onChange: (settings: ComparisonPreferences) => void;
  onAdd: (source: ComparisonSource) => void;
  onOpenAcquisition: () => void;
}) {
  const { t } = useLocale();
  const { sources, start, end, period, mode, hidden } = settings;
  const [query, setQuery] = useState("");
  const [candidate, setCandidate] = useState("");
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

  const selected = new Set(sources.map(sourceKey));
  const options = choices.filter(source => !selected.has(sourceKey(source))
    && comparisonLabel(source).toLowerCase().includes(query.trim().toLowerCase()));
  const additions = options.slice(0, 100);
  const addSource = additions.find(source => sourceKey(source) === candidate);
  const atLimit = sources.length === COMPARISON_LIMIT;
  const showVix = query.trim().toUpperCase() === "VIX";
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
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <label className="grid gap-1 text-xs text-muted-foreground">{t("Search local instruments")}
            <input type="search" value={query} onChange={event => { setQuery(event.target.value); setCandidate(""); }}
              maxLength={80} className="h-8 rounded-md border border-input bg-background px-2 text-foreground" />
          </label>
          <label className="grid min-w-0 gap-1 text-xs text-muted-foreground">{t("Local comparison source")}
            <select value={addSource ? candidate : ""} onChange={event => setCandidate(event.target.value)}
              className="h-8 max-w-full rounded-md border border-input bg-background px-2 text-foreground">
              <option value="">{t("Select a local instrument")}</option>
              {additions.map(source => <option key={sourceKey(source)} value={sourceKey(source)}>{comparisonLabel(source)}</option>)}
            </select>
          </label>
          <Button type="button" size="sm" disabled={!addSource || atLimit} onClick={() => {
            if (addSource) onAdd(addSource);
            setCandidate("");
            setQuery("");
          }}>{t("Add to comparison")}</Button>
          {atLimit && <span role="status" className="text-xs text-muted-foreground">{t("Four comparison sources are selected.")}</span>}
        </div>
        {directoryLoading && <p role="status" className="text-xs text-muted-foreground">{t("Loading local instruments")}</p>}
        {!directoryLoading && (directoryFailed || !choices.length || showVix || !additions.length || options.length > additions.length) &&
          <p role="status" className="text-xs text-muted-foreground">{t(directoryFailed ? errors.backend_unavailable
            : showVix ? errors.unsupported_vix : !choices.length ? "No stored market data yet"
              : !additions.length ? "No matching local comparison sources." : "Refine the search to see more local sources.")}</p>}
        <div className="flex flex-wrap items-end gap-2">
          <label className="grid gap-1 text-xs text-muted-foreground">{t("Comparison period")}
            <select value={period} onChange={event => pickPeriod(event.target.value as ComparisonPeriod)}
              className="h-8 rounded-md border border-input bg-background px-2 text-foreground">
              <option value="1y">{t("Last 1 year")}</option><option value="5y">{t("Last 5 years")}</option>
              <option value="10y">{t("Last 10 years")}</option><option value="custom">{t("Custom dates")}</option>
            </select>
          </label>
          {period === "custom" && <form className="flex flex-wrap items-end gap-2" onSubmit={event => {
            event.preventDefault();
            if (!validComparisonWindow(draft.start, draft.end)) { setDateError(true); return; }
            setDateError(false);
            onChange({ ...settings, ...draft });
          }}>
            <label className="grid gap-1 text-xs text-muted-foreground">{t("Start date")}
              <input type="date" required value={draft.start} onChange={event => setDraft({ ...draft, start: event.target.value })}
                className="h-8 rounded-md border border-input bg-background px-2 text-foreground" />
            </label>
            <label className="grid gap-1 text-xs text-muted-foreground">{t("End date")}
              <input type="date" required value={draft.end} onChange={event => setDraft({ ...draft, end: event.target.value })}
                className="h-8 rounded-md border border-input bg-background px-2 text-foreground" />
            </label>
            <Button size="sm" type="submit" variant="outline">{t("Apply range")}</Button>
          </form>}
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
