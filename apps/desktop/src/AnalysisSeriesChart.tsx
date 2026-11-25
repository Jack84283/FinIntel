import { useId, useMemo, useState, useEffect, useRef, type ReactNode } from "react";
import type { AnalysisOutputField } from "./analysis-jobs-api";
import { useLocale } from "./localization";
import {
  cell, chartNumber, continuousSegments, datePosition, fieldLabel, formatCell, observedDate, unitLabel,
  type Observation, type ResultColumn,
} from "./analysis-result-display";

const DASHES = ["", "8 4", "3 4", "10 4 2 4"];
const COLORS = [
  "stroke-blue-700 [html:not([data-theme=light])_&]:stroke-blue-300", "stroke-amber-700 [html:not([data-theme=light])_&]:stroke-amber-300",
  "stroke-teal-700 [html:not([data-theme=light])_&]:stroke-teal-300", "stroke-fuchsia-700 [html:not([data-theme=light])_&]:stroke-fuchsia-300",
];

// The Market chart uses a compact right-hand scale, while other analysis charts
// retain their original left-hand scale and dimensions.
const MARKET_COLORS = [
  "stroke-blue-700 [html:not([data-theme=light])_&]:stroke-blue-300",
  "stroke-green-700 [html:not([data-theme=light])_&]:stroke-green-300",
  "stroke-amber-700 [html:not([data-theme=light])_&]:stroke-amber-300",
  "stroke-fuchsia-700 [html:not([data-theme=light])_&]:stroke-fuchsia-300",
];
const LEFT = 88;
const RIGHT = 819;
const TOP = 18;
const BOTTOM = 213;

function Legend({ labels, indices }: { labels: readonly string[]; indices?: readonly number[] }) {
  return <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
    {labels.map((label, index) => <li key={label} className="flex items-center gap-2">
      <svg viewBox="0 0 28 8" width="28" height="8" aria-hidden="true">
        <path d="M0 4H28" strokeWidth="2" strokeDasharray={DASHES[indices?.[index] ?? index]} className={COLORS[indices?.[index] ?? index]} />
      </svg>{label}
    </li>)}
  </ul>;
}

function DateLabels({ rows, right = RIGHT, bottom = BOTTOM, left = LEFT, compact = false }: { rows: readonly Observation[]; right?: number; bottom?: number; left?: number; compact?: boolean }) {
  const { language } = useLocale();
  const first = rows[0].session_date;
  const last = rows.at(-1)!.session_date;
  if (compact) {
    const count = Math.min(rows.length, Math.max(2, Math.min(6, Math.floor((right - left) / 115))));
    const span = Date.parse(last) - Date.parse(first);
    const formatter = new Intl.DateTimeFormat(language, span > 366 * 86400000
      ? { year: "numeric", month: "short", timeZone: "UTC" }
      : { month: "short", day: "numeric", timeZone: "UTC" });
    return <g fill="currentColor" className="text-muted-foreground" fontSize="11">
      {Array.from({ length: count }, (_, i) => {
        const row = rows[Math.round(i * (rows.length - 1) / Math.max(1, count - 1))];
        return <text key={row.session_date} x={left + datePosition(row.session_date, first, last) * (right - left)} y={bottom + 22}
          textAnchor={i === 0 ? "start" : i === count - 1 ? "end" : "middle"}>{formatter.format(new Date(row.session_date + "T00:00:00Z"))}</text>;
      })}
    </g>;
  }
  return <g fill="currentColor" className="text-muted-foreground" fontSize="12">
    <text x={left} y={bottom + 26}>{observedDate(first, language)}</text>
    {first !== last && <text x={right} y={bottom + 26} textAnchor="end">{observedDate(last, language)}</text>}
  </g>;
}

export function AnalysisSeriesChart({ title, rows, columns, range, zero, eventField, markers, step, nonPositive, plotting, styleIndices, expanded, inspection, hideLegend }: {
  title: string;
  rows: readonly Observation[];
  columns: readonly ResultColumn[];
  range?: readonly [number, number];
  zero?: boolean;
  eventField?: string;
  step?: boolean;
  nonPositive?: boolean;
  styleIndices?: readonly number[];
  expanded?: boolean;
  hideLegend?: boolean;
  inspection?: (index: number) => ReactNode;
  plotting?: { convert: typeof chartNumber; omittedMessage: string };
  markers?: { dates: ReadonlySet<string>; label: string };
}) {
  const { t, language } = useLocale();
  const descriptionId = useId();
  const [inspected, setInspected] = useState<number>();
  const [hovering, setHovering] = useState(false);
  const [inspectionFocused, setInspectionFocused] = useState(false);
  const plotRef = useRef<SVGSVGElement>(null);
  const [dimensions, setDimensions] = useState({ width: 840, height: 500 });
  useEffect(() => {
    if (!expanded || !plotRef.current || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(entries => {
      const { width, height } = entries[0].contentRect;
      if (width > 0 && height > 0) setDimensions({ width, height });
    });
    observer.observe(plotRef.current);
    return () => observer.disconnect();
  }, [expanded, rows.length, columns.length]);
  const width = expanded ? dimensions.width : 840;
  const height = expanded ? dimensions.height : 250;
  const left = expanded ? 12 : LEFT;
  const right = width - (expanded ? 76 : 21);
  const colors = expanded ? MARKET_COLORS : COLORS;
  const bottom = height - 37;
  const segments = useMemo(() => columns.map(column => continuousSegments(rows, column, plotting?.convert)), [columns, rows, plotting]);
  const omitted = useMemo(() => plotting && columns.some(column => rows.some(row => {
    const value = cell(row, column.field.name);
    return value != null && plotting.convert(value, column.field.units) === null;
  })), [columns, rows, plotting]);
  const extent = useMemo(() => {
    let low = zero ? 0 : Infinity;
    let high = zero ? 0 : -Infinity;
    let available = false;
    for (const series of segments) for (const segment of series) for (const point of segment) {
      low = Math.min(low, point.value);
      high = Math.max(high, point.value);
      available = true;
    }
    if (range) return { low: range[0], high: range[1], available };
    if (!available) return { low: 0, high: 1, available };
    const padding = high === low ? Math.max(Math.abs(high) * 0.04, 0.0001) : (high - low) * 0.06;
    return { low: low - padding, high: nonPositive && high <= 0 ? 0 : high + padding, available };
  }, [segments, range, zero, nonPositive]);

  const first = rows[0]?.session_date;
  const last = rows.at(-1)?.session_date;
  const paths = useMemo(() => {
    if (!first || !last || !extent.available) return [];
    const x = (date: string) => left + datePosition(date, first, last) * (right - left);
    const y = (value: number) => bottom - (value - extent.low) / (extent.high - extent.low) * (bottom - TOP);
    return segments.map(series => ({
      strokes: series.filter(segment => segment.length > 1).map(segment => segment.map((point, j) =>
        j && step ? `H${x(point.date).toFixed(2)}V${y(point.value).toFixed(2)}` :
                `${j ? "L" : "M"}${x(point.date).toFixed(2)},${y(point.value).toFixed(2)}`).join(" ")).join(" "),
      dots: series.filter(segment => segment.length === 1).map(([point]) => ({
        date: point.date, x: x(point.date), y: y(point.value),
      })),
    }));
  }, [first, last, extent, segments, step, right, bottom, left]);

  if (!first || !last || !extent.available) return <figure className="space-y-2">
    <figcaption className={expanded ? "sr-only" : "font-medium"}>{title}</figcaption>
    <p role="status" className="rounded-md border p-4 text-sm text-muted-foreground">
      {omitted ? t(plotting!.omittedMessage) : t("No calculated values in the selected range.")}
    </p>
  </figure>;

  const unit = columns[0].field.units;
  const x = (date: string) => left + datePosition(date, first, last) * (right - left);
  const y = (value: number) => bottom - (value - extent.low) / (extent.high - extent.low) * (bottom - TOP);
  const index = Math.min(inspected ?? rows.length - 1, rows.length - 1);
  const selection = rows[index];
  const ticks = Array.from({ length: 5 }, (_, i) => extent.low + (extent.high - extent.low) * i / 4);
  const formatAxis = new Intl.NumberFormat(language, {
    maximumSignificantDigits: 5,
    notation: plotting && Math.max(Math.abs(extent.low), Math.abs(extent.high)) >= 1e6 ? "scientific" : "standard",
  });
  const hasEvents = eventField && rows.some(row => ["above", "below"].includes(String(cell(row, eventField))));

  return <figure className="min-w-0 space-y-2">
    <figcaption className="font-medium">{title} <span className="text-xs font-normal text-muted-foreground">· {unitLabel(unit, t)}</span></figcaption>
    {!hideLegend && <Legend labels={columns.map(column => column.label)} indices={styleIndices} />}
    {omitted && <p className="text-xs text-muted-foreground">{t(plotting!.omittedMessage)}</p>}
    {markers && markers.dates.size > 0 && <p className="text-xs text-muted-foreground">{markers.label}</p>}
    {hasEvents && <p className="text-xs text-muted-foreground">{t("Triangles mark post-close crossover events.")}</p>}
    <div className={expanded ? "relative overflow-x-auto" : "relative overflow-x-auto rounded-md border bg-muted/30 p-2"}>
      <svg ref={plotRef} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={title + " · " + unitLabel(unit, t)}
        aria-describedby={descriptionId} className={expanded ? "h-[62vh] min-h-[320px] w-full min-w-[460px]" : "h-auto w-full min-w-[460px]"}
        data-comparison-chart={expanded ? "" : undefined}
        onPointerLeave={() => setHovering(false)}
        onPointerMove={event => {
          if (!expanded) return;
          const matrix = event.currentTarget.getScreenCTM?.();
          if (!matrix) return;
          const point = event.currentTarget.createSVGPoint();
          point.x = event.clientX; point.y = event.clientY;
          const position = point.matrixTransform(matrix.inverse());
          if (position.x < left || position.x > right || position.y < TOP || position.y > bottom) { setHovering(false); return; }
          setHovering(true);
          let nearest = 0;
          for (let i = 1; i < rows.length; i++) if (Math.abs(x(rows[i].session_date) - position.x) < Math.abs(x(rows[nearest].session_date) - position.x)) nearest = i;
          setInspected(nearest);
        }}>
        {expanded && <rect x={left} y={TOP} width={right - left} height={bottom - TOP} fill="transparent" />}
        {ticks.map(tick => <g key={tick} className="text-muted-foreground">
          <path d={`M${left},${y(tick)}H${right}`} stroke="currentColor" opacity="0.25" strokeDasharray="3 5" />
          <text x={expanded ? right + 8 : left - 9} y={y(tick) + 4} fontSize="12" textAnchor={expanded ? "start" : "end"} fill="currentColor">{expanded && unit === "percent_points" ? `${tick > 0 ? "+" : ""}${formatAxis.format(tick)}%` : formatAxis.format(tick)}</text>
        </g>)}
        {zero && <path d={`M${left},${y(0)}H${right}`} stroke="currentColor" opacity=".45" />}
        {paths.map(({ strokes, dots }, i) => <g key={columns[i].field.name} data-series={columns[i].field.name}>
          {strokes && <path fill="none" strokeWidth="2" strokeDasharray={expanded ? undefined : DASHES[styleIndices?.[i] ?? i]} strokeLinejoin="round"
            className={colors[styleIndices?.[i] ?? i]} d={strokes}>
            <title>{columns[i].label}</title>
          </path>}
          {dots.map(dot => <circle key={dot.date} cx={dot.x} cy={dot.y} r="2.5" className={colors[styleIndices?.[i] ?? i]} />)}
        </g>)}
        {markers && [...markers.dates].map(date => <path key={date} data-transaction-session={date}
          d={`M${x(date)},${TOP}V${bottom}`} stroke="currentColor" strokeDasharray="1 4" opacity=".65">
          <title>{markers.label + " · " + observedDate(date, language)}</title>
        </path>)}
        {eventField && rows.map(row => {
          const event = cell(row, eventField);
          const level = chartNumber(cell(row, "fast_ema"), unit);
          if ((event !== "above" && event !== "below") || level === null) return null;
          const dx = x(row.session_date), dy = y(level);
          return <path key={row.session_date} data-crossover={event}
            d={`M${dx},${dy + (event === "above" ? -7 : 7)}l-4,${event === "above" ? 7 : -7}h8Z`}
            fill="currentColor" className="text-foreground">
            <title>{row.session_date + " · " + fieldLabel(event, t)}</title>
          </path>;
        })}
        <path d={`M${x(selection.session_date)},${TOP}V${bottom}`} stroke="currentColor" strokeDasharray="3 4" opacity=".48" />
        {expanded && columns.map((column, i) => {
          const value = chartNumber(cell(selection, column.field.name), unit);
          return value === null ? null : <circle key={column.field.name} cx={x(selection.session_date)} cy={y(value)} r="3.5" className={colors[styleIndices?.[i] ?? i]} fill="currentColor" />;
        })}
        <DateLabels rows={rows} right={right} bottom={bottom} left={left} compact={expanded} />
      </svg>
      {expanded && inspection && (hovering || inspectionFocused) && <div aria-hidden="true" data-comparison-tooltip className="pointer-events-none absolute top-4 grid max-w-[42%] gap-1 rounded-md border bg-background/95 p-2 text-xs tabular-nums shadow-sm" style={x(selection.session_date) < width / 2 ? { left: `calc(${100 * x(selection.session_date) / width}% + 12px)` } : { right: `calc(${100 * (width - x(selection.session_date)) / width}% + 12px)` }}>
        <strong>{observedDate(selection.session_date, language)}</strong>
        {inspection(index)}
      </div>}
    </div>
    <p id={descriptionId} className="sr-only">{t("The observation table contains the exact values. Missing values leave gaps.")}</p>
    <label className={expanded ? "sr-only focus-within:not-sr-only focus-within:flex focus-within:items-center focus-within:gap-3" : "flex flex-wrap items-center gap-x-3 gap-y-1 text-xs"}>
      {t("Inspect observed session")}
      <input className="w-full max-w-64 accent-primary focus-visible:ring-2 focus-visible:ring-ring" type="range"
        min={0} max={rows.length - 1} value={index} disabled={rows.length < 2}
        aria-valuetext={observedDate(selection.session_date, language)}
        onFocus={() => setInspectionFocused(true)} onBlur={() => setInspectionFocused(false)}
        onChange={event => setInspected(Number(event.target.value))} />
    </label>
    <output className={expanded ? "sr-only" : "flex flex-wrap gap-x-4 gap-y-1 text-xs tabular-nums"}>
      <strong>{observedDate(selection.session_date, language)}</strong>
      {inspection ? inspection(index) : columns.map(column => <span key={column.field.name}>{column.label} · {formatCell(cell(selection, column.field.name), column.field, language, t)}</span>)}
      {eventField && <span>{fieldLabel(eventField, t)} · {fieldLabel(String(cell(selection, eventField) ?? "Unavailable"), t)}</span>}
    </output>
  </figure>;
}

/** Separate categorical bands show only sessions where the backend returned that state. */
export function AnalysisStateTimeline({ rows, field, title }: {
  rows: readonly Observation[]; field: AnalysisOutputField; title: string;
}) {
  const { t } = useLocale();
  if (!rows.length) return null;
  const first = rows[0].session_date;
  const last = rows.at(-1)!.session_date;
  const states = field.allowed_values;
  const paths = states.map(state => rows.flatMap(row => {
    if (cell(row, field.name) !== state) return [];
    const x = LEFT + datePosition(row.session_date, first, last) * (RIGHT - LEFT);
    return [`M${x.toFixed(2)},-4v8`];
  }).join(" "));
  return <figure className="min-w-0 space-y-2">
    <figcaption className="font-medium">{title}</figcaption>
    <div className="overflow-x-auto rounded-md border bg-muted/30 p-2">
      <svg viewBox="0 0 840 250" role="img" aria-label={title + " · " + t("See observations table for per-session states.")}
        className="h-auto w-full min-w-[460px]">
        {states.map((state, index) => <g key={state} transform={`translate(0,${32 + index * 49})`}>
          <text x={LEFT - 9} y="4" textAnchor="end" fontSize="12" fill="currentColor">{fieldLabel(state, t)}</text>
          <path d={`M${LEFT},0H${RIGHT}`} stroke="currentColor" opacity=".2" />
          <path data-state={state} d={paths[index]} className={COLORS[index]} strokeWidth="2" />
        </g>)}
        <DateLabels rows={rows} />
      </svg>
    </div>
  </figure>;
}
