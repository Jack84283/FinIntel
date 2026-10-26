import { PageHeader } from "./PageHeader";
import { useLocale } from "./localization";
import { MarketDatePicker } from "./components/MarketDatePicker";
import { searchSymbols, type CatalogSymbol } from "./symbol-catalog-api";
import { useEffect, useState, type FormEvent } from "react";
import { Download, RefreshCw, Search } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  acquireIndex, fetchConstituents, fetchIndexes, fetchStockIndexes, INDEX_CODES,
  type ConstituentPage, type IndexCode, type IndexRecord, type SymbolIndexes,
} from "./index-api";

const FETCH_SIZE = 100;

/** Dated evidence-backed equity index membership; does not infer from ETF holdings. */
export function IndexMembershipPage() {
  const { t } = useLocale();
  const [selected, setSelected] = useState<IndexCode>("SP500");
  const [asOf, setAsOf] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [revision, setRevision] = useState(0);
  const [catalog, setCatalog] = useState<IndexRecord[]>([]);
  const [snapshot, setSnapshot] = useState<ConstituentPage | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState("");
  const [stock, setStock] = useState("");
  const [stockResult, setStockResult] = useState<SymbolIndexes | null>(null);
  const [stockError, setStockError] = useState("");
  const [stockBusy, setStockBusy] = useState(false);
  const [acquiring, setAcquiring] = useState(false);
  const [acquireMessage, setAcquireMessage] = useState("");
  const [acquireError, setAcquireError] = useState("");
  const [stockOptions, setStockOptions] = useState<CatalogSymbol[]>([]);
  const [stockFocused, setStockFocused] = useState(false);
  const [stockSearching, setStockSearching] = useState(false);

  useEffect(() => {
    let active = true;
    fetchIndexes(asOf).then(result => { if (active) setCatalog(result); })
      .catch(() => { if (active) setCatalog([]); });
    return () => { active = false; };
  }, [asOf, revision]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setSearch(searchInput.trim());
    }, 250);
    return () => window.clearTimeout(timer);
  }, [searchInput]);

  useEffect(() => {
    let active = true;
    setState("loading");
    setSnapshot(null);
    const loadAll = async () => {
      try {
        const first = await fetchConstituents(selected, asOf, search, 0, FETCH_SIZE);
        if (!active) return;
        const items = [...first.items];
        for (let offset = items.length; offset < first.total; offset += FETCH_SIZE) {
          if (!active) return;
          const next = await fetchConstituents(selected, asOf, search, offset, FETCH_SIZE);
          if (!active) return;
          if (next.snapshot_as_of !== first.snapshot_as_of || next.total !== first.total) {
            throw new Error("Index snapshot changed during loading. Refresh to try again.");
          }
          items.push(...next.items);
          if (next.items.length === 0) {
            throw new Error("Index source returned an incomplete constituent list.");
          }
        }
        if (!active) return;
        setSnapshot({ ...first, items });
        setError("");
        setState("ready");
      } catch (reason) {
        if (!active) return;
        setError(reason instanceof Error ? reason.message : String(reason));
        setState("error");
      }
    };
    void loadAll();
    return () => { active = false; };
  }, [selected, asOf, search, revision]);

  useEffect(() => {
    setStockResult(null);
    setStockError("");
  }, [asOf]);

  useEffect(() => {
    let active = true;
    const query = stock.trim();
    if (!query) return;
    const timer = window.setTimeout(() => {
      searchSymbols(query, "Stock", 8)
        .then(result => { if (active) setStockOptions(result.results); })
        .catch(() => { if (active) setStockOptions([]); })
        .finally(() => { if (active) setStockSearching(false); });
    }, 180);
    return () => { active = false; window.clearTimeout(timer); };
  }, [stock]);

  const lookup = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const ticker = stock.trim().toUpperCase();
    const chosen = stockOptions.find(item => item.symbol === ticker) ??
      stockOptions.find(item => item.name.toLowerCase() === stock.trim().toLowerCase()) ??
      stockOptions.find(item => item.name.toLowerCase().startsWith(stock.trim().toLowerCase()));
    const symbol = chosen?.symbol ?? ticker;
    if (!/^[A-Z0-9][A-Z0-9.-]{0,14}$/.test(symbol)) {
      setStockError("Select a company from the suggestions or enter a valid ticker.");
      return;
    }
    setStock(symbol);
    setStockFocused(false);
    setStockBusy(true);
    setStockResult(null);
    setStockError("");
    try {
      setStockResult(await fetchStockIndexes(symbol, asOf));
    } catch (reason: unknown) {
      setStockError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setStockBusy(false);
    }
  };

  const download = async () => {
    setAcquiring(true);
    setAcquireError("");
    setAcquireMessage("");
    try {
      const result = await acquireIndex(selected);
      setAcquireMessage("Imported " + result.members.toLocaleString() +
        " of " + result.source_total.toLocaleString() + " fund-derived holdings as of " +
        result.snapshot_as_of + (result.complete ? "." :
        ". INCOMPLETE: " + result.unlinked_count.toLocaleString() +
        " holdings have no linked ticker and are excluded. Do not treat this as a full index."));
      setRevision(value => value + 1);
    } catch (reason) {
      setAcquireError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setAcquiring(false);
    }
  };

  const selectedMeta = catalog.find(item => item.code === selected);
  return (
    <section className="space-y-2" aria-labelledby="index-membership-title">
      <PageHeader
        id="index-membership-title"
        breadcrumb={t("FinIntel / Data & Operations")}
        title={t("Index Constituents")}
        subtitle={t("Discover which securities belonged to which indexes, using sourced local snapshots.")}
        actions={<Button type="button" variant="secondary" size="sm" onClick={() => setRevision(value => value + 1)}>
          <RefreshCw aria-hidden="true" /> Refresh
        </Button>}
      />
      <Card>
        <CardHeader className="grid-cols-1">
          <CardTitle>{t("Index membership snapshots")}</CardTitle>
          <CardDescription>
            Membership reflects each index’s latest stored snapshot on or before the selected date.
            No membership is inferred from stock prices or ETF holdings.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-end gap-3">
            <div className="w-full max-w-xs">
              <MarketDatePicker label={t("As-of date")} value={asOf} onChange={value => { setAsOf(value); }} />
            </div>
            <div className="flex min-h-12 flex-wrap items-center gap-3">
              <Button type="button" size="sm" variant="outline" onClick={() => setAsOf("")}>
                Latest available
              </Button>
              <span className="text-xs text-muted-foreground">{t("Blank date uses the latest local snapshot.")}</span>
            </div>
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs text-muted-foreground">{t("Equibles snapshots are ETF-holdings proxies, not official index constituents.")}</p>
            <Button type="button" size="sm" disabled={acquiring}
              onClick={() => { void download(); }}>
              <Download aria-hidden="true" />
              {acquiring ? "Fetching constituents…" : "Fetch constituents"}
            </Button>
          </div>
          {acquireError && <p role="alert" className="text-sm text-destructive">{acquireError}</p>}
          {acquireMessage && <p role="status" className="text-sm text-positive">{acquireMessage}</p>}
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4" role="group" aria-label={t("Select an index")}>
            {INDEX_CODES.map(code => {
              const item = catalog.find(entry => entry.code === code);
              return (
                <button
                  key={code}
                  type="button"
                  aria-pressed={selected === code}
                  onClick={() => { setSelected(code); }}
                  className={"rounded-md border p-3 text-left focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring " +
                    (selected === code ? "border-ring bg-secondary" : "border-border hover:bg-accent")}
                >
                  <p className="text-sm font-semibold">{item?.name ?? code}</p>
                  <p className="mt-2 text-xl font-semibold tabular-nums">
                    {item?.members == null ? "—" : item.members.toLocaleString()}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {item?.snapshot_as_of ? "Snapshot " + item.snapshot_as_of : "No imported snapshot"}
                  </p>
                </button>
              );
            })}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="grid-cols-1">
          <CardTitle>{t("Stock index lookup")}</CardTitle>
          <CardDescription>{t("Search by stock symbol or company name to check all four indexes as of the chosen date.")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <form onSubmit={event => { void lookup(event); }} className="flex flex-wrap items-center gap-2">
            <div className="relative w-full max-w-md">
              <input
                role="combobox"
                aria-label={t("Lookup stock symbol or company name")}
                aria-autocomplete="list"
                aria-controls="index-stock-suggestions"
                aria-expanded={stockFocused && stock.trim().length > 0}
                placeholder={t("Search symbol or company name, e.g. AAPL or Apple")}
                maxLength={80}
                value={stock}
                onChange={event => { setStock(event.target.value); setStockSearching(true); setStockOptions([]); setStockResult(null); }}
                onFocus={() => setStockFocused(true)}
                onBlur={() => setStockFocused(false)}
                onKeyDown={event => {
                  if (event.key === "Escape") setStockFocused(false);
                  if (event.key === "Enter" && stockOptions.length > 0 &&
                    !stockOptions.some(item => item.symbol === stock.trim().toUpperCase())) {
                    event.preventDefault();
                    setStock(stockOptions[0].symbol);
                    setStockFocused(false);
                  }
                }}
                className="h-10 w-full rounded-md border border-input bg-muted px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
              {stockFocused && stock.trim() && (
                <div id="index-stock-suggestions" role="listbox" aria-label={t("Matching stocks")}
                  className="absolute inset-x-0 top-full z-30 mt-1 max-h-64 overflow-y-auto rounded-md border border-border bg-card p-1 shadow-lg">
                  {stockSearching ? <p className="px-3 py-2 text-xs text-muted-foreground">{t("Searching local stocks…")}</p> :
                    stockOptions.length ? stockOptions.map(item => (
                      <button key={item.symbol} type="button" role="option" aria-selected={false}
                        onMouseDown={event => event.preventDefault()}
                        onClick={() => { setStock(item.symbol); setStockFocused(false); }}
                        className="flex w-full gap-3 rounded px-3 py-2 text-left text-sm hover:bg-accent">
                        <span className="w-20 shrink-0 font-semibold">{item.symbol}</span>
                        <span className="min-w-0 truncate text-muted-foreground">{item.name}</span>
                      </button>
                    )) : <p className="px-3 py-2 text-xs text-muted-foreground">{t("No local stock matches; exact ticker entry is supported.")}</p>}
                </div>
              )}
            </div>
            <Button size="sm" type="submit" disabled={stockBusy || !stock.trim()}>
              <Search aria-hidden="true" /> {stockBusy ? "Searching…" : "Check indexes"}
            </Button>
          </form>
          {stockError && <p role="alert" className="text-sm text-destructive">{stockError}</p>}
          {stockResult && (
            <div className="flex flex-wrap items-center gap-2 text-sm" role="status">
              <span className="font-semibold">{stockResult.symbol}</span>
              {stockResult.indexes.map(item => (
                <Badge key={item.code} variant={item.is_member === true ? "success" : "outline"}>
                  {item.name}: {item.is_member === null ? "Unknown" : item.is_member ? "Included" : "Not in snapshot"}
                </Badge>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="grid-cols-1">
          <CardTitle>{selectedMeta?.name ?? selected} — Constituents</CardTitle>
          <CardDescription>
            {snapshot?.snapshot_as_of
              ? "Effective snapshot: " + snapshot.snapshot_as_of + " · Source: " + (snapshot.source ?? "Unknown")
              : "No constituent snapshot has been imported for this date."}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <input
            aria-label={t("Filter index constituents")}
            placeholder={t("Search ticker or company name")}
            value={searchInput}
            maxLength={80}
            onChange={event => setSearchInput(event.target.value)}
            className="h-9 w-full max-w-md rounded-md border border-input bg-background px-3 text-sm"
          />
          {state === "loading" && <Skeleton aria-label={t("Loading index constituents")} className="h-28 w-full" />}
          {state === "error" && <p role="alert" className="text-sm text-destructive">{error}</p>}
          {state === "ready" && !snapshot?.snapshot_as_of && (
            <p className="py-6 text-sm text-muted-foreground">
              No imported constituent list is available for {selectedMeta?.name ?? selected} on or before this date.
              The index definitions alone do not provide stock memberships. Import a full, dated constituent CSV
              from an authorized source using the instructions in docs/INDEX_MEMBERSHIP.md, then select Refresh.
            </p>
          )}
          {state === "ready" && snapshot?.snapshot_as_of && snapshot.total === 0 && (
            <p className="py-6 text-sm text-muted-foreground">{t("No securities match this filter.")}</p>
          )}
          {state === "ready" && snapshot && snapshot.items.length > 0 && (
            <div className="overflow-x-auto rounded-md border border-border">
              <table className="w-full min-w-[650px] text-left text-xs">
                <thead className="bg-muted text-muted-foreground">
                  <tr><th scope="col" className="px-3 py-2">{t("Ticker")}</th>
                    <th scope="col" className="px-3 py-2">{t("Company")}</th>
                    <th scope="col" className="px-3 py-2">{t("Type")}</th>
                    <th scope="col" className="px-3 py-2">{t("Indexes")}</th></tr>
                </thead>
                <tbody>{snapshot.items.map(item => (
                  <tr key={item.symbol} className="border-t border-border">
                    <td className="px-3 py-2 font-semibold">{item.symbol}</td>
                    <td className="px-3 py-2">{item.name}</td>
                    <td className="px-3 py-2">{item.asset_type}</td>
                    <td className="px-3 py-2">
                      <div className="flex flex-wrap gap-1">
                        {item.member_indexes.map(code => <Badge variant="outline" key={code}>{code}</Badge>)}
                      </div>
                    </td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
          {state === "ready" && snapshot?.snapshot_as_of && (
            <p className="text-xs text-muted-foreground" role="status">
              Showing all {snapshot.total.toLocaleString()} matching securities
              {search ? " for the current filter" : ""}.
            </p>
          )}
          <p className="text-xs text-muted-foreground">
            Imported snapshots are not live. A missing membership source is unknown, not proof of exclusion.
            Historical accuracy depends on the source and the snapshot dates.
          </p>
        </CardContent>
      </Card>
    </section>
  );
}
