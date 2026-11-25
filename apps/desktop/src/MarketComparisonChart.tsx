import { useMemo } from "react";
import { AnalysisSeriesChart } from "./AnalysisSeriesChart";
import { formatCell, type ResultColumn } from "./analysis-result-display";
import type { AnalysisOutputField } from "./analysis-jobs-api";
import { LocalDataTable } from "./components/TablePagination";
import { useLocale } from "./localization";
import type { ComparisonMode, ComparisonResult, ComparisonSource } from "./market-comparison-api";

const INSPECTION_COLORS = ["text-blue-700 [html:not([data-theme=light])_&]:text-blue-300", "text-amber-700 [html:not([data-theme=light])_&]:text-amber-300", "text-teal-700 [html:not([data-theme=light])_&]:text-teal-300", "text-fuchsia-700 [html:not([data-theme=light])_&]:text-fuchsia-300"];
const providers = { fred: "FRED", tiingo: "Tiingo", alpaca_sip: "Alpaca SIP", alpha_vantage: "Alpha Vantage" };
export const comparisonLabel = (source: ComparisonSource) => `${source.code} · ${providers[source.provider]}`;
const units = (mode: ComparisonMode) => mode === "close" ? "price_level" : mode === "normalized_100" ? "index_base_100" : "percent_points";
const field = (name: string, units: string): AnalysisOutputField => ({
  name, kind: "decimal", units, nullable: true, strictly_positive: false, allowed_values: [],
});

export function MarketComparisonChart({ result, mode, hidden, onVisibility }: {
  result: ComparisonResult; mode: ComparisonMode; hidden: readonly string[]; onVisibility: (key: string) => void;
}) {
  const { t, language } = useLocale();
  const title = t(mode === "close" ? "Actual close" : mode === "normalized_100" ? "Base 100" : "Cumulative price return");
  const rows = useMemo(() => result.points.map(point => ({
    session_date: point.session_date,
    ...Object.fromEntries(result.sources.map(({ key }, i) => [key, point.values[i]?.[mode] ?? null])),
  })), [result, mode]);
  const columns: ResultColumn[] = useMemo(() => result.sources.map(source => ({
    label: result.sources.filter(item => item.identity.code === source.identity.code).length > 1 ? comparisonLabel(source.identity) : source.identity.code, field: field(source.key, units(mode)),
  })), [result, mode]);
  const displayed = useMemo(() => columns.filter(column => !hidden.includes(column.field.name)), [columns, hidden]);
  const closeField = field("close", "price_level");
  return <div className="space-y-3">
    {mode === "close" && <p className="text-xs text-muted-foreground">{t("Actual closes use USD per share for stocks and ETFs, and index points for indexes. Use returns for relative comparison.")}</p>}
    <fieldset className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
      <legend className="sr-only">{t("Visible comparison series")}</legend>
      {columns.map(column => <label key={column.field.name} className="flex items-center gap-1.5">
        <input type="checkbox" checked={!hidden.includes(column.field.name)} onChange={() => onVisibility(column.field.name)}
          className="accent-primary focus-visible:ring-2 focus-visible:ring-ring" /><span className={INSPECTION_COLORS[columns.indexOf(column)]}>{column.label}</span>
      </label>)}
    </fieldset>
    {displayed.length > 0
      ? <AnalysisSeriesChart title={title} rows={rows} columns={displayed} styleIndices={displayed.map(column => columns.indexOf(column))} expanded hideLegend
          inspection={index => result.sources.map((source, i) => {
            const value = result.points[index].values[i];
            return <span key={source.key}><strong className={INSPECTION_COLORS[i]}>{columns[i].label}</strong> · {t("Close")}: {value ? formatCell(value.close, closeField, language, t) : t("Unavailable")} {value ? (source.identity.kind === "index" ? "pts" : "USD") : ""} · {t("Cumulative price return")}: {value ? formatCell(value.cumulative_return_pct, field("return", "percent_points"), language, t) : t("Unavailable")}</span>;
          })} />
      : <p role="status" className="text-sm text-muted-foreground">{t("Show a series to display the comparison chart.")}</p>}
    <details className="space-y-2">
      <summary className="cursor-pointer text-sm font-semibold">{t("Comparison observation table")}</summary>
      <section className="space-y-2" aria-label={t("Comparison observation table")}>
      <LocalDataTable rows={result.points} label={t("Comparison observation table")} searchable={row => row.session_date}>
        {visible => <div className="overflow-x-auto rounded-md border">
          <table className="w-full min-w-max text-right text-xs tabular-nums">
            <thead className="bg-muted text-muted-foreground">
              <tr><th scope="col" className="px-3 py-2 text-left">{t("Date")}</th>
                {columns.map(column => <th key={column.field.name} scope="col" className="px-3 py-2">{column.label} · {mode === "close" ? title : `${t("Close")} / ${title}`}</th>)}</tr>
            </thead>
            <tbody>{visible.map(point => <tr key={point.session_date} className="border-t">
              <th scope="row" className="px-3 py-2 text-left font-medium">{point.session_date}</th>
              {point.values.map((value, i) => <td key={columns[i].field.name} className="px-3 py-2">
                {value ? <>{mode !== "close" && <span className="text-muted-foreground">{formatCell(value.close, closeField, language, t)} / </span>}
                  {formatCell(value[mode], columns[i].field, language, t)}</> : t("Unavailable")}
              </td>)}
            </tr>)}</tbody>
          </table>
        </div>}
      </LocalDataTable>
    </section>
    </details>
  </div>;
}
