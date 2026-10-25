import { useEffect, useState } from "react";
import { BarChart3, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { searchMarketImports } from "./market-api";
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
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  const [searchTerm, setSearchTerm] = useState("");
  const pageSize = 20;

  useEffect(() => {
    const timer = window.setTimeout(() => { setPage(0); setSearchTerm(query.trim()); }, 180);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    let cancelled = false;
    setState("loading");
    searchMarketImports(searchTerm, provider === "all" ? "" : provider, page * pageSize, pageSize)
      .then((result) => {
        if (cancelled) return;
        setItems(result.items);
        setTotal(result.total);
        setError("");
        setState("ready");
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        setError(reason instanceof Error ? reason.message : String(reason));
        setState("error");
      });
    return () => { cancelled = true; };
  }, [revision, searchTerm, provider, page]);

  const pageCount = Math.max(1, Math.ceil(total / pageSize));

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
          <CardDescription>Search all persisted successful imports (including CLI). Failed requests, model executions, and AI actions are not yet captured by the current history store.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap gap-3">
            <input aria-label="Search history" placeholder="Search symbol, company, ETF or provider" value={query} onChange={(event) => setQuery(event.target.value)} className="h-9 min-w-48 flex-1 rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
            <select aria-label="Filter provider" value={provider} onChange={(event) => { setProvider(event.target.value); setPage(0); }} className="h-9 rounded-md border border-input bg-background px-3 text-sm">
              <option value="all">All providers</option>
              <option value="alpha_vantage">Alpha Vantage</option>
              <option value="alpaca_sip">Alpaca SIP</option>
              <option value="tiingo">Tiingo</option>
            </select>
          </div>
          {state === "loading" && <Skeleton aria-label="Loading action history" className="h-28 w-full" />}
          {state === "error" && <div role="alert" className="space-y-2 text-sm"><p className="text-destructive">Could not load history: {error}</p><Button size="sm" type="button" variant="secondary" onClick={() => { setState("loading"); setRevision((value) => value + 1); }}>Retry</Button></div>}
          {state === "ready" && total === 0 && !searchTerm && provider === "all" && <p className="py-6 text-sm text-muted-foreground">No completed imports yet. Use Data Acquisition to fetch and store historical market data.</p>}
          {state === "ready" && total === 0 && (Boolean(searchTerm) || provider !== "all") && <p className="py-6 text-sm text-muted-foreground">No history records match these filters.</p>}
          {state === "ready" && items.length > 0 && (
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
                <tbody>{items.map((item) => (
                  <tr key={item.id} className="border-t border-border hover:bg-muted/50">
                    <td className="px-3 py-2">{item.imported_at}</td>
                    <td className="px-3 py-2">Import</td>
                    <td className="px-3 py-2 font-semibold">{item.symbol}</td>
                    <td className="px-3 py-2">{item.provider === "alpaca_sip" ? "Alpaca SIP" : item.provider === "alpha_vantage" ? "Alpha Vantage" : item.provider}</td>
                    <td className="px-3 py-2"><Badge variant="success">Success</Badge></td>
                    <td className="px-3 py-2 text-right">{item.inserted.toLocaleString()} / {item.updated.toLocaleString()} / {item.unchanged.toLocaleString()}</td>
                    <td className="px-3 py-2 text-right">
                      {item.provider === "alpaca_sip" || item.provider === "alpha_vantage" || item.provider === "tiingo"
                        ? <Button type="button" size="sm" variant="ghost" onClick={() => onViewMarket({ symbol: item.symbol, provider: item.provider as AcquisitionProvider })}><BarChart3 aria-hidden="true" /> View</Button>
                        : "—"}
                    </td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
          <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
            <p className="text-muted-foreground" aria-live="polite">{total ? `Showing ${page * pageSize + 1}–${Math.min((page + 1) * pageSize, total)} of ${total.toLocaleString()} imports` : "No matching imports"} · Page {page + 1} of {pageCount}</p>
            <div className="flex items-center gap-2">
              <Button type="button" variant="secondary" size="sm" disabled={state !== "ready" || page === 0} onClick={() => setPage((v) => Math.max(0, v - 1))}>Previous</Button>
              <Button type="button" variant="secondary" size="sm" disabled={state !== "ready" || page + 1 >= pageCount} onClick={() => setPage((v) => v + 1)}>Next</Button>
            </div>
          </div>
          <p className="text-xs text-muted-foreground">Full local SQLite import-history search, 20 records per page. Only successful imports are recorded here.</p>
        </CardContent>
      </Card>
    </section>
  );
}
