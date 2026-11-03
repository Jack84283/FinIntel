import { useEffect, useRef, useState, type FormEvent } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { MarketDatePicker } from "./components/MarketDatePicker";
import { INDEX_CODES, type IndexCode } from "./index-api";
import {
  fetchMembershipOverlap, type OverlapQuery, type OverlapResult, type OverlapSource, type OverlapStatus,
} from "./membership-overlap-api";
import { useLocale } from "./localization";

const PAGE_SIZE = 50;
const selectClass = "h-9 min-w-0 rounded-md border border-input bg-background px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const statuses: Record<OverlapStatus, string> = {
  verified_snapshot: "Both source snapshots verified for the exact selected date",
  earlier_snapshot: "Earlier snapshots — membership on the requested date is not confirmed",
  proxy_only: "ETF holdings proxy — indicative constituents, not official",
  incomplete: "Incomplete constituent snapshot — overlap may be understated",
  unverified: "Unverified source snapshots — overlap is indicative",
  unavailable: "Source snapshot unavailable — overlap is unknown, not zero",
};
/** Unknown price indexes have no sourced constituent catalog; never silently map them to SP500. */
export function membershipIndexForMarket(code: string): IndexCode | null {
  return INDEX_CODES.includes(code as IndexCode) ? code as IndexCode : null;
}
export function overlapStatusDescription(status: OverlapStatus): string {
  return statuses[status];
}

/** Show both the imported snapshot date and the underlying evidence's effective date. */
export function OverlapSourceDates({ source }: { source: OverlapSource }) {
  const { t } = useLocale();
  return <>
    <p>{t("Source snapshot date")}: {source.snapshot_as_of ?? t("Unavailable")}</p>
    <p>{t("Underlying source as-of date")}: {source.source_as_of ?? t("Unavailable")}</p>
  </>;
}

/** Independent point-in-time membership evidence, never inferred from price history. */
export function ConstituentOverlapPanel({
  primaryIndex,
  analysisEnd,
}: {
  primaryIndex: string;
  analysisEnd: string;
}) {
  const { t, language } = useLocale();
  const initialIndex = membershipIndexForMarket(primaryIndex);
  const [expanded, setExpanded] = useState(false);
  const [left, setLeft] = useState<IndexCode | null>(initialIndex);
  const [right, setRight] = useState<IndexCode>(initialIndex === "NDX" ? "SP500" : "NDX");
  const [overrideDate, setOverrideDate] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [result, setResult] = useState<OverlapResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const asOf = overrideDate || analysisEnd || new Date().toISOString().slice(0, 10);
  const unsupportedAnalysisIndex = membershipIndexForMarket(primaryIndex) === null;

  useEffect(() => {
    const next = membershipIndexForMarket(primaryIndex);
    setLeft(next);
    if (next) setRight(old => old === next ? (next === "SP500" ? "NDX" : "SP500") : old);
    setOffset(0);
  }, [primaryIndex]);

  useEffect(() => {
    const id = ++generation.current;
    setResult(null);
    setError("");
    if (!expanded || !left) { setBusy(false); return; }
    const query: OverlapQuery = { left, right, asOf, q: search, limit: PAGE_SIZE, offset };
    setBusy(true);
    fetchMembershipOverlap(query).then(value => {
      if (id === generation.current) {
        setResult(value);
        setError("");
      }
    }).catch(reason => {
      if (id === generation.current) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    }).finally(() => {
      if (id === generation.current) setBusy(false);
    });
    return () => { generation.current++; };
  }, [expanded, left, right, asOf, search, offset, revision]);

  function changeLeft(next: IndexCode) {
    setLeft(next);
    if (next === right) setRight(left && left !== next ? left : next === "SP500" ? "NDX" : "SP500");
    setOffset(0);
  }
  function changeRight(next: IndexCode) {
    setRight(next);
    if (next === left) setLeft(right);
    setOffset(0);
  }
  function submitSearch(event: FormEvent) {
    event.preventDefault();
    setSearch(searchInput.trim());
    setOffset(0);
  }
  function selectedDate(value: string) {
    setOverrideDate(value);
    setOffset(0);
  }
  const details = result?.sources ?? [];
  const found = result?.filtered_total ?? 0;
  const showCount = result?.status !== "unavailable" && result !== null;
  const count = (n: number | null | undefined) => n === null || n === undefined
    ? "—" : n.toLocaleString(language);
  return (
    <Card>
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
        <div>
          <CardTitle>{t("Dated constituent overlap")}</CardTitle>
          <CardDescription>
            {t("Inspect only sourced membership snapshots; price history does not establish constituents.")}
          </CardDescription>
          {unsupportedAnalysisIndex && <p role="status" className="mt-2 text-xs text-muted-foreground">
            {primaryIndex}: {t("No sourced constituent catalog for this analysis index. Choose constituent indexes explicitly; overlap does not represent the selected price index.")}
          </p>}
        </div>
        <Button type="button" variant="outline" size="sm" aria-expanded={expanded}
          onClick={() => setExpanded(value => !value)}>
          {expanded ? t("Close constituent overlap") : t("Explore constituent overlap")}
        </Button>
      </CardHeader>
      {expanded && <CardContent className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            {t("First constituent index")}
            <select className={selectClass} aria-label={t("First constituent index")}
              value={left ?? ""} onChange={event => changeLeft(event.target.value as IndexCode)}>
              <option value="" disabled>{t("Select constituent index")}</option>
              {INDEX_CODES.map(code => <option value={code} key={code}>{code}</option>)}
            </select>
          </label>
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            {t("Second constituent index")}
            <select className={selectClass} aria-label={t("Second constituent index")}
              value={right} onChange={event => changeRight(event.target.value as IndexCode)}>
              {INDEX_CODES.map(code => <option value={code} key={code}>{code}</option>)}
            </select>
          </label>
          <div className="max-w-[340px]">
            <MarketDatePicker label={t("Constituent snapshot date")} value={asOf}
              onChange={selectedDate} />
          </div>
        </div>
        {overrideDate && <Button size="sm" variant="ghost" type="button"
          onClick={() => selectedDate("")}>{t("Use analysis end date")}</Button>}
        <p className="text-xs text-muted-foreground">
          {t("Requested date")}: {asOf}.{" "}
          {t("The latest available source snapshot on or before this date is compared. Older snapshots do not prove membership remained unchanged.")}
        </p>
        <form onSubmit={submitSearch} className="flex flex-wrap items-end gap-2">
          <label className="grid min-w-0 flex-1 gap-1 text-xs text-muted-foreground">
            {t("Filter overlap symbols or names")}
            <input className={selectClass + " w-full"} type="search" maxLength={80}
              value={searchInput} onChange={event => setSearchInput(event.target.value)}
              placeholder={t("Symbol or company name")} />
          </label>
          <Button size="sm" variant="outline" type="submit">{t("Search")}</Button>
          <Button size="sm" variant="ghost" type="button" onClick={() => setRevision(n => n + 1)}>
            {t("Refresh")}
          </Button>
        </form>
        {!left && <p role="status" className="text-sm text-muted-foreground">
          {t("Select a constituent index to begin the overlap comparison.")}
        </p>}
        {busy && <p role="status" aria-live="polite" className="text-sm text-muted-foreground">
          {t("Checking local membership snapshots…")}
        </p>}
        {!busy && error && <p role="alert" className="text-sm text-destructive">
          {t("Local constituent overlap unavailable")}: {error}
        </p>}
        {!busy && !error && result && <>
          <div className="flex flex-wrap gap-2">
            <Badge variant={result.status === "verified_snapshot" ? "success" : "outline"}>
              {t(overlapStatusDescription(result.status))}
            </Badge>
            <Badge variant="outline">{left} / {right}</Badge>
          </div>
          <div className="grid gap-2 sm:grid-cols-3">
            <div className="rounded-md border border-border p-3">
              <p className="text-xs text-muted-foreground">{t("Observed common members")}</p>
              <p className="text-xl font-semibold tabular-nums">{count(result.common_members)}</p>
            </div>
            <div className="rounded-md border border-border p-3">
              <p className="text-xs text-muted-foreground">{t("Observed union members")}</p>
              <p className="text-xl font-semibold tabular-nums">{count(result.union_members)}</p>
            </div>
            <div className="rounded-md border border-border p-3">
              <p className="text-xs text-muted-foreground">{t("Snapshot Jaccard overlap")}</p>
              <p className="text-xl font-semibold tabular-nums">
                {result.jaccard_ratio === null ? "—" :
                  new Intl.NumberFormat(language, { style: "percent", maximumFractionDigits: 1 })
                    .format(Number(result.jaccard_ratio))}
              </p>
            </div>
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            {details.map(source => <div key={source.code}
              className="min-w-0 space-y-1 rounded-md border border-border p-3 text-xs text-muted-foreground">
              <strong className="text-sm text-foreground">{source.code} · {source.name}</strong>
              <OverlapSourceDates source={source} />
              <p>{t("Source")}: {source.source ?? t("Unavailable")}</p>
              <p>{t("Source classification")}: {t(source.source_kind ?? "unknown")}</p>
              <p>{t("Source completeness")}: {t(source.validation_status ?? "unknown")}</p>
              <p>{t("Source availability")}: {t(source.availability_status)}</p>
              <p>{t("Snapshot members")}: {count(source.member_count)}
                {source.expected_count !== null ? " / " + count(source.expected_count) : ""}</p>
            </div>)}
          </div>
          {showCount && <>
            <div className="max-h-[360px] overflow-auto rounded-md border border-border">
              <table className="w-full border-collapse text-xs">
                <caption className="px-3 py-2 text-left text-muted-foreground">
                  {t("Shared symbols in both dated source snapshots")}
                </caption>
                <thead className="sticky top-0 bg-muted text-muted-foreground">
                  <tr>
                    <th className="p-2 text-left" scope="col">{t("Symbol")}</th>
                    <th className="p-2 text-left" scope="col">{t("Company")}</th>
                    <th className="p-2 text-left" scope="col">{t("Type")}</th>
                  </tr>
                </thead>
                <tbody>
                  {result.items.map(item => <tr key={item.symbol} className="border-t border-border">
                    <th className="p-2 text-left font-medium" scope="row">{item.symbol}</th>
                    <td className="p-2">{item.name}</td><td className="p-2">{item.asset_type}</td>
                  </tr>)}
                </tbody>
              </table>
            </div>
            {found === 0 && <p role="status" className="text-sm text-muted-foreground">
              {search ? t("No shared members match this filter.") : t("No shared symbols in the compared snapshots.")}
            </p>}
            <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
              <span>{t("Matching shared symbols")}: {count(result.filtered_total)}</span>
              <div className="flex items-center gap-2">
                <Button type="button" variant="outline" size="sm" disabled={offset === 0}
                  onClick={() => setOffset(n => Math.max(0, n - PAGE_SIZE))}>{t("Previous")}</Button>
                <span className="tabular-nums">{found ? Math.min(offset + 1, found) : 0}–{Math.min(offset + result.items.length, found)}</span>
                <Button type="button" variant="outline" size="sm"
                  disabled={offset + PAGE_SIZE >= found || offset + PAGE_SIZE > 1_000_000}
                  onClick={() => setOffset(n => n + PAGE_SIZE)}>{t("Next")}</Button>
              </div>
            </div>
          </>}
          {result.status !== "verified_snapshot" && <p className="text-xs text-muted-foreground">
            {t("This comparison is indicative or unavailable, not a verified point-in-time constituent set for backtesting.")}
          </p>}
        </>}
      </CardContent>}
    </Card>
  );
}
