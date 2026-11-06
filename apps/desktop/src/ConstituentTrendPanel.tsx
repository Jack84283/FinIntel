import { useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { MarketDatePicker } from "./components/MarketDatePicker";
import { INDEX_CODES, type IndexCode } from "./index-api";
import { membershipIndexForMarket } from "./ConstituentOverlapPanel";
import {
  fetchMembershipTrend, type TrendEvidence, type TrendPoint, type TrendQuery, type TrendResult,
} from "./membership-trend-api";
import { useLocale } from "./localization";
import { TablePager, TABLE_PAGE_SIZE } from "./components/TablePagination";

const PAGE_SIZE = TABLE_PAGE_SIZE;
const selectClass = "h-9 w-full min-w-0 rounded-md border border-input bg-background px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const evidenceDescriptions: Record<TrendEvidence, string> = {
  verified_snapshot: "Verified dated membership snapshot",
  source_date_mismatch: "Underlying source date does not match snapshot date",
  proxy_only: "ETF holdings proxy — indicative constituents, not official",
  incomplete: "Incomplete constituent snapshot — overlap may be understated",
  unverified: "Unverified source snapshots — overlap is indicative",
};

export function trendEvidenceDescription(status: TrendEvidence): string {
  return evidenceDescriptions[status];
}
/** Draws one isolated bar per actual snapshot, never an interpolated daily line. */
export function SnapshotCountBar({ point, maximum }: { point: TrendPoint; maximum: number }) {
  const { t } = useLocale();
  const width = Math.max(1, Math.round(point.member_count / Math.max(1, maximum) * 100));
  return (
    <div className="flex min-w-[110px] items-center gap-2">
      <div className="h-2 w-full rounded bg-muted" role="img"
        aria-label={t("Stored snapshot constituent count") + ": " + point.member_count}>
        <div className="h-2 rounded bg-primary" style={{ width: width + "%" }} />
      </div>
      <span className="sr-only">{point.member_count}</span>
    </div>
  );
}

/** Source-backed snapshots; missing dates are deliberately not plotted or filled. */
export function ConstituentTrendPanel({
  primaryIndex, analysisStart, analysisEnd,
}: {
  primaryIndex: string; analysisStart: string; analysisEnd: string;
}) {
  const { t, language } = useLocale();
  const [expanded, setExpanded] = useState(false);
  const [code, setCode] = useState<IndexCode | null>(() => membershipIndexForMarket(primaryIndex));
  const [overrideStart, setOverrideStart] = useState("");
  const [overrideEnd, setOverrideEnd] = useState("");
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [evidenceFilter, setEvidenceFilter] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<TrendResult | null>(null);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const start = overrideStart || analysisStart;
  const end = overrideEnd || analysisEnd;
  const invalidRange = !start || !end || start > end;

  useEffect(() => {
    setCode(membershipIndexForMarket(primaryIndex));
    setOffset(0);
  }, [primaryIndex]);
  useEffect(() => {
    setOffset(0);
  }, [start, end, code]);
  useEffect(() => {
    const id = ++generation.current;
    setResult(null);
    setError("");
    if (!expanded || !code || invalidRange) { setBusy(false); return; }
    const query: TrendQuery = {
      code, fromAsOf: start, toAsOf: end, limit: PAGE_SIZE, offset,
      evidenceStatus: evidenceFilter ? evidenceFilter as TrendEvidence : undefined,
    };
    setBusy(true);
    fetchMembershipTrend(query).then(value => {
      if (id === generation.current) setResult(value);
    }).catch(reason => {
      if (id === generation.current) setError(reason instanceof Error ? reason.message : String(reason));
    }).finally(() => {
      if (id === generation.current) setBusy(false);
    });
    return () => { generation.current++; };
  }, [expanded, code, start, end, invalidRange, offset, evidenceFilter, revision]);

  const snapshots = result?.snapshots ?? [];
  // Backend filters before LIMIT/OFFSET; total_snapshots is the real matched count.
  const maximum = Math.max(1, ...snapshots.map(p => p.member_count));
  const total = result?.total_snapshots ?? 0;
  const count = (value: number | null) =>
    value === null ? "—" : value.toLocaleString(language);

  return (
    <Card>
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
        <div>
          <CardTitle>{t("Historical constituent snapshot trends")}</CardTitle>
          <CardDescription>{t("Only imported dated snapshots are displayed; no daily membership is inferred.")}</CardDescription>
          {membershipIndexForMarket(primaryIndex) === null && <p role="status" className="mt-2 text-xs text-muted-foreground">
            {primaryIndex}: {t("Choose a supported constituent index explicitly; this price index has no sourced membership catalog.")}
          </p>}
        </div>
        <Button size="sm" variant="outline" type="button" aria-expanded={expanded}
          onClick={() => setExpanded(value => !value)}>
          {expanded ? t("Close snapshot trends") : t("Explore snapshot trends")}
        </Button>
      </CardHeader>
      {expanded && <CardContent className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-3">
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            {t("Constituent trend index")}
            <select className={selectClass} value={code ?? ""}
              aria-label={t("Constituent trend index")}
              onChange={event => setCode(event.target.value as IndexCode)}>
              <option value="" disabled>{t("Select constituent index")}</option>
              {INDEX_CODES.map(item => <option key={item} value={item}>{item}</option>)}
            </select>
          </label>
          <MarketDatePicker label={t("Snapshot trend start")} value={start}
            onChange={value => setOverrideStart(value)} />
          <MarketDatePicker label={t("Snapshot trend end")} value={end}
            onChange={value => setOverrideEnd(value)} />
        </div>
        {(overrideStart || overrideEnd) && <Button size="sm" type="button" variant="ghost"
          onClick={() => { setOverrideStart(""); setOverrideEnd(""); }}>
          {t("Use analysis date range")}
        </Button>}
        <p className="text-xs text-muted-foreground">
          {t("Each bar is a real stored snapshot, not a continuous constituent history. Additions and removals compare adjacent stored snapshots only.")}
        </p>
        <Button size="sm" type="button" variant="ghost" onClick={() => setRevision(n => n + 1)}>
          {t("Refresh")}
        </Button>
        {!code && <p role="status" className="text-sm text-muted-foreground">
          {t("Select constituent index")}
        </p>}
        {invalidRange && <p role="status" className="text-sm text-muted-foreground">
          {t("Select a valid snapshot start and end date.")}
        </p>}
        {busy && <p role="status" aria-live="polite" className="text-sm text-muted-foreground">
          {t("Loading local snapshot trends…")}
        </p>}
        {!busy && error && <p role="alert" className="text-sm text-destructive">
          {t("Local constituent trends unavailable")}: {error}
        </p>}
        {!busy && !error && result && <>
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <Badge variant="outline">{t(evidenceFilter ? "Matching source snapshots" : "Observed source snapshots")}: {count(total)}</Badge>
            <Badge variant="outline">{t("Snapshot-only; no interpolation")}</Badge>
          </div>
          {total === 0 ? <p role="status" className="text-sm text-muted-foreground">
            {t("No imported membership snapshots in this period; unknown is not zero membership.")}
          </p> : <>
            {total < 2 && <p role="status" className="text-xs text-muted-foreground">
              {t("At least two stored snapshots are needed to observe changes.")}
            </p>}
            <label className="flex max-w-xs flex-col gap-1 text-xs text-muted-foreground">
              {t("Filter by evidence")}
              <select className={selectClass} value={evidenceFilter}
                onChange={event => { setEvidenceFilter(event.target.value); setOffset(0); }}>
                <option value="">{t("All evidence statuses")}</option>
                {(Object.keys(evidenceDescriptions) as TrendEvidence[]).map(status =>
                  <option key={status} value={status}>{t(trendEvidenceDescription(status))}</option>)}
              </select>
            </label>
            <div className="max-h-[430px] overflow-auto rounded-md border border-border">
              <table className="w-full border-collapse text-left text-xs tabular-nums">
                <caption className="px-3 py-2 text-left text-muted-foreground">
                  {t("Dated membership evidence and observed changes")}
                </caption>
                <thead className="sticky top-0 bg-muted text-muted-foreground">
                  <tr>
                    <th scope="col" className="p-2">{t("Source snapshot date")}</th>
                    <th scope="col" className="p-2">{t("Stored snapshot constituent count")}</th>
                    <th scope="col" className="p-2 text-right">{t("Added")}</th>
                    <th scope="col" className="p-2 text-right">{t("Removed")}</th>
                    <th scope="col" className="p-2">{t("Source classification")}</th>
                    <th scope="col" className="p-2">{t("Snapshot evidence")}</th>
                  </tr>
                </thead>
                <tbody>
                  {snapshots.map(point => <tr key={point.snapshot_as_of} className="border-t border-border align-top">
                    <th scope="row" className="p-2 font-medium">{point.snapshot_as_of}</th>
                    <td className="p-2">
                      <span>{count(point.member_count)}</span>
                      <SnapshotCountBar point={point} maximum={maximum} />
                    </td>
                    <td className="p-2 text-right">{count(point.added_members)}</td>
                    <td className="p-2 text-right">{count(point.removed_members)}</td>
                    <td className="p-2">
                      <div>{t(point.source_kind)}</div>
                      <div className="text-muted-foreground">{t("Source")}: {point.source}</div>
                      <div className="text-muted-foreground">{t("Underlying source as-of date")}: {point.source_as_of ?? "—"}</div>
                      <div className="text-muted-foreground">{t("Source completeness")}: {t(point.validation_status)}</div>
                      <div className="text-muted-foreground">{t("Expected count")}: {count(point.expected_count)}</div>
                    </td>
                    <td className="p-2">
                      <Badge variant={point.evidence_status === "verified_snapshot" ? "success" : "outline"}>
                        {t(trendEvidenceDescription(point.evidence_status))}
                      </Badge>
                      <p className="mt-1 text-muted-foreground">
                        {point.previous_snapshot_as_of === null
                          ? t("First observed snapshot in selected range")
                          : t(point.change_status === "verified_pair"
                            ? "Changes between two verified source snapshots"
                            : "Indicative changes; source confidence or completeness differs")}
                      </p>
                      {point.previous_snapshot_as_of && <p className="text-muted-foreground">
                        {t("Compared with stored snapshot")}: {point.previous_snapshot_as_of}
                      </p>}
                    </td>
                  </tr>)}
                </tbody>
              </table>
            </div>
            <TablePager page={Math.floor(offset / PAGE_SIZE)} total={total}
              onPageChange={next => setOffset(next * PAGE_SIZE)} disabled={busy} />
          </>}
          <p className="text-xs text-muted-foreground">
            {t("Observed snapshot changes do not establish when membership changed between imports, and indicative sources must not be used as verified backtests.")}
          </p>
        </>}
      </CardContent>}
    </Card>
  );
}
