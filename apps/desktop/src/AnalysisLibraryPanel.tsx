import { useLayoutEffect, useRef, useState, type RefObject } from "react";
import { Tabs } from "@base-ui/react/tabs";
import { isTauri } from "@tauri-apps/api/core";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import contracts from "../../../contracts/analysis-jobs.json";
import { MarketDatePicker } from "./components/MarketDatePicker";
import { AnalysisJobDetail } from "./AnalysisJobDetail";
import {
  queueAnalysisJob, type AnalysisJobSummary, type AnalysisModelId, type AnalysisQualityPolicy, type AnalysisSourceCode,
} from "./analysis-jobs-api";
import {
  analysisErrorMessage, analysisRequest, commonSessions, FAMILIES, HISTORY_LIMIT, inspectSources,
  isAnalysisSource, minimumHistory, MODELS, parameterDefaults, parameterRules, PARAMETERS,
  SOURCE_CODES, SOURCE_NAMES, validateParameters,
} from "./analysis-workflow";
import type { IndexCode, IndexCatalogRecord } from "./index-history-api";
import type { AnalyticsResult } from "./index-intelligence-api";
import { useLocale } from "./localization";

const field = "grid min-w-0 gap-1.5 text-sm font-medium";
const input = "h-9 w-full min-w-0 rounded-md border border-input bg-background px-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
export const tabList = "flex flex-wrap gap-1 border-b";
export const tab = "border-b-2 border-transparent px-3 py-2 text-sm text-muted-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring data-[active]:border-primary data-[active]:font-semibold data-[active]:text-foreground";

type Family = keyof typeof FAMILIES;
type Draft = {
  sources: AnalysisSourceCode[];
  start: string;
  end: string;
  quality: AnalysisQualityPolicy;
  parameters: Record<string, string>;
};
type PanelProps = {
  primary: IndexCode;
  start: string;
  end: string;
  catalog: IndexCatalogRecord[];
  catalogLoaded: boolean;
  catalogUnavailable?: boolean;
  onOpenAcquisition?: () => void;
  onOpenJob?: (id: string) => void;
};

function validDate(value: string) {
  const date = new Date(value + "T00:00:00Z");
  return /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(date.getTime()) &&
    date.toISOString().slice(0, 10) === value;
}

function shiftYears(value: string, years: number) {
  const date = new Date(value + "T00:00:00Z");
  const month = date.getUTCMonth();
  date.setUTCFullYear(date.getUTCFullYear() - years);
  if (date.getUTCMonth() !== month) date.setUTCDate(0);
  return date.toISOString().slice(0, 10);
}

export function AnalysisLibraryPanel(props: PanelProps) {
  const { t } = useLocale();
  const generation = useRef(0);
  const [family, setFamily] = useState<Family>("Trend & Momentum");
  const [model, setModel] = useState<AnalysisModelId>("builtin.ema_trend");
  return <Card className="min-w-0 gap-3">
    <CardHeader>
      <CardTitle>{t("Analysis Library")}</CardTitle>
      <CardDescription>{t("Configure model inputs, check local sources, and queue historical research.")}</CardDescription>
    </CardHeader>
    <CardContent className="space-y-3">
      <Tabs.Root value={family} onValueChange={(value: Family) => {
        if (family === value) return;
        generation.current++;
        setFamily(value);
        setModel(FAMILIES[value][0]);
      }}>
        <Tabs.List className={tabList} aria-label={t("Analysis model family")}>
          {(Object.keys(FAMILIES) as Family[]).map(key => <Tabs.Tab key={key} className={tab} value={key}>{t(key)}</Tabs.Tab>)}
        </Tabs.List>
        <Tabs.Panel value={family} className="space-y-3 pt-3">
          <label className={field}>{t("Analysis model")}
            <select className={input} value={model} onChange={event => {
              if (event.target.value === model) return;
              generation.current++;
              setModel(event.target.value as AnalysisModelId);
            }}>
              {FAMILIES[family].map(id => <option key={id} value={id}>
                {t(MODELS[id][0])} · {contracts[id].version}
              </option>)}
            </select>
          </label>
          <AnalysisForm key={JSON.stringify([model, props.primary, props.start, props.end])} {...props} model={model} family={family} generation={generation} />
        </Tabs.Panel>
      </Tabs.Root>
    </CardContent>
  </Card>;
}

function AnalysisForm({ model, family, primary, start, end, catalog, catalogLoaded, catalogUnavailable, onOpenAcquisition, onOpenJob, generation }: PanelProps & {
  model: AnalysisModelId;
  family: Family;
  generation: RefObject<number>;
}) {
  const { t, language } = useLocale();
  const definition = contracts[model];
  const initialSource = isAnalysisSource(primary) ? [primary] : [];
  const [draft, setDraft] = useState<Draft>(() => ({
    sources: initialSource, start, end, quality: "require_complete", parameters: parameterDefaults(model),
  }));
  const rules = parameterRules(model);
  const { parameters, errors } = validateParameters(model, draft.parameters);
  const selected = draft.sources.map(code => catalog.find(entry => entry.code === code && entry.provider === "fred" && entry.series_type === "price"));
  const missing = catalogLoaded && selected.some(entry => !entry?.sessions || !entry.local_first_session || !entry.local_last_session);
  const datesValid = validDate(draft.start) && validDate(draft.end) && draft.start <= draft.end &&
    draft.end <= new Date().toISOString().slice(0, 10);
  const noRange = datesValid && catalogLoaded && !missing && selected.some(entry =>
    entry?.local_first_session && entry.local_last_session &&
    (draft.end < entry.local_first_session || draft.start > entry.local_last_session));
  const knownOverLimit = datesValid && selected.some(entry => entry?.local_first_session && entry.local_last_session &&
    draft.start <= entry.local_first_session && draft.end >= entry.local_last_session && entry.sessions > HISTORY_LIMIT);
  const enoughSources = draft.sources.length >= definition.min_sources;
  const canInspect = datesValid && enoughSources && !missing && !noRange && !knownOverLimit && catalogLoaded && !Object.keys(errors).length;
  const starts = selected.flatMap(entry => entry?.local_first_session ?? []).sort();
  const ends = selected.flatMap(entry => entry?.local_last_session ?? []).sort();
  const first = starts.length === selected.length ? starts[0] : undefined;
  const latest = ends.length === selected.length ? ends[0] : undefined;
  const last = ends.length === selected.length ? ends.at(-1) : undefined;
  const minimum = minimumHistory(model, parameters);
  const patch = (changes: Partial<Draft>) => {
    const next = { ...draft, ...changes };
    if (JSON.stringify(next) === JSON.stringify(draft)) return;
    generation.current++;
    setDraft(next);
  };
  const relative = model === "builtin.relative_strength";
  const risk = model === "builtin.risk_profile";
  const sourceLabel = (index: number) => index === 0 ? (relative ? "Benchmark" : "Primary source") : "Additional index";
  const replace = (index: number, code: string) => {
    const sources = [...draft.sources];
    if (isAnalysisSource(code)) sources[index] = code;
    else sources.splice(index, 1);
    patch({ sources });
  };
  const move = (index: number, delta: number) => {
    const sources = [...draft.sources];
    [sources[index], sources[index + delta]] = [sources[index + delta], sources[index]];
    patch({ sources });
  };
  const sourceRange = definition.min_sources === definition.max_sources
    ? String(definition.min_sources) : definition.min_sources + "–" + definition.max_sources;
  return <div className="space-y-3">
    <p className="text-sm text-muted-foreground">{t(MODELS[model][1])} · {t(family)} · {t("Required source count")} {sourceRange} · {t("Research only")}</p>
    <div className="grid gap-3 rounded-md border p-3 sm:grid-cols-2">
      {Array.from({ length: Math.min(definition.max_sources, draft.sources.length + 1) }, (_, index) =>
        <div key={index} className="flex min-w-0 flex-wrap items-end gap-2">
          <label className={field + " flex-1"}>{t(sourceLabel(index)) + (index > 0 ? " " + index : "")}
            <select className={input} value={draft.sources[index] ?? ""} onChange={event => replace(index, event.target.value)}>
              <option value="">{t("Select an index")}</option>
              {SOURCE_CODES.filter(code => code === draft.sources[index] || !draft.sources.includes(code)).map(code =>
                <option key={code} value={code}>{t(SOURCE_NAMES[code])} · {code}</option>)}
            </select>
          </label>
          {draft.sources[index] && index > 0 && <Button type="button" variant="outline" size="sm" onClick={() => move(index, -1)}
            aria-label={t("Move earlier") + " " + draft.sources[index]}>{t("Move earlier")}</Button>}
        </div>)}
      <p className="text-xs text-muted-foreground sm:col-span-2">{t(relative ? "The first index is the benchmark. The selected order is preserved." : risk
        ? "The first index is primary. Add a second index to enable historical correlation."
        : "Eligible sources are local FRED daily price indexes.")}</p>
      {!isAnalysisSource(primary) && <p role="status" className="text-sm sm:col-span-2">{t("The current index is unsupported for Analysis Library jobs. Select a FRED price index.")}</p>}
      {draft.sources.map((code, index) => {
        const record = selected[index];
        return <p key={code} className="text-xs text-muted-foreground">
          <strong className="text-foreground">{t(SOURCE_NAMES[code])} · FRED</strong><br />
          {record?.sessions ? <>{record.local_first_session} – {record.local_last_session} · {record.sessions.toLocaleString(language)} {t("local observations")}</>
            : t(catalogUnavailable ? "The local analysis service is unavailable." : catalogLoaded ? "No local observations for this index." : "Loading local source catalog…")}
        </p>;
      })}
      {noRange && <p role="status" className="text-sm sm:col-span-2">{t("No local observations in the requested window for one or more sources.")}</p>}
      {(missing || noRange) && onOpenAcquisition && <div className="sm:col-span-2"><Button type="button" variant="outline" onClick={onOpenAcquisition}>{t("Open Historical Data")}</Button></div>}
    </div>
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2" aria-label={t("Date presets")}>
        <Button type="button" size="sm" variant="outline" disabled={!latest} onClick={() => latest && patch({ start: shiftYears(latest, 1), end: latest })}>{t("1 year")}</Button>
        <Button type="button" size="sm" variant="outline" disabled={!latest} onClick={() => latest && patch({ start: shiftYears(latest, 5), end: latest })}>{t("5 years")}</Button>
        <Button type="button" size="sm" variant="outline" disabled={!first || !last} onClick={() => first && last && patch({ start: first, end: last })}>{t("All available data")}</Button>
        <Button type="button" size="sm" variant="outline" disabled={!latest} onClick={() => latest && patch({ end: latest })}>{t("Last available date")}</Button>
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        <MarketDatePicker label={t("Start date")} value={draft.start} onChange={value => patch({ start: value })} />
        <MarketDatePicker label={t("End date")} value={draft.end} onChange={value => patch({ end: value })} />
        <label className={field}>{t("Data quality policy")}
          <select className={input} value={draft.quality} onChange={event => patch({ quality: event.target.value as AnalysisQualityPolicy })}>
            <option value="require_complete">{t("require_complete")}</option>
            {definition.allows_unverified && <option value="allow_unverified">{t("allow_unverified")}</option>}
          </select>
        </label>
      </div>
      {!datesValid && <p role="status" className="text-sm text-destructive">{t("Select a valid start and end date.")}</p>}
      {draft.quality === "allow_unverified" && <p role="status" className="rounded-md border bg-muted p-2 text-sm">{t("Unverified evidence is allowed for this historical research.")}</p>}
    </div>
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      {Object.entries(rules).map(([name, rule]) => {
        const id = "analysis-parameter-" + name;
        const props = {
          className: input, value: draft.parameters[name], "aria-invalid": Boolean(errors[name]),
          "aria-describedby": errors[name] ? id : undefined,
        };
        const change = (value: string) => patch({ parameters: { ...draft.parameters, [name]: value } });
        return <label className={field} key={name}>{t(PARAMETERS[name])}
          {rule.enum ? <select {...props} onChange={event => change(event.target.value)}>
            {rule.enum.map(value => <option key={value} value={value}>{value}</option>)}
          </select> : <input {...props} inputMode={rule.pattern ? "decimal" : "numeric"} onChange={event => change(event.target.value)} />}
          {errors[name] && <span id={id} className="text-xs text-destructive">{t(errors[name])}</span>}
        </label>;
      })}
    </div>
    <div className="space-y-1 text-xs text-muted-foreground">
      {Number.isFinite(minimum) && <p>{t("Minimum shared observed sessions")} · {minimum.toLocaleString(language)}</p>}
      <p role={knownOverLimit ? "alert" : undefined} className={knownOverLimit ? "text-destructive" : undefined}>
        {t("Maximum 2,000 selected observations per source. Choose a shorter window if needed.")}
      </p>
      {model === "builtin.rsi_state" && !errors.window && <p>{t("Observed closes for the first RSI value")} · {Number(parameters.window) + 1}</p>}
      {risk && !Object.keys(errors).length && <p>{t("Observed closes for volatility")} · {Number(parameters.risk_window) + 1} · {t("Observed closes for VaR and ES")} · {Number(parameters.tail_window) + 1}. {t("Historical 95% tail estimates require the entire tail window.")}</p>}
      {model === "builtin.market_regime" && !Object.keys(errors).length && <p>{t("Observed closes for trend classification")} · {parameters.long_window} · {t("Observed closes for full volatility calibration")} · {Number(parameters.volatility_window) + Number(parameters.calibration_window) + 1}</p>}
    </div>
    {!enoughSources && <p role="status" className="text-sm">{t("Select the required number of distinct sources.")}</p>}
    <AnalysisPreview key={JSON.stringify([draft, canInspect])} model={model} draft={draft}
      parameters={parameters} minimum={minimum} enabled={canInspect} onOpenJob={onOpenJob} generation={generation} />
  </div>;
}

function AnalysisPreview({ model, draft, parameters, minimum, enabled, onOpenJob, generation }: {
  model: AnalysisModelId; draft: Draft; parameters: Record<string, number | string>; minimum: number;
  generation: RefObject<number>;
  enabled: boolean; onOpenJob?: (id: string) => void;
}) {
  const { t, language } = useLocale();
  const [preview, setPreview] = useState<AnalyticsResult[]>();
  const [job, setJob] = useState<AnalysisJobSummary>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const gate = useRef({ alive: false, pending: false });
  const native = isTauri();
  const window = { start: draft.start, end: draft.end };
  const shared = preview ? commonSessions(preview) : 0;
  const overLimit = preview?.some(item => item.observations > HISTORY_LIMIT);
  const unverified = preview?.some(item => !item.quality.quality_verified);
  const eligible = preview && !overLimit && shared >= minimum && (!unverified || draft.quality === "allow_unverified");
  useLayoutEffect(() => {
    const scope = { alive: true, pending: false };
    gate.current = scope;
    return () => { scope.alive = false; };
  }, []);

  function startAnotherAnalysis() {
    generation.current++;
    setJob(undefined);
    setPreview(undefined);
    setError("");
  }

  async function run(action: "preview" | "queue") {
    const scope = gate.current;
    if (scope.pending || !scope.alive || !enabled || !native) return;
    const revision = generation.current;
    const current = () => scope.alive && generation.current === revision;
    scope.pending = true;
    setBusy(true);
    setError("");
    try {
      if (action === "preview") {
        setPreview(undefined);
        const result = await inspectSources(draft.sources, window);
        if (current()) setPreview(result);
      } else if (eligible) {
        const queued = await queueAnalysisJob(analysisRequest(model, preview, window, draft.quality, parameters));
        if (current()) setJob(queued);
      }
    } catch (reason) {
      if (current()) {
        setError(analysisErrorMessage(reason));
        if (action === "queue") setPreview(undefined);
      }
    } finally {
      scope.pending = false;
      if (current()) setBusy(false);
    }
  }

  return <div className="space-y-3 border-t pt-3">
    {!native && <p role="status" className="text-sm">{t("Analysis Library jobs require the managed FinIntel desktop.")}</p>}
    <Button type="button" variant="outline" disabled={!enabled || !native || busy || Boolean(job)} onClick={() => void run("preview")}>{t("Validate local sources")}</Button>
    {busy && <p role="status" className="text-sm">{t("Checking local analysis inputs…")}</p>}
    {error && <p role="alert" className="text-sm text-destructive">{t(error)}</p>}
    {preview && <section aria-label={t("Review analysis inputs")} className="space-y-2 text-sm">
      <h3 className="font-semibold">{t("Review analysis inputs")}</h3>
      <p>{t(MODELS[model][0])} · {contracts[model].version} · {window.start} – {window.end} · {t(draft.quality)} · {t("Research only")}</p>
      <p>{Object.entries(parameters).map(([name, value]) => t(PARAMETERS[name]) + " " + value).join(" · ")}</p>
      <ol className="list-decimal space-y-1 pl-5">
        {preview.map(item => <li key={item.identity.code}>
          {item.identity.code} · FRED · {item.first_session ?? "—"} – {item.last_session ?? "—"} · {item.observations.toLocaleString(language)} {t("Observations")} · {t(item.quality.quality_verified ? "verified" : "unverified")}
          <details><summary className="cursor-pointer text-muted-foreground">{t("Source snapshot SHA-256")}</summary>
            <code className="select-all break-all">{item.source.snapshot_sha256}</code>
          </details>
        </li>)}
      </ol>
      <p>{t("Shared observed sessions")} · {shared.toLocaleString(language)}</p>
      {overLimit && <p role="alert">{t("Maximum 2,000 selected observations per source. Choose a shorter window if needed.")}</p>}
      {shared < minimum && <p role="alert">{t("Insufficient shared history. Extend the date range or select other local sources.")}</p>}
      {unverified && draft.quality === "require_complete" && <p role="alert">{t("Run a current, complete Data Quality scan for every selected source.")}</p>}
      <Button type="button" disabled={!eligible || busy || Boolean(job)} onClick={() => void run("queue")}>{t("Queue analysis job")}</Button>
    </section>}
    {job && <>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p role="status" className="text-sm">{t("Model job queued locally.")}</p>
        <Button type="button" variant="outline" onClick={startAnotherAnalysis}>{t("Run again")}</Button>
      </div>
      <AnalysisJobDetail id={job.id} initial={job} onOpenJob={onOpenJob} selectionGeneration={generation} />
    </>}
  </div>;
}
