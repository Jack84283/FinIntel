import { useState } from "react";
import { BarChart3 } from "lucide-react";
import { AcquisitionCard } from "./AcquisitionCard";
import { Badge } from "@/components/ui/badge";
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
  const [latest, setLatest] = useState<MarketSelection | null>(null);

  return () => { cancelled = true; };
  }, [revision]);

  return (
    <section className="space-y-2" aria-labelledby="acquisition-page-title">
      <header className="flex flex-wrap items-start justify-between gap-4 border-b border-border pb-5">
        <div>
          <p className="text-[10px] font-bold tracking-[0.16em] text-[#8aa5c3] uppercase">FinIntel / Data Acquisition</p>
          <h1 id="acquisition-page-title" className="mt-2 text-2xl font-semibold tracking-tight">Data Acquisition</h1>
          <p className="mt-2 text-sm text-muted-foreground">Fetch, validate, and store provider data. Review completed import actions separately from market analysis.</p>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="outline">Local SQLite</Badge>
          <Button type="button" variant="secondary" size="sm" onClick={() => onViewMarket(latest ?? undefined)}>
            <BarChart3 aria-hidden="true" /> Open Markets
          </Button>
        </div>
      </header>

      <AcquisitionCard
        selectedSymbol=""
        onAcquired={(symbol, provider) => {
          setLatest({ symbol, provider });
        }}
      />

      {latest && (
        <div role="status" className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border px-3 py-2 text-xs">
          <span>Stored {latest.symbol} from {latest.provider === "alpaca_sip" ? "Alpaca SIP" : "Alpha Vantage"} successfully.</span>
          <Button type="button" size="sm" variant="secondary" onClick={() => onViewMarket(latest)}>
            <BarChart3 aria-hidden="true" /> View in Markets
          </Button>
        </div>
      )}

      <p className="text-xs text-muted-foreground">Completed imports are available in the dedicated Action History section. Only successful persisted import records are currently tracked.</p>
    </section>
  );
}
