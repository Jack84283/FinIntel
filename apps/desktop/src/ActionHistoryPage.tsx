import { useEffect, useMemo, useState } from "react";
import { BarChart3, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { fetchMarketImports } from "./market-api";
import { searchSymbols } from "./symbol-catalog-api";
import type { ImportRecord } from "./market";
import type { MarketSelection } from "./DataAcquisitionPage";
import type { AcquisitionProvider } from "./market-acquisition-api";

/** Read-only history of successful persisted imports. No events are fabricated. */
export function ActionHistoryPage({ onViewMarket }: { onViewMarket: (selection?: MarketSelection) => void }) {
  const [items, setItems] = useState<ImportRecord[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [query, setQuery] = useState("");
  const [provider, setProvider] = useState("all");
  const [nameMatches, setNameMatches] = useState<string[]>([]);
  const [nameSearchPending, setNameSearchPending] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchMarketImports(100).then((records) => {
      if (cancelled) return;
      setItems(records);
      setError("");
      setState("ready");
    }).catch((reason: unknown) => {
      if (cancelled) return;
      setError(reason instanceof Error ? reason.message : String(reason));
      setState("error");
    });
    return () => { cancelled = true; };
  }, [revision]);

  useEffect(() => {
    const text = query.trim();
    if (!text) {
      setNameMatches([]);
      setNameSearchPending(false);
      return;
    }
    let cancelled = false;
    setNameSearchPending(true);
    const timeout = window.setTimeout(() => {
      searchSymbols(text, "All", 100)
        .then((result) => {
          if (!cancelled) setNameMatches(result.results.map((item) => item.symbol));
        })
        .catch(() => {
          // Keep ticker and provider filtering usable without a symbol catalog.
          if (!cancelled) setNameMatches([]);
        })
        .finally(() => { if (!cancelled) setNameSearchPending(false); });
    }, 180);
    return () => { cancelled = true; window.clearTimeout(timeout); };
  }, [query]);

  const filtered = useMemo(() => items.filter((record) =>
    (provider === "all" || record.provider === provider) &&
    (record.symbol.toLowerCase().includes(query.trim().toLowerCase()) ||
     record.provider.toLowerCase().includes(query.trim().toLowerCase()) ||
     nameMatches.includes(record.symbol))
  ), [items, provider, query, nameMatches]);

  return (
    <section className="space-y-2" aria-labelledby="action-history-title">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-2">
        <div>
          <p className="text-[10px] font-bold tracking-[0.16em] text-[#8aa5c3] uppercase">FinIntel / Data & Operations</p>
          <h1 id="action-history-title" className="mt-2 text-2xl font-semibold tracking-tight">Action History</h1>
          <p className="mt-2 text-sm text-muted-foreground">Audit successful local ingestion actions separately from market analysis and acquisition controls.</p>
        </div>
        <Button type="button" variant="secondary" size="sm" disabled={state === "loading"} onClick={() => { setState("loading"); setRevision((value) => value + 1); }}>
          <RefreshCw aria-hidden="true" /> Refresh
        </Button>
      </header>
      <Card>
        <CardHeader className="flex flex-col items-start gap-2">
          <CardTitle>Completed imports</CardTitle>
          <CardDescription>Most recent 100 persisted successful imports (including CLI). Failed requests, model executions, and AI actions are not yet captured by the current history store.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap gap-3">
            <input aria-label="Search history" placeholder="Search symbol, company, ETF or provider" value={query} onChange={(event) => setQuery(event.target.value)} className="h-9 min-w-48 flex-1 rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
            <select aria-label="Filter provider" value={provider} onChange={(event) => setProvider(event.target.value)} className="h-9 rounded-md border border-input bg-background px-3 text-sm">
              <option value="all">All providers</option>
              <option value="alpha_vantage">Alpha Vantage</option>
              <option value="alpaca_sip">Alpaca SIP</option>
            </select>
          </div>
          {state === "loading" && <Skeleton aria-label="Loading action history" className="h-28 w-full" />}
          {state === "error" && <div role="alert" className="space-y-2 text-sm"><p className="text-destructive">Could not load history: {error}</p><Button size="sm" type="button" variant="secondary" onClick={() => { setState("loading"); setRevision((value) => value + 1); }}>Retry</Button></div>}
          {state === "ready" && items.length === 0 && <p className="py-6 text-sm text-muted-foreground">No completed imports yet. Use Data Acquisition to fetch and store historical market data.</p>}
          {state === "ready" && items.length > 0 && filtered.length === 0 && !nameSearchPending && <p className="py-6 text-sm text-muted-foreground">No history records match these filters.</p>}
          {state === "ready" && filtered.length > 0 && (
            <div className="overflow-x-auto rounded-md border border-border">
              <table className="w-full min-w-[840px] text-left text-xs tabular-nums">
                <thead className="bg-muted text-muted-foreground"><tr>
                  <th scope="col" className="px-3 py-2">Imported (UTC)</th>
                  <th scope="col" className="px-3 py-2">Action</th>
                  <th scope="col" className="px-3 py-2">Symbol</th>
                  <th scope="col" className="px-3 py-2">Provider</th>
                  <th scope="col" className="px-3 py-2">Status</th>
                  <th scope="col" className="px-3 py-2 text-right">New / Updated / Unchanged</th>
                  <th scope="col" className="px-3 py-2 text-right">Details</th>
                </tr></thead>
                <tbody>{filtered.map((item) => (
                  <tr key={item.id} className="border-t border-border hover:bg-muted/50">
                    <td className="px-3 py-2">{item.imported_at}</td>
                    <td className="px-3 py-2">Import</td>
                    <td className="px-3 py-2 font-semibold">{item.symbol}</td>
                    <td className="px-3 py-2">{item.provider === "alpaca_sip" ? "Alpaca SIP" : item.provider === "alpha_vantage" ? "Alpha Vantage" : item.provider}</td>
                    <td className="px-3 py-2"><Badge variant="success">Success</Badge></td>
                    <td className="px-3 py-2 text-right">{item.inserted.toLocaleString()} / {item.updated.toLocaleString()} / {item.unchanged.toLocaleString()}</td>
                    <td className="px-3 py-2 text-right">
                      {item.provider === "alpaca_sip" || item.provider === "alpha_vantage"
                        ? <Button type="button" size="sm" variant="ghost" onClick={() => onViewMarket({ symbol: item.symbol, provider: item.provider as AcquisitionProvider })}><BarChart3 aria-hidden="true" /> View</Button>
                        : "—"}
                    </td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
          <p className="text-xs text-muted-foreground">This view is read-only and uses the local SQLite import log. Results are limited to the latest 100 records; it is not a complete audit trail of failures or user interactions.</p>
        </CardContent>
      </Card>
    </section>
  );
}
