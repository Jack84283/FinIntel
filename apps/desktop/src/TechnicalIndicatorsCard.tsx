import { useMemo } from "react";
import { Activity } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { DailyBar } from "./market";
import { calculateTechnicalIndicators } from "./technical-indicators";
import { useLocale } from "./localization";

const format = (value: number | null, digits = 2) =>
  value === null ? "—" : new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(value);

export function TechnicalIndicatorsCard({ bars }: { bars: DailyBar[] }) {
  const { t } = useLocale();
  const result = useMemo(() => calculateTechnicalIndicators(bars), [bars]);
  const metrics = [
    { title: "SMA 20", value: result.sma20, sessions: 20 },
    { title: "SMA 50", value: result.sma50, sessions: 50 },
    { title: "SMA 200", value: result.sma200, sessions: 200 },
    { title: "RSI 14", value: result.rsi14, sessions: 15 },
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <span className="flex items-center gap-2">
            <Activity aria-hidden="true" className="size-4" />
            {t("Technical indicators")}
          </span>
        </CardTitle>
        <CardDescription>
          {t("Calculated locally from loaded daily close prices. Read-only; no data download.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-2 grid-cols-2 xl:grid-cols-4">
          {metrics.map(metric => (
            <div key={metric.title} className="min-w-0 rounded-md border border-border bg-muted/40 px-3 py-3">
              <p className="text-xs font-medium text-muted-foreground">{metric.title}</p>
              <p className="mt-1 text-xl font-semibold tabular-nums" aria-label={metric.title + ": " + (metric.value === null ? t("Insufficient history") : format(metric.value))}>
                {format(metric.value)}
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                {metric.value === null
                  ? t("Insufficient history")
                  : metric.title === "RSI 14"
                    ? t("0–100 momentum scale")
                    : t("Daily close average")}
              </p>
            </div>
          ))}
        </div>
        <p className="text-xs leading-relaxed text-muted-foreground">
          {t("Indicators use")} {result.sessions.toLocaleString()} {t("loaded daily sessions")}
          {result.sessionDate ? " · " + t("through") + " " + result.sessionDate : ""}.
          {" "}{t("SMA needs 20, 50 or 200 sessions; RSI needs 15. Insufficient history is shown as —. These are descriptive measures, not forecasts.")}
        </p>
      </CardContent>
    </Card>
  );
}
