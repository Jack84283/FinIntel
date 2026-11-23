import { useMemo, useState } from "react";
import { AnalysisSeriesChart } from "./AnalysisSeriesChart";
import { formatCell, observedDate, type ResultColumn, type Translator } from "./analysis-result-display";
import type { BacktestJobResult, BacktestJobSummary, BacktestReport } from "./backtest-jobs-api";
import { LocalDataTable } from "./components/TablePagination";
import { useLocale } from "./localization";

const MONEY = "Modeled capital units";
const FRACTION = "decimal_return";
const equityFields = [
  ["equity_after_cost", "Strategy equity after costs", MONEY],
  ["frictionless_benchmark_equity", "Frictionless benchmark equity", MONEY],
  ["drawdown", "Drawdown", FRACTION],
  ["exposure_after_close", "Modeled index allocation", FRACTION],
  ["transaction_cost", "Modeled transaction cost", MONEY],
] as const;
const transactionFields = [
  ["exposure_before", "Previous exposure"], ["exposure_after", "New exposure"],
  ["turnover_fraction", "Turnover"], ["cost", "Modeled transaction cost"],
] as const;
const metricFields = [
  ["cumulative_return", "Cumulative strategy return", FRACTION, "Total change in strategy equity relative to initial capital."],
  ["annualized_return", "Annualized return", FRACTION, "Compounded return expressed using 252 observed sessions per year."],
  ["max_drawdown", "Maximum drawdown", FRACTION, "Largest observed equity decline from a prior peak."],
  ["annualized_volatility", "Annualized volatility", FRACTION, "Sample standard deviation of modeled session returns, scaled by the square root of 252."],
  ["sharpe", "Sharpe ratio", "Ratio", "Annualized ratio of excess return to return volatility using the specified risk-free rate."],
  ["frictionless_benchmark_cumulative_return", "Benchmark cumulative return", FRACTION, "Change in same-index buy-and-hold equity relative to initial capital, without costs."],
  ["relative_to_frictionless_benchmark", "Relative performance", FRACTION, "Ending strategy equity divided by ending benchmark equity, minus one."],
  ["transaction_cost_total", "Total modeled transaction costs", MONEY, "Sum of modeled commission and slippage."],
  ["turnover_fraction", "Modeled turnover", FRACTION, "Sum of absolute changes in the modeled allocation fraction."],
  ["transactions", "Hypothetical transactions", "Count", "Number of modeled exposure changes."],
] as const;

function column(name: string, label: string, units: string, t: Translator): ResultColumn {
  return { label: t(label), field: { name, kind: "decimal", units, nullable: true, strictly_positive: false, allowed_values: [] } };
}

function decimalEqual(a: string, b: string) {
  const normalize = (v: string) => v.includes(".") ? v.replace(/0+$/, "").replace(/\.$/, "") : v;
  return normalize(a) === normalize(b) || (["0", "-0"].includes(normalize(a)) && ["0", "-0"].includes(normalize(b)));
}

function increasing(a: string, b: string) {
  const [aw, af = ""] = a.split(".");
  const [bw, bf = ""] = b.split(".");
  const precision = Math.max(af.length, bf.length);
  return BigInt(aw + af.padEnd(precision, "0")) > BigInt(bw + bf.padEnd(precision, "0"));
}

function Transactions({ transactions }: { transactions: BacktestReport["transactions"] }) {
  const { t, language } = useLocale();
  const [direction, setDirection] = useState("all");
  const rows = useMemo(() => transactions.filter(row => direction === "all" ||
    (direction === "increase" ? increasing(row.exposure_after, row.exposure_before) : increasing(row.exposure_before, row.exposure_after))), [transactions, direction]);
  return <section className="space-y-3">
    <h4 className="font-semibold">{t("Hypothetical transactions")}</h4>
    {transactions.length === 0 ? <p role="status">{t("The strategy made no modeled exposure changes.")}</p> : <>
      <label className="flex flex-wrap items-center gap-2">{t("Exposure direction")}
        <select value={direction} onChange={event => setDirection(event.target.value)} className="h-9 rounded-md border border-input bg-background px-2 focus-visible:ring-2 focus-visible:ring-ring">
          <option value="all">{t("All")}</option><option value="increase">{t("Increase")}</option><option value="decrease">{t("Decrease")}</option>
        </select>
      </label>
      <LocalDataTable rows={rows} label={t("Signal or hypothetical fill date")}
        searchable={row => [row.signal_as_of_session, row.hypothetical_fill_session, observedDate(row.signal_as_of_session, language), observedDate(row.hypothetical_fill_session, language)].join(" ")}>
        {visible => <div className="overflow-x-auto rounded-md border">
          <table aria-label={t("Hypothetical transactions")} className="w-full whitespace-nowrap text-left text-xs tabular-nums">
            <thead className="bg-muted"><tr>{["Signal session", "Hypothetical fill session", ...transactionFields.map(([, label]) => label)].map(label =>
              <th scope="col" className="p-2" key={label}>{t(label)}</th>)}</tr></thead>
            <tbody>{visible.map(row => <tr className="border-t" key={row.hypothetical_fill_session}>
              <td className="p-2">{observedDate(row.signal_as_of_session, language)}</td>
              <th scope="row" className="p-2 font-normal">{observedDate(row.hypothetical_fill_session, language)}</th>
              {transactionFields.map(([key, label]) => <td className="p-2" title={row[key]} key={key}>
                {formatCell(row[key], column(key, label, key === "cost" ? MONEY : FRACTION, t).field, language, t)}
              </td>)}
            </tr>)}</tbody>
          </table>
        </div>}
      </LocalDataTable>
    </>}
  </section>;
}

const backtestPlotting = {
  convert(value: unknown, unit: string) {
    if (typeof value !== "string") return null;
    const scaled = Number(value) * (unit === FRACTION ? 100 : 1);
    // Leave headroom for axis padding and coordinate math, and never plot underflow as zero.
    if (Math.abs(scaled) > 1e300 || !Number.isFinite(scaled) || (scaled === 0 && /[1-9]/.test(value))) return null;
    return scaled;
  },
  omittedMessage: "Some values are outside the chart’s numerical range. See the table for exact values.",
};

export function BacktestResultView({ job, result }: { job: BacktestJobSummary; result: BacktestJobResult }) {
  const { t, language } = useLocale();
  const { report } = result;
  const { simulation } = job.request;
  const { assumptions } = simulation;
  const columns = equityFields.map(([name, label, units]) => column(name, label, units, t));
  const markers = useMemo(() => new Set(report.transactions.map(row => row.hypothetical_fill_session)), [report.transactions]);
  const worst = report.rows.filter(row => decimalEqual(row.drawdown, report.metrics.max_drawdown));

  return <section aria-label={t("Verified backtest result")} className="min-w-0 space-y-4 border-t pt-3">
    <div className="space-y-2">
      <h3 className="font-semibold">{t("Verified backtest result")}</h3>
      <p className="text-muted-foreground">{t("Test period")} · {observedDate(simulation.test_start, language)} – {observedDate(simulation.test_end, language)} · {report.test_sessions.toLocaleString(language)} {t("Observed sessions")}</p>
      <p>{t("Initial capital")} · <strong>{formatCell(assumptions.initial_capital, columns[0].field, language, t)} {t(MONEY)}</strong>
        {" · "}{t("Modeled index allocation")} · <strong>{formatCell(assumptions.position_fraction, columns[3].field, language, t)}</strong></p>
      <p className="text-muted-foreground">{t("The source is a price index and returns exclude dividends. The benchmark ignores transaction costs.")}</p>
    </div>
    <dl className="grid gap-2 rounded-md border bg-muted/30 p-3 sm:grid-cols-2 xl:grid-cols-3">
      {metricFields.map(([key, label, units]) => <div key={key} className="min-w-0 space-y-1">
        <dt className="text-xs text-muted-foreground">{t(label)}</dt>
        <dd className="break-all font-medium tabular-nums" title={report.metrics[key]?.toString()}>
          {formatCell(report.metrics[key], column(key, label, units, t).field, language, t)}
          {units === MONEY && <span className="ml-1 text-xs font-normal">{t(MONEY)}</span>}
        </dd>
      </div>)}
    </dl>
    <details className="rounded-md border p-3">
      <summary className="cursor-pointer font-medium">{t("Metric definitions")}</summary>
      <dl className="mt-2 space-y-2">{metricFields.map(([key, label, , explanation]) => <div key={key}>
        <dt className="font-medium">{t(label)}</dt><dd className="text-muted-foreground">{t(explanation)}</dd>
      </div>)}</dl>
    </details>
    <AnalysisSeriesChart plotting={backtestPlotting} title={t("Strategy and benchmark equity")} rows={report.rows} columns={columns.slice(0, 2)} />
    <AnalysisSeriesChart plotting={backtestPlotting} title={t("Historical drawdown")} rows={report.rows} columns={[columns[2]]} zero nonPositive />
    {worst.length > 0 && <p>{t("Worst observed drawdown")} · {formatCell(report.metrics.max_drawdown, columns[2].field, language, t)}
      {" · "}{observedDate(worst[0].session_date, language)}
      {worst.length > 1 && <> · {t("Last occurrence")} {observedDate(worst.at(-1)!.session_date, language)} · {worst.length.toLocaleString(language)} {t("Observed sessions")}</>}</p>}
    <AnalysisSeriesChart plotting={backtestPlotting} title={t("Modeled index allocation")} rows={report.rows} columns={[columns[3]]} range={[0, 100]} step
      markers={{ dates: markers, label: t("Dotted markers indicate hypothetical fill sessions.") }} />
    <AnalysisSeriesChart plotting={backtestPlotting} title={t("Modeled transaction cost")} rows={report.rows} columns={[columns[4]]} zero />
    <section className="space-y-2">
      <h4 className="font-semibold">{t("Equity history")}</h4>
      <LocalDataTable rows={report.rows} label={t("Observed session")}
        searchable={row => row.session_date + " " + observedDate(row.session_date, language)}>
        {visible => <div className="overflow-x-auto rounded-md border"><table aria-label={t("Equity history")} className="w-full whitespace-nowrap text-left text-xs tabular-nums">
          <thead className="bg-muted"><tr>{[t("Observed session"), ...columns.map(item => item.label), t("Prior signal session")].map(label =>
            <th scope="col" key={label} className="p-2">{label}</th>)}</tr></thead>
          <tbody>{visible.map(row => <tr key={row.session_date} className="border-t">
            <th scope="row" className="p-2 font-normal">{observedDate(row.session_date, language)}</th>
            {equityFields.map(([key], i) => <td key={key} className="p-2" title={row[key]}>{formatCell(row[key], columns[i].field, language, t)}</td>)}
            <td className="p-2">{observedDate(row.executed_from_prior_signal, language)}</td>
          </tr>)}</tbody>
        </table></div>}
      </LocalDataTable>
    </section>
    <Transactions transactions={report.transactions} />
  </section>;
}
