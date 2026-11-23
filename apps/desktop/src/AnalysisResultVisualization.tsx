import { useState, type ReactNode } from "react";
import { LocalDataTable } from "./components/TablePagination";
import { useLocale } from "./localization";
import type { AnalysisModelId, AnalysisOutputField, AnalysisPoint, AnalysisResult } from "./analysis-jobs-api";
import { MODELS, PARAMETERS, SOURCE_NAMES } from "./analysis-workflow";
import { AnalysisSeriesChart, AnalysisStateTimeline } from "./AnalysisSeriesChart";
import {
  cell, fieldLabel, formatCell, observedDate, unitLabel, type Observation, type ResultColumn, type Translator,
} from "./analysis-result-display";

type ModelFields<M extends AnalysisModelId> = keyof AnalysisPoint<M> & string;
type SummaryItem = { label: string; value: ReactNode };
const selectClass = "h-9 rounded-md border border-input bg-background px-2 text-foreground focus-visible:ring-2 focus-visible:ring-ring";

function isModel<M extends AnalysisModelId>(result: AnalysisResult, model: M): result is Extract<AnalysisResult, { evidence: { model: { model_id: M } } }> {
  return result.evidence.model.model_id === model;
}

function columns(fields: readonly AnalysisOutputField[], names: readonly string[], t: Translator): ResultColumn[] {
  return names.flatMap(name => {
    const field = fields.find(item => item.name === name);
    return field ? [{ field, label: fieldLabel(name, t) }] : [];
  });
}

function AnalysisMetricTable({ rows, fields }: { rows: readonly Observation[]; fields: readonly ResultColumn[] }) {
  const { t, language } = useLocale();
  return <LocalDataTable rows={rows} label={t("Historical observations")}
    searchable={row => [row.session_date, ...fields.map(column => {
      const raw = cell(row, column.field.name);
      return [raw, formatCell(raw, column.field, language, t)].join(" ");
    })].join(" ")}>
    {visible => <div className="overflow-x-auto rounded-md border">
      <table aria-label={t("Historical observations")} className="w-full text-left text-xs">
        <thead className="bg-muted"><tr>
          <th scope="col" className="whitespace-nowrap p-2">{t("Observed session")}</th>
          {fields.map(({ field, label }) => <th key={field.name} scope="col" className="min-w-28 p-2 text-right">
            {label}
            {(field.kind === "decimal" || field.kind === "integer") &&
              <span className="block font-normal text-muted-foreground">{unitLabel(field.units, t)}</span>}
          </th>)}
        </tr></thead>
        <tbody>{visible.map(row => <tr key={row.session_date} className="border-t" data-session={row.session_date}>
          <th scope="row" className="whitespace-nowrap p-2 font-normal tabular-nums">{observedDate(row.session_date, language)}</th>
          {fields.map(({ field }) => {
            const value = cell(row, field.name);
            const raw = field.kind === "reason_codes" && Array.isArray(value) ? value.join(" · ") :
              field.kind === "decimal" && typeof value === "string" ? value : undefined;
            return <td key={field.name} data-field={field.name} title={raw}
              className="max-w-64 break-words p-2 text-right tabular-nums">
              {formatCell(value, field, language, t)}
            </td>;
          })}
        </tr>)}</tbody>
      </table>
    </div>}
  </LocalDataTable>;
}

function Summary({ items, label }: { items: readonly SummaryItem[]; label: string }) {
  return <dl aria-label={label} className="grid grid-cols-2 overflow-hidden rounded-md border sm:grid-cols-3 xl:grid-cols-4">
    {items.map(({ label, value }) => <div key={label} className="min-w-0 border-b border-r p-3">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-1 break-words font-medium tabular-nums">{value}</dd>
    </div>)}
  </dl>;
}

function ModeSelect<T extends string>({ options, value, onChange }: {
  options: Readonly<Record<T, string>>; value: T; onChange: (value: T) => void;
}) {
  const { t } = useLocale();
  return <label className="flex flex-wrap items-center gap-2">
    {t("Chart and table metric")}
    <select className={selectClass} value={value} onChange={event => onChange(event.target.value as T)}>
      {(Object.entries(options) as [T, string][]).map(([key, label]) =>
        <option key={key} value={key}>{t(label)}</option>)}
    </select>
  </label>;
}

const EMA = ["close", "fast_ema", "slow_ema"] as const satisfies readonly ModelFields<"builtin.ema_trend">[];
const RSI = ["rsi", "lower_threshold", "upper_threshold"] as const satisfies readonly ModelFields<"builtin.rsi_state">[];
const MOMENTUM = ["momentum"] as const satisfies readonly ModelFields<"builtin.momentum">[];
const VOLATILITY = ["annualized_volatility", "downside_deviation", "var_95", "expected_shortfall_95"] as const satisfies readonly ModelFields<"builtin.risk_profile">[];
const DRAWDOWN = ["current_drawdown", "maximum_drawdown"] as const satisfies readonly ModelFields<"builtin.risk_profile">[];
const REGIME_PRICE = ["close", "sma50", "sma200"] as const satisfies readonly ModelFields<"builtin.market_regime">[];
const REGIME_DISTANCE = ["close_distance", "sma_distance"] as const satisfies readonly ModelFields<"builtin.market_regime">[];
const REGIME_VOL = ["realized_volatility", "volatility_p25", "volatility_p75"] as const satisfies readonly ModelFields<"builtin.market_regime">[];
const OSCILLATOR = [0, 100] as const;

function useFields(result: AnalysisResult) {
  const { t, language } = useLocale();
  const fields = result.evidence.output_fields;
  const modelParameters = result.evidence.parameters as Record<string, number | string>;
  const cols = (names: readonly string[]) => columns(fields, names, t);
  const summary = (names: readonly string[], row: Observation | undefined): SummaryItem[] => cols(names).map(({ field, label }) => ({
    label, value: formatCell(row ? cell(row, field.name) : null, field, language, t),
  }));
  const parameters = (names: readonly string[]) => names.map(name => ({
    label: t(PARAMETERS[name]), value: modelParameters[name]?.toLocaleString(language),
  }));
  return { t, language, cols, summary, parameters, fields };
}

function TrendView({ result }: { result: AnalysisResult<"builtin.ema_trend" | "builtin.momentum" | "builtin.rsi_state"> }) {
  const { t, cols, summary, parameters, fields } = useFields(result);
  const rows = result.points;
  const latest = rows.at(-1);
  let chart: readonly string[];
  let metrics: SummaryItem[];
  if (isModel(result, "builtin.ema_trend")) {
    chart = EMA;
    const lastEvent = result.points.filter(row => row.crossover_after_close === "above" || row.crossover_after_close === "below").at(-1);
    metrics = [
      ...summary(["session_date", ...EMA, "trend_after_close", "warmup_complete"], latest),
      { label: t("Most recent crossover"), value: lastEvent
        ? summary(["session_date", "crossover_after_close"], lastEvent).map(item => item.value).join(" · ")
        : t("Unavailable") },
      ...parameters(["fast_window", "slow_window"]),
    ];
  } else if (isModel(result, "builtin.momentum")) {
    chart = MOMENTUM;
    metrics = [...summary(["session_date", "momentum", "comparison_session_date", "warmup_complete"], latest),
      ...parameters(["lookback_sessions"])];
  } else {
    chart = RSI;
    metrics = [...summary(["session_date", ...RSI, "rsi_state", "warmup_complete"], latest), ...parameters(["window"])];
  }
  const id = result.evidence.model.model_id;

  return <>
    <Summary items={metrics} label={t("Latest historical observation")} />
    <AnalysisSeriesChart title={t(MODELS[id][0])} rows={rows} columns={cols(chart)}
      range={id === "builtin.rsi_state" ? OSCILLATOR : undefined} zero={id === "builtin.momentum"}
      eventField={id === "builtin.ema_trend" ? "crossover_after_close" : undefined} />
    <AnalysisMetricTable rows={rows} fields={cols(fields.filter(field => field.name !== "session_date").map(field => field.name))} />
  </>;
}

const relativeModes = {
  normalized_100: "Normalized index levels",
  relative_strength: "Relative Strength ratios",
  return_difference: "Return differences",
} as const;

function RelativeStrengthView({ result }: { result: AnalysisResult<"builtin.relative_strength"> }) {
  const { t, language, cols } = useFields(result);
  const [mode, setMode] = useState<keyof typeof relativeModes>("normalized_100");
  const { sources, observed_sessions } = result.evidence;
  const points = result.points;
  const sourceColumns = (metric: keyof typeof relativeModes) => sources.flatMap(({ identity: { code } }, index) => {
    const name: ModelFields<"builtin.relative_strength"> = `${code.toLowerCase() as Lowercase<typeof code>}_${metric}`;
    return cols([name]).map(column => ({ ...column, label: code + (index === 0 ? " · " + t("Benchmark") : "") }));
  });
  const displayed = sourceColumns(mode);
  const latest = points.at(-1);
  const ratios = sourceColumns("relative_strength");
  const differences = sourceColumns("return_difference");
  return <>
    <Summary label={t("Shared source coverage")} items={[
      { label: t("Benchmark"), value: sources[0].identity.code },
      { label: t("Compared indexes"), value: sources.slice(1).map(source => source.identity.code).join(" · ") },
      { label: t("Latest shared session"), value: latest ? observedDate(latest.session_date, language) : t("Unavailable") },
      { label: t("Shared observed sessions"), value: observed_sessions.length.toLocaleString(language) },
    ]} />
    <div className="overflow-x-auto rounded-md border">
      <table aria-label={t("Shared source coverage")} className="w-full text-left text-xs">
        <thead className="bg-muted"><tr>{["Sources", "Excluded observations", "Latest ratio", "Latest return difference"].map(label =>
          <th key={label} scope="col" className="p-2">{t(label)}</th>)}</tr></thead>
        <tbody>{ratios.map((ratio, index) => {
          const difference = differences[index];
          return <tr key={ratio.field.name} className="border-t tabular-nums">
            <th scope="row" className="p-2 font-medium">{ratio.label}</th>
            <td className="p-2">{sources[index].excluded_count.toLocaleString(language)}</td>
            {[ratio, difference].map(({ field }) => <td key={field.name} className="p-2">
              {formatCell(latest ? cell(latest, field.name) : null, field, language, t)}
            </td>)}
          </tr>;
        })}</tbody>
      </table>
    </div>
    <ModeSelect options={relativeModes} value={mode} onChange={setMode} />
    <AnalysisSeriesChart key={mode} title={t(relativeModes[mode])} rows={points} columns={displayed} zero={mode === "return_difference"} />
    <AnalysisMetricTable rows={points} fields={displayed} />
  </>;
}

const riskModes = { volatility: "Volatility and tail risk", drawdown: "Drawdown", all: "All risk fields" } as const;
const OPEN_DRAWDOWN = ["open_peak_date", "open_trough_date", "open_elapsed_intervals"] as const satisfies readonly ModelFields<"builtin.risk_profile">[];
const MAX_DRAWDOWN = ["maximum_peak_date", "maximum_trough_date"] as const satisfies readonly ModelFields<"builtin.risk_profile">[];
const RECOVERY = ["last_recovered_peak_date", "last_recovered_trough_date", "last_recovery_date", "last_peak_to_recovery_intervals",
  "last_trough_to_recovery_intervals"] as const satisfies readonly ModelFields<"builtin.risk_profile">[];
const SAMPLE = ["historical_sample_count", "historical_tail_count", "historical_tail_status"] as const satisfies readonly ModelFields<"builtin.risk_profile">[];

function RiskView({ result }: { result: AnalysisResult<"builtin.risk_profile"> }) {
  const { t, cols, summary, parameters, fields } = useFields(result);
  const [mode, setMode] = useState<keyof typeof riskModes>("volatility");
  const rows = result.points;
  const latest = rows.at(-1);
  const sources = result.evidence.sources;
  const comparison = sources.length === 2;
  const table = mode === "drawdown" ? [...DRAWDOWN, ...OPEN_DRAWDOWN, ...MAX_DRAWDOWN, ...RECOVERY] :
    mode === "volatility" ? [...VOLATILITY, ...SAMPLE, ...(comparison ? ["correlation"] : [])] :
      fields.filter(field => field.name !== "session_date" &&
        (comparison || !["comparison_return", "correlation"].includes(field.name))).map(field => field.name);
  return <>
    <Summary label={t("Historical risk measurements")} items={[
      ...summary(["session_date", ...VOLATILITY, ...DRAWDOWN, ...(comparison ? ["correlation"] : []), ...SAMPLE], latest),
      ...parameters(["risk_window", "tail_window"]),
    ]} />
    {latest?.historical_tail_status === "insufficient_history" &&
      <p className="text-muted-foreground">{t("VaR and ES require the full tail history window.")}</p>}
    {latest?.historical_tail_status === "empty_tail" &&
      <p className="text-muted-foreground">{t("Expected Shortfall is unavailable because the historical tail is empty.")}</p>}
    {comparison && <p className="text-muted-foreground">
      {t("Primary source")} · {t(SOURCE_NAMES[sources[0].identity.code])} · {t("Comparison source")} · {t(SOURCE_NAMES[sources[1].identity.code])}. {t("Correlation describes aligned historical observed-session returns.")}
    </p>}
    <ModeSelect options={riskModes} value={mode} onChange={setMode} />
    {mode !== "drawdown" && <AnalysisSeriesChart title={t(riskModes.volatility)} rows={rows} columns={cols(VOLATILITY)} />}
    {mode !== "volatility" && <>
      <AnalysisSeriesChart title={t(riskModes.drawdown)} rows={rows} columns={cols(DRAWDOWN)} zero />
      <div className="grid gap-3 lg:grid-cols-2">
        <section className="space-y-2"><h4 className="font-medium">{t("Ongoing drawdown")}</h4>
          <Summary label={t("Ongoing drawdown")} items={summary(OPEN_DRAWDOWN, latest)} />
        </section>
        <section className="space-y-2"><h4 className="font-medium">{t("Last completed recovery")}</h4>
          <Summary label={t("Last completed recovery")} items={summary(RECOVERY, latest)} />
        </section>
      </div>
      <section className="space-y-2"><h4 className="font-medium">{t("Maximum historical drawdown dates")}</h4>
        <Summary label={t("Maximum historical drawdown dates")} items={summary(MAX_DRAWDOWN, latest)} />
      </section>
    </>}
    <AnalysisMetricTable rows={rows} fields={cols(table)} />
    <details className="rounded-md border px-3 py-2">
      <summary className="cursor-pointer font-medium">{t("Risk assumptions")}</summary>
      <ul className="ml-5 mt-2 list-disc space-y-1 text-muted-foreground">
        <li>{t("Historical price-index research uses simple returns from actual observed sessions.")}</li>
        <li>{t("Volatility uses sample standard deviation and √252 annualization. Downside deviation is per observed session.")}</li>
        <li>{t("Historical VaR and Expected Shortfall use empirical losses at 95% confidence.")}</li>
        <li>{t("Future losses may exceed historical VaR. These measurements describe historical index behavior and do not assess portfolio risk.")}</li>
      </ul>
    </details>
  </>;
}

const regimeModes = { trend: "Trend features", volatility: "Volatility features" } as const;
const REGIME_STATES = ["trend_state", "volatility_state", "reason_codes"] as const satisfies readonly ModelFields<"builtin.market_regime">[];

function RegimeView({ result }: { result: AnalysisResult<"builtin.market_regime"> }) {
  const { t, cols, summary, parameters } = useFields(result);
  const [mode, setMode] = useState<keyof typeof regimeModes>("trend");
  const rows = result.points;
  const latest = rows.at(-1);
  const features = mode === "trend" ? [...REGIME_PRICE, ...REGIME_DISTANCE] : [...REGIME_VOL, "volatility_calibration_count"];
  return <>
    <Summary label={t("Latest historical classifications")} items={[
      ...summary(["session_date", ...REGIME_STATES, "sma50", "sma200", ...REGIME_VOL, "volatility_calibration_count"], latest),
      ...parameters(["short_window", "long_window", "neutrality_band", "volatility_window", "calibration_window"]),
    ]} />
    <div className="grid min-w-0 gap-3 2xl:grid-cols-2">
      {cols(["trend_state", "volatility_state"]).map(({ field, label }) =>
        <AnalysisStateTimeline key={field.name} rows={rows} field={field} title={label} />)}
    </div>
    <ModeSelect options={regimeModes} value={mode} onChange={setMode} />
    {mode === "trend" ? <>
      <AnalysisSeriesChart title={t("Trend price levels")} rows={rows} columns={cols(REGIME_PRICE)} />
      <AnalysisSeriesChart title={t("Trend distances")} rows={rows} columns={cols(REGIME_DISTANCE)} zero />
    </> : <AnalysisSeriesChart title={t(regimeModes.volatility)} rows={rows} columns={cols(REGIME_VOL)} />}
    <AnalysisMetricTable rows={rows} fields={cols([...REGIME_STATES, ...features])} />
  </>;
}

export function AnalysisResultVisualization({ result }: { result: AnalysisResult }) {
  const { t } = useLocale();
  const content = isModel(result, "builtin.relative_strength") ? <RelativeStrengthView result={result} /> :
    isModel(result, "builtin.risk_profile") ? <RiskView result={result} /> :
    isModel(result, "builtin.market_regime") ? <RegimeView result={result} /> : <TrendView result={result} />;
  const model = t(MODELS[result.evidence.model.model_id][0]);
  return <section aria-label={model} className="min-w-0 space-y-4 border-t pt-3" key={result.output_sha256}>
    <header className="flex flex-wrap items-baseline justify-between gap-2">
      <div>
        <h3 className="font-semibold">{model}</h3>
        <p className="text-xs text-muted-foreground">{t("Historical results")}</p>
      </div>
      <strong className="rounded-md border bg-muted px-2 py-1 text-xs">{t("Research only")}</strong>
    </header>
    {result.evidence.quality_policy === "allow_unverified" &&
      <p role="status" className="rounded-md border bg-muted p-2">{t("Unverified evidence is allowed for this historical research.")}</p>}
    {result.evidence.sources.some(source => source.quality.status !== "verified") &&
      <p role="status" className="rounded-md border bg-muted p-2">{t("Result uses unverified source evidence.")}</p>}
    {content}
  </section>;
}
