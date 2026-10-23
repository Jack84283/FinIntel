import { useEffect, useState } from "react";
import { ArrowDownToLine, BarChart3, History, RefreshCw } from "lucide-react";
import { AcquisitionCard } from "./AcquisitionCard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { fetchMarketImports } from "./market-api";
import type { ImportRecord } from "./market";
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
  const [history, setHistory] = useState<ImportRecord[]>([]);
  const [historyState, setHistoryState] = useState<"loading" | "ready" | "error">("loading");
  const [historyError, setHistoryError] = useState("");
  const [revision, setRevision] = useState(0);
  const [latest, setLatest] = useState<MarketSelection | null>(null);

  useEffect(() => {
    let cancelled = false;
    setHistoryState("loading");
    fetchMarketImports(25)
      .then((items) => {
        if (cancelled) return;
        setHistory(items);
        setHistoryState("ready");
        setHistoryError("");
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setHistoryState("error");
        setHistoryError(error instanceof Error ? error.message : String(error));
      });
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
          setRevision((value) => value + 1);
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

      <Card aria-labelledby="action-history-title">
        <CardHeader>
          <CardTitle>
            <span id="action-history-title" className="flex items-center gap-2">
              <History aria-hidden="true" className="size-4" /> Action History
            </span>
          </CardTitle>
          <CardAction>
            <Button type="button" variant="secondary" size="sm" disabled={historyState === "loading"} onClick={() => setRevision((value) => value + 1)}>
              <RefreshCw aria-hidden="true" /> Refresh history
            </Button>
          </CardAction>
          <CardDescription className="col-span-2">
            Last 25 completed import actions, including CLI imports. Each row is a persisted ingestion run; failed requests are not yet recorded.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {historyState === "loading" && <Skeleton aria-label="Loading acquisition history" className="h-24 w-full" />}
          {historyState === "error" && (
            <div role="alert" className="space-y-3 text-sm">
              <p className="text-destructive">Could not load acquisition history: {historyError}</p>
              <Button type="button" size="sm" variant="secondary" onClick={() => setRevision((value) => value + 1)}>Retry</Button>
            </div>
          )}
          {historyState === "ready" && history.length === 0 && (
            <p className="py-5 text-sm text-muted-foreground">No completed imports yet. Fetch and store a stock or ETF above to create the first history record.</p>
          )}
          {historyState === "ready" && history.length > 0 && (
            <div className="overflow-x-auto rounded-md border border-border">
              <table className="w-full min-w-[850px] text-left text-xs tabular-nums">
                <thead className="bg-muted text-muted-foreground">
                  <tr>
                    <th scope="col" className="px-3 py-2 font-medium">Symbol</th>
                    <th scope="col" className="px-3 py-2 font-medium">Provider</th>
                    <th scope="col" className="px-3 py-2 font-medium">Imported (UTC)</th>
                    <th scope="col" className="px-3 py-2 font-medium">Sessions</th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">New</th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">Updated</th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">Unchanged</th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {history.map((item) => (
                    <tr key={item.id} className="border-t border-border hover:bg-muted/50">
                      <td className="px-3 py-2 font-semibold">{item.symbol}</td>
                      <td className="px-3 py-2">{item.provider === "alpaca_sip" ? "Alpaca SIP" : item.provider === "alpha_vantage" ? "Alpha Vantage" : item.provider}</td>
                      <td className="px-3 py-2">{item.imported_at}</td>
                      <td className="px-3 py-2">{item.first_session} – {item.last_session}</td>
                      <td className="px-3 py-2 text-right">{item.inserted.toLocaleString()}</td>
                      <td className="px-3 py-2 text-right">{item.updated.toLocaleString()}</td>
                      <td className="px-3 py-2 text-right">{item.unchanged.toLocaleString()}</td>
                      <td className="px-3 py-2 text-right">
                        {item.provider === "alpaca_sip" || item.provider === "alpha_vantage" ? (
                          <Button size="sm" variant="ghost" type="button" onClick={() => onViewMarket({ symbol: item.symbol, provider: item.provider as AcquisitionProvider })}>
                            <BarChart3 aria-hidden="true" /> View
                          </Button>
                        ) : <span className="text-muted-foreground">—</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
      <footer className="text-xs text-muted-foreground">
        <ArrowDownToLine className="mr-1 inline size-3" aria-hidden="true" />
        Provider requests are always explicit · Import history contains successful persisted runs only.
      </footer>
    </section>
  );
}
