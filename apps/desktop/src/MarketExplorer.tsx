import { useEffect, useMemo, useState, type FormEvent } from "react";
import { AlertCircle, BarChart3, Database, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { fetchMarketDaily, fetchMarketInstruments } from "./market-api";
import type { MarketSelection } from "./DataAcquisitionPage";
import { MarketCharts } from "./market-chart";
import { yearsAgo, type HistoryPeriod } from "./history-range";
import { sortableBars, type DailySeries, type MarketInstrument } from "./market";

type LoadState = "loading" | "ready" | "error";
type SortColumn = "session_date" | "close" | "volume";

const pageSize = 25;
const formatInteger = (value: number) => new Intl.NumberFormat("en-US").format(value);
const price = (raw: string) => new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 }).format(Number(raw));

export function MarketExplorer({ initialSelection, onOpenAcquisition }: {
  initialSelection?: MarketSelection | null;
  onOpenAcquisition: () => void;
}) {
  const [instruments, setInstruments] = useState<MarketInstrument[]>([]);
  const [indexState, setIndexState] = useState<LoadState>("loading");
  const [indexError, setIndexError] = useState("");
  const [symbol, setSymbol] = useState(initialSelection?.symbol ?? "");
  const [priceProvider, setPriceProvider] = useState<"alpha_vantage" | "alpaca_sip" | "tiingo">(initialSelection?.provider ?? "alpha_vantage");
  const [draftStart, setDraftStart] = useState("");
  const [draftEnd, setDraftEnd] = useState("");
  const [range, setRange] = useState({ start: "", end: "" });
  const [period, setPeriod] = useState<HistoryPeriod>("recent");
  const [validationError, setValidationError] = useState("");
  const [series, setSeries] = useState<DailySeries | null>(null);
  const [seriesState, setSeriesState] = useState<LoadState>("loading");
  const [seriesError, setSeriesError] = useState("");
  const [sortColumn, setSortColumn] = useState<SortColumn>("session_date");
  const [descending, setDescending] = useState(true);
  const [page, setPage] = useState(0);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let ignore = false;
    setIndexState("loading");
    fetchMarketInstruments()
      .then((catalog) => {
        if (ignore) return;
        setInstruments(catalog);
        const matched = catalog.find((item) => item.symbol === symbol && item.provider === priceProvider)
          ?? catalog.find((item) => item.symbol === symbol)
          ?? catalog[0];
        setSymbol(matched?.symbol ?? "");
        setPriceProvider((matched?.provider ?? "alpha_vantage") as "alpha_vantage" | "alpaca_sip" | "tiingo");
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
      provider: priceProvider,
      startDate: range.start || undefined,
      endDate: range.end || undefined,
      limit: period === "recent" ? 100 : 4000,
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
  }, [symbol, priceProvider, range, reload, indexState, period]);

  const bars = series?.observations ?? [];
  const sorted = useMemo(() => sortableBars(bars, sortColumn, descending), [bars, sortColumn, descending]);
  const pageCount = Math.max(1, Math.ceil(sorted.length / pageSize));
  const visibleRows = sorted.slice(page * pageSize, (page + 1) * pageSize);
  const currentInstrument = instruments.find((item) => item.symbol === symbol && item.provider === priceProvider);
  const last = bars[bars.length - 1];
  const first = bars[0];
  const prior = bars.length > 1 ? bars[bars.length - 2] : null;
  const closeChange = last && prior ? Number(last.close) - Number(prior.close) : null;

  const selectPeriod = (selected: HistoryPeriod) => {
    setPeriod(selected);
    setValidationError("");
    if (selected === "custom") return;
    const start = selected === "recent" ? "" : yearsAgo(
      selected === "1y" ? 1 : selected === "5y" ? 5 : 10,
    );
    setDraftStart(start);
    setDraftEnd("");
    setRange({ start, end: "" });
  };

  const applyRange = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (draftStart && draftEnd && draftStart > draftEnd) {
      setValidationError("Start date must not exceed end date.");
      return;
    }
    setValidationError("");
    setPeriod("custom");
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
    <section className="space-y-2" aria-labelledby="markets-title">
      <header className="flex flex-wrap items-start justify-between gap-4 border-b border-border pb-5">
        <div>
          <p className="text-[10px] font-bold tracking-[0.16em] text-[#8aa5c3] uppercase">FinIntel / Markets</p>
          <h1 id="markets-title" className="mt-2 text-2xl font-semibold tracking-tight">Market Data Explorer</h1>
          <p className="mt-2 text-sm text-muted-foreground">Historical daily prices stored in local SQLite. No live quotes.</p>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="outline">Local SQLite</Badge>
          <Button type="button" variant="secondary" size="sm" onClick={() => setReload((v) => v + 1)}>
            <RefreshCw aria-hidden="true" /> Reload local data
          </Button>
          <Button type="button" size="sm" onClick={onOpenAcquisition}>
            <Database aria-hidden="true" /> Data Acquisition
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
            <CardDescription>FinIntel reads your local SQLite database. Open Data Acquisition to fetch and store your first stock or ETF.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex items-center gap-2 text-muted-foreground"><Database aria-hidden="true" className="size-5" /><span className="text-sm">No price series have been imported.</span></div>
            <Button type="button" onClick={onOpenAcquisition}><Database aria-hidden="true" /> Open Data Acquisition</Button>
            <code className="block overflow-x-auto rounded-md bg-muted px-3 py-3 text-xs">python -m finintel.storage import data/raw/AAPL.json</code>
            <p className="text-xs text-muted-foreground">Alternatively, import a previously downloaded snapshot using the command above. All new data acquisition and completed import history are managed in Data Acquisition.</p>
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
                  <select
                    value={symbol + "|" + priceProvider}
                    onChange={(event) => {
                      const [ticker, selected] = event.target.value.split("|");
                      setSymbol(ticker);
                      setPriceProvider(selected as "alpha_vantage" | "alpaca_sip" | "tiingo");
                    }}
                    aria-label="Stored instrument and historical data source"
                    className="h-10 rounded-md border border-input bg-muted px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {instruments.map((item) => (
                      <option key={item.symbol + item.provider} value={item.symbol + "|" + item.provider}>
                        {item.symbol} · {item.provider === "tiingo" ? "Tiingo" : item.provider === "alpaca_sip" ? "Alpaca SIP" : "Alpha Vantage"} · {formatInteger(item.sessions)} sessions
                      </option>
                    ))}
                  </select>
                </label>
                <label className="grid min-w-[150px] flex-1 gap-2 text-xs font-medium text-muted-foreground">
                  Display period
                  <select
                    aria-label="Historical time range"
                    value={period}
                    onChange={(event) => selectPeriod(event.target.value as HistoryPeriod)}
                    className="h-10 rounded-md border border-input bg-muted px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <option value="recent">Latest 100 sessions</option>
                    <option value="1y">Last 1 year</option>
                    <option value="5y">Last 5 years</option>
                    <option value="10y">Last 10 years</option>
                    <option value="custom">Custom dates</option>
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
                  setDraftStart(""); setDraftEnd(""); setRange({ start: "", end: "" }); setValidationError(""); setPeriod("recent");
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
              <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
                <Metric label="Instrument" value={series.symbol} note={currentInstrument ? formatInteger(currentInstrument.sessions) + " stored sessions · " + priceProvider : series.provider} />
                <Metric label="Last close" value={last ? price(last.close) : "—"} note={last?.session_date ?? "No trading sessions"} />
                <Metric label="Daily change" value={closeChange === null ? "—" : (closeChange >= 0 ? "+" : "") + price(String(closeChange))} note="Compared with prior loaded session" />
                <Metric label="Loaded observations" value={formatInteger(bars.length)} note={series.has_more ? "Bounded result · more stored" : "Available stored data"} />
              </div>

              {period !== "recent" && bars.length > 0 && bars.length < (period === "10y" ? 1200 : period === "5y" ? 600 : 120) && !series.has_more && (
                <p role="status" className="text-xs text-muted-foreground">
                  Only {formatInteger(bars.length)} sessions are stored for this selection. More history may require a premium full-history import, or the instrument may have a shorter trading history. This selector never downloads data automatically.
                </p>
              )}
              {series.has_more && (
                <p role="status" className="text-xs text-muted-foreground">
                  Display is limited to 4,000 latest records. Use date filters to examine older stored sessions.
                </p>
              )}

              <Card>
                <CardHeader>
                  <CardTitle><span className="flex items-center gap-2"><BarChart3 aria-hidden="true" className="size-5" /> OHLCV history</span></CardTitle>
                  <CardAction><Badge variant="outline">{series.price_adjustment.replaceAll("_", " ")}</Badge></CardAction>
                  <CardDescription className="col-span-2 mt-2">{first && last ? first.session_date + " – " + last.session_date : "No sessions in selected range"} · historical end-of-day data</CardDescription>
                </CardHeader>
                <CardContent><MarketCharts bars={bars} period={period} /></CardContent>
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

        </>
      )}
      <footer className="text-xs text-muted-foreground">Local research workstation · Charts display local SQLite data only · Fetches and action history live in Data Acquisition · Not a live market feed</footer>
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
