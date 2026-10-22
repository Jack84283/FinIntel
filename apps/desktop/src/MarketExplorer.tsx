import { useEffect, useMemo, useState, type FormEvent } from "react";
import { AlertCircle, BarChart3, Database, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { fetchMarketDaily, fetchMarketImports, fetchMarketInstruments } from "./market-api";
import { MarketCharts } from "./market-chart";
import { sortableBars, type DailySeries, type ImportRecord, type MarketInstrument } from "./market";

type LoadState = "loading" | "ready" | "error";
type SortColumn = "session_date" | "close" | "volume";

const pageSize = 25;
const formatInteger = (value: number) => new Intl.NumberFormat("en-US").format(value);
const price = (raw: string) => new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 }).format(Number(raw));

export function MarketExplorer() {
  const [instruments, setInstruments] = useState<MarketInstrument[]>([]);
  const [indexState, setIndexState] = useState<LoadState>("loading");
  const [indexError, setIndexError] = useState("");
  const [symbol, setSymbol] = useState("");
  const [draftStart, setDraftStart] = useState("");
  const [draftEnd, setDraftEnd] = useState("");
  const [range, setRange] = useState({ start: "", end: "" });
  const [validationError, setValidationError] = useState("");
  const [series, setSeries] = useState<DailySeries | null>(null);
  const [seriesState, setSeriesState] = useState<LoadState>("loading");
  const [seriesError, setSeriesError] = useState("");
  const [imports, setImports] = useState<ImportRecord[]>([]);
  const [sortColumn, setSortColumn] = useState<SortColumn>("session_date");
  const [descending, setDescending] = useState(true);
  const [page, setPage] = useState(0);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let ignore = false;
    setIndexState("loading");
    Promise.all([fetchMarketInstruments(), fetchMarketImports(10)])
      .then(([catalog, history]) => {
        if (ignore) return;
        setInstruments(catalog);
        setImports(history);
        setSymbol((previous) => catalog.some((item) => item.symbol === previous) ? previous : (catalog[0]?.symbol ?? ""));
        setIndexState("ready");
        setIndexError("");
      })
      .catch((error: unknown) => {
        if (ignore) return;
        setIndexState("error");
        setIndexError(error instanceof Error ? error.message : String(error));
      });
    return () => { ignore = true; };
  }, [reload]);

  useEffect(() => {
    if (!symbol || indexState !== "ready") return;
    let ignore = false;
    setSeries(null);
    setSeriesState("loading");
    setPage(0);
    fetchMarketDaily({
      symbol,
      startDate: range.start || undefined,
      endDate: range.end || undefined,
      limit: 1000,
    })
      .then((data) => {
        if (ignore) return;
        setSeries(data);
        setSeriesState("ready");
        setSeriesError("");
      })
      .catch((error: unknown) => {
        if (ignore) return;
        setSeriesState("error");
        setSeriesError(error instanceof Error ? error.message : String(error));
      });
    return () => { ignore = true; };
  }, [symbol, range, reload, indexState]);

  const bars = series?.observations ?? [];
  const sorted = useMemo(() => sortableBars(bars, sortColumn, descending), [bars, sortColumn, descending]);
  const pageCount = Math.max(1, Math.ceil(sorted.length / pageSize));
  const visibleRows = sorted.slice(page * pageSize, (page + 1) * pageSize);
  const currentInstrument = instruments.find((item) => item.symbol === symbol);
  const last = bars[bars.length - 1];
  const first = bars[0];
  const prior = bars.length > 1 ? bars[bars.length - 2] : null;
  const closeChange = last && prior ? Number(last.close) - Number(prior.close) : null;

  const applyRange = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (draftStart && draftEnd && draftStart > draftEnd) {
      setValidationError("Start date must not exceed end date.");
      return;
    }
    setValidationError("");
    setRange({ start: draftStart, end: draftEnd });
  };

  const sortBy = (column: SortColumn) => {
    setPage(0);
    if (sortColumn === column) {
      setDescending((previous) => !previous);
    } else {
      setSortColumn(column);
      setDescending(true);
    }
  };

  const sortLabel = (column: SortColumn, label: string) =>
    label + (sortColumn === column ? (descending ? " ↓" : " ↑") : "");

  return (
    <section className="space-y-5" aria-labelledby="markets-title">
      <header className="flex flex-wrap items-start justify-between gap-4 border-b border-border pb-5">
        <div>
          <p className="text-[10px] font-bold tracking-[0.16em] text-[#8aa5c3] uppercase">FinIntel / Markets</p>
          <h1 id="markets-title" className="mt-2 text-2xl font-semibold tracking-tight">Market Data Explorer</h1>
          <p className="mt-2 text-sm text-muted-foreground">Historical daily prices stored in local SQLite. No live quotes.</p>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="outline">Local / read only</Badge>
          <Button type="button" variant="secondary" size="sm" onClick={() => setReload((v) => v + 1)}>
            <RefreshCw aria-hidden="true" /> Reload local data
          </Button>
        </div>
      </header>

      {indexState === "loading" && (
        <Card><CardContent><Skeleton className="h-8 w-64" aria-label="Loading local instruments" /></CardContent></Card>
      )}
      {indexState === "error" && (
        <Card role="alert"><CardHeader><CardTitle>Local database unavailable</CardTitle></CardHeader>
          <CardContent className="space-y-3 text-sm text-muted-foreground">
            <p>{indexError}</p><p>Start the Python service on 127.0.0.1:8765 and try again.</p>
            <Button onClick={() => setReload((v) => v + 1)} variant="secondary">Retry</Button>
          </CardContent>
        </Card>
      )}
      {indexState === "ready" && instruments.length === 0 && (
        <Card>
          <CardHeader><CardTitle>No stored market data yet</CardTitle>
            <CardDescription>FinIntel reads your local SQLite database. Import a previously downloaded snapshot to populate the Explorer.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex items-center gap-2 text-muted-foreground"><Database aria-hidden="true" className="size-5" /><span className="text-sm">No symbols are available.</span></div>
            <code className="block overflow-x-auto rounded-md bg-muted px-3 py-3 text-xs">python -m finintel.storage import data/raw/AAPL.json</code>
            <p className="text-xs text-muted-foreground">Run the command from your FinIntel repository root using the same database path as the backend. Import is intentionally not triggered from this page.</p>
          </CardContent>
        </Card>
      )}

      {indexState === "ready" && instruments.length > 0 && (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Instrument & time range</CardTitle>
              <CardAction><Badge variant="secondary">{instruments.length} local series</Badge></CardAction>
            </CardHeader>
            <CardContent>
              <form onSubmit={applyRange} className="flex flex-wrap items-end gap-3">
                <label className="grid min-w-[155px] flex-1 gap-2 text-xs font-medium text-muted-foreground">
                  Instrument
                  <select value={symbol} onChange={(e) => setSymbol(e.target.value)} className="h-10 rounded-md border border-input bg-muted px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                    {instruments.map((item) => <option key={item.symbol + item.provider} value={item.symbol}>{item.symbol} · {formatInteger(item.sessions)} sessions</option>)}
                  </select>
                </label>
                <label className="grid min-w-[155px] flex-1 gap-2 text-xs font-medium text-muted-foreground">
                  From (optional)
                  <input type="date" value={draftStart} onChange={(e) => setDraftStart(e.target.value)} className="h-10 rounded-md border border-input bg-muted px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
                </label>
                <label className="grid min-w-[155px] flex-1 gap-2 text-xs font-medium text-muted-foreground">
                  To (optional)
                  <input type="date" value={draftEnd} onChange={(e) => setDraftEnd(e.target.value)} className="h-10 rounded-md border border-input bg-muted px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
                </label>
                <Button type="submit" size="lg">Apply range</Button>
                <Button type="button" size="lg" variant="ghost" onClick={() => {
                  setDraftStart(""); setDraftEnd(""); setRange({ start: "", end: "" }); setValidationError("");
                }}>Clear</Button>
              </form>
              {validationError && <p role="alert" className="mt-3 text-sm text-destructive">{validationError}</p>}
            </CardContent>
          </Card>

          {seriesState === "loading" && (
            <Card><CardContent className="space-y-4"><Skeleton className="h-7 w-44" aria-label="Loading historical prices" /><Skeleton className="h-72 w-full" /></CardContent></Card>
          )}
          {seriesState === "error" && (
            <Card role="alert"><CardContent className="space-y-3">
              <p className="flex items-center gap-2 text-sm text-destructive"><AlertCircle aria-hidden="true" className="size-4" />Could not load stored prices for {symbol}.</p>
              <p className="text-xs text-muted-foreground">{seriesError}</p>
              <Button variant="secondary" onClick={() => setReload((v) => v + 1)}>Retry local query</Button>
            </CardContent></Card>
          )}
          {seriesState === "ready" && series && (
            <>
              <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
                <Metric label="Instrument" value={series.symbol} note={currentInstrument ? formatInteger(currentInstrument.sessions) + " stored sessions" : series.provider} />
                <Metric label="Last close" value={last ? price(last.close) : "—"} note={last?.session_date ?? "No trading sessions"} />
                <Metric label="Daily change" value={closeChange === null ? "—" : (closeChange >= 0 ? "+" : "") + price(String(closeChange))} note="Compared with prior loaded session" />
                <Metric label="Loaded observations" value={formatInteger(bars.length)} note={series.has_more ? "Latest 1,000 only · more stored" : "Complete selected range"} />
              </div>

              <Card>
                <CardHeader>
                  <CardTitle><span className="flex items-center gap-2"><BarChart3 aria-hidden="true" className="size-5" /> OHLCV history</span></CardTitle>
                  <CardAction><Badge variant="outline">{series.price_adjustment.replaceAll("_", " ")}</Badge></CardAction>
                  <CardDescription className="col-span-2 mt-2">{first && last ? first.session_date + " – " + last.session_date : "No sessions in selected range"} · historical end-of-day data</CardDescription>
                </CardHeader>
                <CardContent><MarketCharts bars={bars} /></CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle>Historical price table</CardTitle>
                  <CardAction><Badge variant="secondary">{formatInteger(sorted.length)} loaded rows</Badge></CardAction>
                  <CardDescription className="col-span-2 mt-2">Sortable local data. Prices are displayed without assuming a currency.</CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="overflow-x-auto rounded-md border border-border">
                    <table className="w-full min-w-[690px] border-collapse text-right text-xs tabular-nums">
                      <thead className="bg-muted text-muted-foreground">
                        <tr>
                          <th scope="col" className="px-3 py-3 text-left"><button type="button" onClick={() => sortBy("session_date")} className="font-medium hover:text-foreground">{sortLabel("session_date", "Session")}</button></th>
                          <th scope="col" className="px-3 py-3 font-medium">Open</th>
                          <th scope="col" className="px-3 py-3 font-medium">High</th>
                          <th scope="col" className="px-3 py-3 font-medium">Low</th>
                          <th scope="col" className="px-3 py-3"><button type="button" onClick={() => sortBy("close")} className="font-medium hover:text-foreground">{sortLabel("close", "Close")}</button></th>
                          <th scope="col" className="px-3 py-3"><button type="button" onClick={() => sortBy("volume")} className="font-medium hover:text-foreground">{sortLabel("volume", "Volume")}</button></th>
                        </tr>
                      </thead>
                      <tbody>
                        {visibleRows.map((bar) => (
                          <tr key={bar.session_date} className="border-t border-border/80 hover:bg-muted/65">
                            <td className="px-3 py-2.5 text-left font-medium text-foreground">{bar.session_date}</td>
                            <td className="px-3 py-2.5">{price(bar.open)}</td>
                            <td className="px-3 py-2.5">{price(bar.high)}</td>
                            <td className="px-3 py-2.5">{price(bar.low)}</td>
                            <td className="px-3 py-2.5 font-semibold">{price(bar.close)}</td>
                            <td className="px-3 py-2.5">{formatInteger(bar.volume)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {visibleRows.length === 0 && <p className="p-8 text-center text-sm text-muted-foreground">No observations in this date range.</p>}
                  </div>
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="text-xs text-muted-foreground" aria-live="polite">Page {page + 1} of {pageCount}</p>
                    <div className="flex gap-2">
                      <Button size="sm" variant="secondary" disabled={page <= 0} onClick={() => setPage((value) => Math.max(0, value - 1))}>Previous</Button>
                      <Button size="sm" variant="secondary" disabled={page >= pageCount - 1} onClick={() => setPage((value) => Math.min(pageCount - 1, value + 1))}>Next</Button>
                    </div>
                  </div>
                </CardContent>
              </Card>

              <Card>
                <CardHeader><CardTitle>Source & provenance</CardTitle>
                  <CardAction><Badge variant="outline">Recorded at import</Badge></CardAction>
                </CardHeader>
                <CardContent className="grid gap-3 text-xs text-muted-foreground sm:grid-cols-2">
                  <p>Provider: <span className="text-foreground">{series.provider}</span></p>
                  <p>Frequency: <span className="text-foreground">{series.frequency}</span></p>
                  <p>Data kind: <span className="text-foreground">{series.data_kind}</span></p>
                  <p>Retrieved at: <span className="text-foreground">{series.retrieved_at ?? "Unavailable"}</span></p>
                  <p>Provider last refreshed: <span className="text-foreground">{series.provider_last_refreshed ?? "Unavailable"}</span></p>
                  <p>Stored sessions: <span className="text-foreground">{formatInteger(currentInstrument?.sessions ?? bars.length)}</span></p>
                  <p className="sm:col-span-2 break-all">Source URL: <span className="text-foreground">{series.source_url ?? "Unavailable"}</span></p>
                </CardContent>
              </Card>
            </>
          )}

          {imports.length > 0 && (
            <Card>
              <CardHeader><CardTitle>Recent local imports</CardTitle><CardAction><Badge variant="outline">Last {imports.length}</Badge></CardAction></CardHeader>
              <CardContent>
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[590px] text-left text-xs">
                    <thead><tr className="text-muted-foreground">
                      <th className="pb-3 font-medium">Symbol</th>
                      <th className="pb-3 font-medium">Imported at (UTC)</th>
                      <th className="pb-3 text-right font-medium">Inserted</th>
                      <th className="pb-3 text-right font-medium">Updated</th>
                      <th className="pb-3 text-right font-medium">Unchanged</th>
                    </tr></thead>
                    <tbody>{imports.map((entry) => (
                      <tr key={entry.id} className="border-t border-border">
                        <td className="py-2.5 font-semibold">{entry.symbol}</td>
                        <td className="py-2.5">{entry.imported_at}</td>
                        <td className="py-2.5 text-right">{entry.inserted}</td>
                        <td className="py-2.5 text-right">{entry.updated}</td>
                        <td className="py-2.5 text-right">{entry.unchanged}</td>
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
              </CardContent>
            </Card>
          )}
        </>
      )}
      <footer className="text-xs text-muted-foreground">Local research workstation · Displayed values come from your SQLite database only · Not a live market feed</footer>
    </section>
  );
}

function Metric({ label, value, note }: { label: string; value: string; note: string }) {
  return <Card className="gap-2 py-4">
    <CardContent className="space-y-2">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <p className="break-words text-2xl font-semibold tabular-nums">{value}</p>
      <p className="text-[11px] leading-5 text-muted-foreground">{note}</p>
    </CardContent>
  </Card>;
}
