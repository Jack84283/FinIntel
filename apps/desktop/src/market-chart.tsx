import { useLocale } from "./localization";
import type { DailyBar } from "./market";
import { chartBars, chartInterval, type HistoryPeriod } from "./history-range";

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
        <p className="px-2 pt-1 text-xs font-medium text-muted-foreground">Trading volume · {interval.toLowerCase()} aggregation</p>
        <svg viewBox="0 0 840 105" role="img" aria-label={"Trading volume by " + interval.toLowerCase() + " interval for " + visible.length + " intervals"} className="h-auto min-w-[560px] w-full">
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
      <p className="text-xs text-muted-foreground">Chart represents {bars.length.toLocaleString()} actual daily sessions across {visible.length} {interval.toLowerCase()} intervals. Green means close ≥ open; red means close &lt; open. Prices are raw, not corporate-action adjusted.</p>
    </div>
  );
}
