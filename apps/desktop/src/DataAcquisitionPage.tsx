import { PageHeader } from "./PageHeader";
import { useLocale } from "./localization";
import { useState } from "react";
import { BarChart3, CheckCircle2, Database, History, ShieldCheck } from "lucide-react";
import { AcquisitionCard } from "./AcquisitionCard";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import type { AcquisitionProvider } from "./market-acquisition-api";

export interface MarketSelection {
  symbol: string;
  provider: AcquisitionProvider;
}

/** Acquisition actions and their completed-import history live outside Markets. */
export function DataAcquisitionPage({
  onViewMarket,
}: {
  onViewMarket: (selection?: MarketSelection) => void;
}) {
  const { t } = useLocale();
  const [latest, setLatest] = useState<MarketSelection | null>(null);

  return (
    <section className="min-w-0 space-y-2" aria-labelledby="acquisition-page-title">
      <PageHeader id="acquisition-page-title" breadcrumb={t("FinIntel / Data Acquisition")} title={t("Data Acquisition")} subtitle={t("Fetch, validate, and store provider data. Review completed import actions separately from market analysis.")} actions={<><Badge variant="outline">{t("Local SQLite")}</Badge>
            <Button type="button" variant="secondary" size="sm" onClick={() => onViewMarket(latest ?? undefined)}>
              <BarChart3 aria-hidden="true" /> Open Markets
            </Button></>} />

      <div className="grid min-w-0 items-start gap-2 2xl:grid-cols-[minmax(0,1fr)_300px]">
        <div className="min-w-0 space-y-2">
          <AcquisitionCard
            selectedSymbol=""
            onAcquired={(symbol, provider) => {
              setLatest({ symbol, provider });
            }}
          />
          {latest && (
            <div role="status" aria-live="polite" className="flex min-w-0 flex-wrap items-center justify-between gap-3 rounded-xl border border-positive/30 bg-positive/5 p-4 text-sm">
              <span className="flex min-w-0 items-center gap-2"><CheckCircle2 aria-hidden="true" className="size-4 shrink-0 text-positive" /> {t("Stored")} {latest.symbol} {t("from")} {latest.provider === "alpaca_sip" ? "Alpaca SIP" : latest.provider === "tiingo" ? "Tiingo" : "Alpha Vantage"}.</span>
              <Button type="button" size="sm" variant="secondary" onClick={() => onViewMarket(latest)}>
                <BarChart3 aria-hidden="true" /> View in Markets
              </Button>
            </div>
          )}
        </div>
        <aside className="grid min-w-0 gap-2 sm:grid-cols-2 2xl:grid-cols-1" aria-label={t("Acquisition guidance")}>
          <Card className="min-w-0">
            <CardHeader className="gap-y-2">
              <CardTitle className="flex items-center gap-2"><Database aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" /> {t("Local data workflow")}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-xs leading-6 text-muted-foreground">
              <p>{t("Choose a stock or ETF, select the provider and history window, then fetch the observations into local SQLite.")}</p>
              <p>{t("Manual acquisition on this page is independent of the historical backfill running from your terminal.")}</p>
            </CardContent>
          </Card>
          <Card className="min-w-0">
            <CardHeader className="gap-y-2">
              <CardTitle className="flex items-center gap-2"><ShieldCheck aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" /> {t("Data integrity")}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-xs leading-6 text-muted-foreground">
              <p>{t("Price observations are validated before storage. Raw prices are not split or dividend adjusted.")}</p>
              <p>{t("Provider access and historical availability depend on your account and the instrument.")}</p>
            </CardContent>
          </Card>
          <div className="flex min-w-0 items-start gap-3 rounded-lg border border-border p-4 text-xs leading-6 text-muted-foreground sm:col-span-2 2xl:col-span-1">
            <History aria-hidden="true" className="mt-1 size-4 shrink-0" />
            <p>{t("Completed imports are available in Action History. This page shows only the most recent manual download result.")}</p>
          </div>
        </aside>
      </div>
    </section>
  );
}
