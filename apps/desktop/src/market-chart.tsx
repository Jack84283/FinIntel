import { useLocale } from "./localization";
import type { DailyBar } from "./market";
import { chartBars, chartInterval, type HistoryPeriod } from "./history-range";
import type { IndexCloseRecord } from "./index-history-api";

/** Deliberately lightweight SVG: no new chart dependency or third-party data feed. */
export function MarketCharts({ bars, period }: { bars: DailyBar[]; period: HistoryPeriod }) {
  const { t } = useLocale();
  const interval = chartInterval(period, bars.length);
  const visible = chartBars(bars, interval);
  if (visible.length === 0) return <p className="py-10 text-center text-sm text-muted-foreground">{t("No observations in this date range.")}</p>;

  const width = 840;
  const left = 56;
  const right = 18;
  const innerWidth = width - left - right;
  const plotTop = 18;
  const plotBottom = 238;
  const maxPrice = Math.max(...visible.map((bar) => Number(bar.high)));
  const minPrice = Math.min(...visible.map((bar) => Number(bar.low)));
  const span = Math.max((maxPrice - minPrice) * 1.08, 0.01);
  const floor = minPrice - span * 0.04;
  const step = innerWidth / visible.length;
  const candleWidth = Math.max(2, Math.min(step * 0.58, 10));
  const yPrice = (value: number) => plotBottom - (value - floor) / span * (plotBottom - plotTop);
  const maxVolume = Math.max(1, ...visible.map((bar) => bar.volume));
  const ticks = Array.from({ length: 5 }, (_, i) => floor + span * i / 4);
  const labels = [0, Math.floor((visible.length - 1) / 2), visible.length - 1]
    .filter((index, i, arr) => arr.indexOf(index) === i);

  return (
    <div className="space-y-2">
      <div className="overflow-x-auto rounded-md border border-border bg-muted/40 p-2">
        <svg viewBox="0 0 840 280" role="img" aria-label={`${t("Historical")} ${t(interval.toLowerCase())} ${t("candlestick chart showing")} ${visible.length} ${t("intervals")}`} className="h-auto min-w-[560px] w-full">
          {ticks.map((tick) => (
            <g key={tick}>
              <line x1={left} x2={width - right} y1={yPrice(tick)} y2={yPrice(tick)} stroke="#2b394b" strokeDasharray="3 4" />
              <text x={left - 7} y={yPrice(tick) + 4} textAnchor="end" fontSize="11" fill="#a5b4c7">{tick.toFixed(2)}</text>
            </g>
          ))}
          {visible.map((bar, index) => {
            const x = left + step * (index + 0.5);
            const open = yPrice(Number(bar.open));
            const close = yPrice(Number(bar.close));
            const up = Number(bar.close) >= Number(bar.open);
            const color = up ? "#4bbf91" : "#eb7777";
            return (
              <g key={bar.session_date}>
                <line x1={x} x2={x} y1={yPrice(Number(bar.high))} y2={yPrice(Number(bar.low))} stroke={color} strokeWidth="1.4" />
                <rect x={x - candleWidth / 2} y={Math.min(open, close)} width={candleWidth} height={Math.max(1.5, Math.abs(close - open))} fill={color}>
                  <title>{bar.session_date + ": O " + bar.open + " H " + bar.high + " L " + bar.low + " C " + bar.close}</title>
                </rect>
              </g>
            );
          })}
          {labels.map((index) => (
            <text key={visible[index].session_date} x={left + step * (index + 0.5)} y={264} fontSize="11" textAnchor="middle" fill="#a5b4c7">
              {visible[index].session_date}
            </text>
          ))}
        </svg>
      </div>
      <div className="overflow-x-auto rounded-md border border-border bg-muted/40 p-2">
        <p className="px-2 pt-1 text-xs font-medium text-muted-foreground">{t("Trading volume")} · {t(interval.toLowerCase())} {t("aggregation")}</p>
        <svg viewBox="0 0 840 105" role="img" aria-label={`${t("Trading volume by")} ${t(interval.toLowerCase())} ${t("interval for")} ${visible.length} ${t("intervals")}`} className="h-auto min-w-[560px] w-full">
          <text x={left - 7} y={12} textAnchor="end" fontSize="11" fill="#a5b4c7">
            {new Intl.NumberFormat("en", { notation: "compact" }).format(maxVolume)}
          </text>
          <line x1={left} x2={width - right} y1="86" y2="86" stroke="#2b394b" />
          {visible.map((bar, index) => {
            const x = left + step * (index + 0.5);
            const height = bar.volume / maxVolume * 76;
            return (
              <rect
                key={bar.session_date}
                x={x - candleWidth / 2}
                y={86 - height}
                width={candleWidth}
                height={Math.max(1, height)}
                fill={Number(bar.close) >= Number(bar.open) ? "#4bbf91" : "#eb7777"}
                opacity={0.85}
              >
                <title>{bar.session_date + ": " + bar.volume.toLocaleString() + " shares"}</title>
              </rect>
            );
          })}
        </svg>
      </div>
      <p className="text-xs text-muted-foreground">{t("Chart represents")} {bars.length.toLocaleString()} {t("actual daily sessions across")} {visible.length} {t(interval.toLowerCase())} {t("intervals. Green means close ≥ open; red means close < open. Prices are raw, not corporate-action adjusted.")}</p>
    </div>
  );
}

export function IndexLineChart({ observations }: { observations: IndexCloseRecord[] }) {
  const { t } = useLocale();
  if (observations.length === 0) {
    return <p className="py-10 text-center text-sm text-muted-foreground">{t("No observations in this date range.")}</p>;
  }

  const width = 840;
  const left = 64;
  const right = 18;
  const top = 18;
  const bottom = 238;
  const innerWidth = width - left - right;
  const values = observations.map((row) => Number(row.close));
  const maximum = Math.max(...values);
  const minimum = Math.min(...values);
  const padding = Math.max((maximum - minimum) * 0.08, 0.01);
  const floor = minimum - padding;
  const ceiling = maximum + padding;
  const span = Math.max(ceiling - floor, 0.01);
  const x = (index: number) => left + (index / Math.max(observations.length - 1, 1)) * innerWidth;
  const y = (value: number) => bottom - ((value - floor) / span) * (bottom - top);
  const path = observations.map((row, index) => `${index === 0 ? "M" : "L"}${x(index).toFixed(2)},${y(Number(row.close)).toFixed(2)}`).join(" ");
  const ticks = Array.from({ length: 5 }, (_, index) => floor + span * index / 4);
  const labels = [0, Math.floor((observations.length - 1) / 2), observations.length - 1]
    .filter((index, indexInArray, all) => all.indexOf(index) === indexInArray);
  const formatLevel = (value: number) => new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(value);

  return (
    <div className="overflow-x-auto rounded-md border border-border bg-muted/40 p-2">
      <svg viewBox="0 0 840 280" role="img" aria-label={`${t("Historical index closing-level line chart")} ${observations.length} ${t("observations")}`} className="h-auto min-w-[560px] w-full">
        {ticks.map((tick) => (
          <g key={tick}>
            <line x1={left} x2={width - right} y1={y(tick)} y2={y(tick)} stroke="#2b394b" strokeDasharray="3 4" />
            <text x={left - 7} y={y(tick) + 4} textAnchor="end" fontSize="11" fill="#a5b4c7">{formatLevel(tick)}</text>
          </g>
        ))}
        <path d={path} fill="none" stroke="#5c9ded" strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round" />
        {observations.map((row, index) => (
          <circle key={row.session_date} cx={x(index)} cy={y(Number(row.close))} r={observations.length > 120 ? 1.5 : 2.5} fill="#5c9ded">
            <title>{row.session_date + ": " + row.close}</title>
          </circle>
        ))}
        {labels.map((index) => (
          <text key={observations[index].session_date} x={x(index)} y={264} fontSize="11" textAnchor="middle" fill="#a5b4c7">
            {observations[index].session_date}
          </text>
        ))}
      </svg>
    </div>
  );
}
