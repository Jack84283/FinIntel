import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Download, RefreshCw, Search, ShieldCheck } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "./PageHeader";
import { useLocale } from "./localization";
import { useNotify } from "./notifications";
import {
  createQualityCorrection, exportQualityCsv, exportQualityJson, fetchCorporateActions,
  fetchCorrectionPreview, fetchFindingHistory, fetchQualityCorrections, fetchQualityFindings,
  fetchQualityOverview, fetchReconciliation, redactedEvidence, runQualityScan,
  submitFindingReview, transitionQualityCorrection, verifyQualityCorporateActions,
  type CorporateActions, type Correction, type CorrectionAction, type CorrectionPreview,
  type FindingHistory, type FindingState, type QualityFinding, type QualityOverview,
  type QualityPage,
} from "./quality-api";

const indexCodes = ["SP500", "DJIA", "NASDAQCOM", "NDX", "VIX"] as const;
const pageSize = 20;
const selectClass = "h-9 max-w-full rounded-md border border-input bg-background px-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const inputClass = "h-9 min-w-0 rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

function localDownload(name: string, content: string, type: string): void {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function readableError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** A dedicated, source-first investigation screen. No raw market data is edited here. */
export function DataQualityPage() {
  const { t, language } = useLocale();
  const notify = useNotify();
  const [scope, setScope] = useState<"index" | "equity">("index");
  const [indexCode, setIndexCode] = useState<(typeof indexCodes)[number]>("SP500");
  const [symbolInput, setSymbolInput] = useState("AAPL");
  const [equityCode, setEquityCode] = useState("AAPL");
  const code = scope === "index" ? indexCode : equityCode;
  const [typeFilter, setTypeFilter] = useState("");
  const [providerFilter, setProviderFilter] = useState("");
  const [reviewFilter, setReviewFilter] = useState("");
  const [page, setPage] = useState(0);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [overview, setOverview] = useState<QualityOverview | null>(null);
  const [findings, setFindings] = useState<QualityPage<QualityFinding> | null>(null);
  const [corrections, setCorrections] = useState<QualityPage<Correction> | null>(null);
  const [corporate, setCorporate] = useState<CorporateActions | null>(null);
  const [providerCounts, setProviderCounts] = useState<{ provider: string; series_count: number }[]>([]);
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState("");
  const [selectedFinding, setSelectedFinding] = useState<number | null>(null);
  const [history, setHistory] = useState<FindingHistory | null>(null);
  const [historyError, setHistoryError] = useState("");
  const [reviewState, setReviewState] = useState<FindingState>("confirmed");
  const [reviewNote, setReviewNote] = useState("");
  const [proposedProvider, setProposedProvider] = useState("");
  const [proposedClose, setProposedClose] = useState("");
  const [correctionNote, setCorrectionNote] = useState("");
  const [selectedCorrection, setSelectedCorrection] = useState<string | null>(null);
  const [preview, setPreview] = useState<CorrectionPreview | null>(null);
  const [approvalChecked, setApprovalChecked] = useState(false);

  const reload = () => setRevision(value => value + 1);
  useEffect(() => { setPage(0); setSelectedFinding(null); setSelectedCorrection(null); }, [code, typeFilter, providerFilter, reviewFilter]);
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError("");
    const filters = {
      code, finding_type: typeFilter || undefined, provider: providerFilter || undefined,
      review_state: reviewFilter || undefined, limit: pageSize, offset: page * pageSize,
    };
    Promise.allSettled([
      fetchQualityOverview(code), fetchQualityFindings(filters),
      fetchQualityCorrections(code), fetchCorporateActions(code), fetchReconciliation(code),
    ]).then(results => {
      if (cancelled) return;
      const [summary, rows, proposals, actions, providers] = results;
      if (summary.status === "fulfilled") setOverview(summary.value);
      if (rows.status === "fulfilled") setFindings(rows.value);
      if (proposals.status === "fulfilled") setCorrections(proposals.value);
      if (actions.status === "fulfilled") setCorporate(actions.value);
      if (providers.status === "fulfilled") setProviderCounts(providers.value.provider_series);
      const errors = results.filter(result => result.status === "rejected") as PromiseRejectedResult[];
      if (errors.length) setLoadError(errors.map(error => readableError(error.reason)).join(" · "));
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [code, typeFilter, providerFilter, reviewFilter, page, revision]);

  useEffect(() => {
    let active = true;
    setHistory(null);
    setHistoryError("");
    if (selectedFinding === null) return;
    fetchFindingHistory(selectedFinding)
      .then(result => { if (active) { setHistory(result); setProposedProvider(result.finding.provider.split("|")[0]); } })
      .catch(reason => { if (active) setHistoryError(readableError(reason)); });
    return () => { active = false; };
  }, [selectedFinding, revision]);

  useEffect(() => {
    let active = true;
    setPreview(null);
    setApprovalChecked(false);
    if (!selectedCorrection) return;
    fetchCorrectionPreview(selectedCorrection)
      .then(result => { if (active) setPreview(result); })
      .catch(reason => { if (active) setHistoryError(readableError(reason)); });
    return () => { active = false; };
  }, [selectedCorrection, revision]);

  const notifyFailure = (error: unknown) => {
    const message = readableError(error);
    setLoadError(message);
    notify(t("Data Quality operation failed."), "error");
  };
  async function perform(label: string, task: () => Promise<unknown>, message: string) {
    setBusy(label);
    setLoadError("");
    try {
      await task();
      notify(t(message), "success");
      reload();
    } catch (error) {
      notifyFailure(error);
    } finally {
      setBusy("");
    }
  }
  const changeScope = (next: "index" | "equity") => {
    setScope(next);
    setTypeFilter(""); setProviderFilter(""); setReviewFilter(""); setPage(0);
  };
  const openFinding = (finding: QualityFinding) => {
    setSelectedFinding(value => value === finding.id ? null : finding.id);
    setReviewState("confirmed");
    setReviewNote("");
    setProposedClose("");
    setCorrectionNote("");
    setSelectedCorrection(null);
    setProposedProvider(finding.provider.split("|")[0]);
  };
  const selected = history?.finding;
  const canPropose = selected?.review_state === "confirmed" && !selected.superseded;
  const scopeDescription = scope === "index"
    ? t("Index levels are examined separately from equity OHLCV and total-return series.")
    : t("Inspect stored equity providers and their daily raw, unadjusted observations.");
  const pageCount = Math.max(1, Math.ceil((findings?.total ?? 0) / pageSize));

  async function exportReport(format: "json" | "csv") {
    if (!overview) return;
    setBusy("export");
    try {
      // Explicit upper bound protects desktop memory and oversized downloads.
      const collected: QualityFinding[] = [];
      let offset = 0;
      let total = 0;
      do {
        const result = await fetchQualityFindings({
          code, provider: providerFilter || undefined, finding_type: typeFilter || undefined,
          review_state: reviewFilter || undefined, offset, limit: 500,
        });
        total = result.total;
        collected.push(...result.items);
        offset += result.items.length;
        if (!result.items.length) break;
      } while (offset < total && collected.length < 5000);
      const base = `finintel-quality-${code.toLowerCase()}-${new Date().toISOString().slice(0, 10)}`;
      localDownload(
        base + "." + format,
        format === "json" ? exportQualityJson(code, overview, collected) : exportQualityCsv(collected),
        format === "json" ? "application/json" : "text/csv;charset=utf-8",
      );
      if (collected.length < total) notify(t("Export limited to first 5,000 filtered findings."), "warning");
      else notify(t("Redacted local report exported."), "success");
    } catch (error) {
      notifyFailure(error);
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="min-w-0 space-y-2" aria-labelledby="data-quality-title">
      <PageHeader id="data-quality-title" breadcrumb={t("FinIntel / Data & Operations")}
        title={t("Data Quality")}
        subtitle={t("Investigate persisted findings and approve only explicit, reversible corrections. Source prices never change.")}
        actions={
          <>
            <Button size="sm" variant="secondary" disabled={loading || !!busy} onClick={reload}>
              <RefreshCw aria-hidden="true" /> {t("Refresh")}
            </Button>
            <Button size="sm" disabled={!!busy} onClick={() => void perform(
              "scan", () => runQualityScan(1000), "Quality scan completed or saved a resumable checkpoint.",
            )}>{busy === "scan" ? t("Scanning…") : t("Run / resume quality scan")}</Button>
          </>
        } />
      <Card className="gap-3 py-3">
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <fieldset className="flex items-center gap-2" aria-label={t("Instrument scope")}>
              <Button size="sm" variant={scope === "index" ? "default" : "secondary"} aria-pressed={scope === "index"} onClick={() => changeScope("index")}>{t("Indexes")}</Button>
              <Button size="sm" variant={scope === "equity" ? "default" : "secondary"} aria-pressed={scope === "equity"} onClick={() => changeScope("equity")}>{t("Stocks / ETFs")}</Button>
            </fieldset>
            {scope === "index" ? (
              <label className="flex items-center gap-2 text-sm">{t("Index")}
                <select className={selectClass} aria-label={t("Select index")} value={indexCode}
                  onChange={event => setIndexCode(event.target.value as typeof indexCode)}>
                  {indexCodes.map(item => <option key={item} value={item}>{item === "SP500" ? "S&P 500 (SP500)" : item}</option>)}
                </select>
              </label>
            ) : (
              <form className="flex flex-wrap items-center gap-2" onSubmit={event => {
                event.preventDefault();
                const next = symbolInput.trim().toUpperCase();
                if (/^[A-Z0-9][A-Z0-9.-]{0,14}$/.test(next)) setEquityCode(next);
                else notify(t("Enter a valid equity ticker."), "warning");
              }}>
                <label className="text-sm" htmlFor="quality-symbol">{t("Ticker")}</label>
                <input id="quality-symbol" className={inputClass} value={symbolInput} maxLength={15}
                  onChange={event => setSymbolInput(event.target.value)} />
                <Button type="submit" size="sm" variant="secondary"><Search aria-hidden="true" /> {t("View")}</Button>
              </form>
            )}
            <Badge variant="outline">{code}</Badge>
          </div>
          <p className="text-xs text-muted-foreground">{scopeDescription}</p>
          {overview && <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
            {[
              [t("Active findings"), overview.total],
              [t("Candidates"), overview.counts.review_state.candidate ?? 0],
              [t("Confirmed"), overview.counts.review_state.confirmed ?? 0],
              [t("Warnings"), overview.counts.severity.warning ?? 0],
              [t("Errors"), overview.counts.severity.error ?? 0],
            ].map(([label, value]) => <div key={label} className="rounded-md border border-border px-3 py-2">
              <p className="text-xs text-muted-foreground">{label}</p>
              <p className="text-lg font-semibold tabular-nums">{Number(value).toLocaleString(language)}</p>
            </div>)}
          </div>}
          {overview && <div role="status" aria-live="polite" className="flex flex-wrap items-center gap-2 text-xs">
            {overview.scan.complete ? <CheckCircle2 aria-hidden="true" className="size-4 text-positive" />
              : <AlertTriangle aria-hidden="true" className="size-4 text-warning" />}
            <span>{t("Scan status")}: {t(overview.scan.status)}</span>
            {overview.scan.stale && <Badge variant="destructive">{t("Stale data")}</Badge>}
            {overview.scan.partial && <Badge variant="outline">{t("Partial scan")}</Badge>}
            {!overview.scan.complete && <span className="text-muted-foreground">{t("Results may be incomplete. Candidates are not confirmed defects.")}</span>}
            {overview.scan.cursor_phase && <span className="text-muted-foreground">
              {t("Checkpoint")}: {t(overview.scan.cursor_phase)} · {overview.scan.cursor_next ?? 0}
            </span>}
            <span className="text-muted-foreground">{t("Examined")}: {overview.scan.findings_examined ?? "—"}</span>
            {busy === "scan" && <span>{t("Scan in progress. The backend reports results on completion.")}</span>}
          </div>}
          {!!loadError && <p role="alert" className="text-sm text-destructive">{loadError}</p>}
          {loading && <p role="status" className="text-sm text-muted-foreground">{t("Loading quality findings…")}</p>}
        </CardContent>
      </Card>

      <Card className="gap-3 py-3">
        <CardHeader><CardTitle>{t("Findings & evidence")}</CardTitle>
          <CardDescription>{t("Filter and review stored quality candidates. Findings remain separate from approved corrections.")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap gap-2">
            <label className="text-xs text-muted-foreground">{t("Finding type")}
              <select aria-label={t("Finding type")} className={"mt-1 block " + selectClass} value={typeFilter}
                onChange={event => setTypeFilter(event.target.value)}>
                <option value="">{t("All types")}</option>
                {["missing_session", "price_anomaly", "ohlcv_anomaly", "provider_divergence", "duplicate", "stale_observation"].map(item =>
                  <option key={item} value={item}>{t(item)}</option>)}
              </select>
            </label>
            <label className="text-xs text-muted-foreground">{t("Review state")}
              <select aria-label={t("Review state")} className={"mt-1 block " + selectClass} value={reviewFilter}
                onChange={event => setReviewFilter(event.target.value)}>
                <option value="">{t("All reviews")}</option>
                {(["candidate", "confirmed", "rejected", "accepted_correction"] as const).map(item =>
                  <option key={item} value={item}>{t(item)}</option>)}
              </select>
            </label>
            <label className="text-xs text-muted-foreground">{t("Provider")}
              <input aria-label={t("Provider")} className={"mt-1 block w-40 " + inputClass}
                placeholder={t("Exact provider")} value={providerFilter}
                onChange={event => setProviderFilter(event.target.value.slice(0, 80))} />
            </label>
            <div className="ml-auto flex items-end gap-2">
              <Button size="sm" variant="secondary" disabled={!overview || !!busy} onClick={() => void exportReport("json")}>
                <Download aria-hidden="true" /> {t("Export JSON")}
              </Button>
              <Button size="sm" variant="secondary" disabled={!overview || !!busy} onClick={() => void exportReport("csv")}>
                <Download aria-hidden="true" /> {t("Export CSV")}
              </Button>
            </div>
          </div>
          <p className="text-xs text-muted-foreground">{t("Exports include up to 5,000 filtered findings with allowlisted, redacted evidence.")}</p>
          <div className="overflow-x-auto rounded-md border border-border">
            <table className="w-full min-w-[760px] text-left text-xs">
              <thead className="bg-muted text-muted-foreground"><tr>
                {["Session", "Type", "Provider", "Rule", "Severity", "Review state", "Details"].map(label =>
                  <th key={label} scope="col" className="px-2 py-2">{t(label)}</th>)}
              </tr></thead>
              <tbody>{(findings?.items ?? []).map(item => <tr key={item.id} className="border-t border-border hover:bg-muted/30">
                <td className="whitespace-nowrap px-2 py-2">{item.session_date ?? "—"}</td>
                <td className="px-2 py-2">{t(item.finding_type)}</td>
                <td className="px-2 py-2">{item.provider}</td>
                <td className="max-w-48 truncate px-2 py-2" title={item.rule}>{item.rule}</td>
                <td className="px-2 py-2"><Badge variant={item.severity === "error" ? "destructive" : "outline"}>{t(item.severity)}</Badge></td>
                <td className="px-2 py-2">{t(item.review_state)}</td>
                <td className="px-2 py-2"><Button size="sm" variant="ghost" aria-expanded={selectedFinding === item.id}
                  onClick={() => openFinding(item)}>{t("Inspect")}</Button></td>
              </tr>)}</tbody>
            </table>
            {!loading && !(findings?.items.length) && <p className="p-4 text-sm text-muted-foreground">{t("No matching quality findings.")}</p>}
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
            <span aria-live="polite">{t("Showing")} {findings?.total ? page * pageSize + 1 : 0}–{Math.min((page + 1) * pageSize, findings?.total ?? 0)} {t("of")} {findings?.total ?? 0} · {t("Page")} {page + 1} {t("of")} {pageCount}</span>
            <div className="flex gap-2">
              <Button size="sm" variant="secondary" disabled={loading || page === 0} onClick={() => setPage(v => Math.max(0, v - 1))}>{t("Previous")}</Button>
              <Button size="sm" variant="secondary" disabled={loading || page + 1 >= pageCount} onClick={() => setPage(v => v + 1)}>{t("Next")}</Button>
            </div>
          </div>
          {selectedFinding !== null && <section className="space-y-3 rounded-md border border-border bg-muted/20 p-3" aria-label={t("Finding investigation")}>
            {!history && !historyError && <p className="text-xs">{t("Loading review history…")}</p>}
            {historyError && <p role="alert" className="text-xs text-destructive">{historyError}</p>}
            {history && <>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-sm font-semibold">{t("Finding investigation")} #{history.finding.id}</h3>
                <Button size="sm" variant="ghost" onClick={() => setSelectedFinding(null)}>{t("Close details")}</Button>
              </div>
              <p className="text-xs text-muted-foreground">{history.finding.code} · {history.finding.provider} · {history.finding.rule}</p>
              <dl className="grid gap-2 text-xs sm:grid-cols-2 xl:grid-cols-4">
                {Object.entries(redactedEvidence(history.finding.evidence)).map(([key, value]) => <div key={key} className="min-w-0 rounded border border-border px-2 py-1">
                  <dt className="text-muted-foreground">{t(key.replaceAll("_", " "))}</dt>
                  <dd className="break-all font-medium">{String(value)}</dd>
                </div>)}
              </dl>
              <div className="space-y-1">
                <h4 className="text-xs font-semibold">{t("Review history")}</h4>
                {history.reviews.length ? <ol className="space-y-1 text-xs">
                  {history.reviews.map(entry => <li key={entry.id} className="rounded border border-border px-2 py-1">
                    {entry.reviewed_at} · {t(entry.from_state)} → {t(entry.to_state)} · {entry.note || "—"}
                  </li>)}
                </ol> : <p className="text-xs text-muted-foreground">{t("No review decisions yet.")}</p>}
                {history.source_revision.superseded_at && <p role="status" className="text-xs text-warning">{t("Superseded")}: {history.source_revision.superseded_at} · {history.source_revision.supersession_reason}</p>}
              </div>
              <form className="flex flex-wrap items-end gap-2" onSubmit={event => {
                event.preventDefault();
                if (!reviewNote.trim()) { notify(t("Enter a review note."), "warning"); return; }
                void perform("review", () => submitFindingReview(history.finding.id, reviewState, reviewNote.trim()), "Review decision recorded.");
              }}>
                <label className="text-xs">{t("Review decision")}
                  <select className={"mt-1 block " + selectClass} value={reviewState}
                    onChange={event => setReviewState(event.target.value as FindingState)}>
                    {(["candidate", "confirmed", "rejected", "accepted_correction"] as const).map(item =>
                      <option key={item} value={item}>{t(item)}</option>)}
                  </select>
                </label>
                <label className="min-w-40 flex-1 text-xs">{t("Review note")}
                  <input className={"mt-1 w-full " + inputClass} value={reviewNote} maxLength={2000}
                    onChange={event => setReviewNote(event.target.value)} required />
                </label>
                <Button type="submit" size="sm" disabled={!!busy}>{t("Save review")}</Button>
              </form>
              {canPropose && <form className="grid gap-2 border-t border-border pt-3 md:grid-cols-[minmax(120px,1fr)_minmax(120px,1fr)_minmax(200px,2fr)_auto]"
                onSubmit={event => {
                  event.preventDefault();
                  if (!proposedClose || !correctionNote.trim()) return;
                  void perform("propose", async () => {
                    const result = await createQualityCorrection(history.finding.id, proposedProvider, proposedClose, correctionNote.trim());
                    setSelectedCorrection(result.id);
                  }, "Correction proposal created; preview and validate before approval.");
                }}>
                <label className="text-xs">{t("Source provider")}
                  <select className={"mt-1 w-full " + selectClass} value={proposedProvider} onChange={event => setProposedProvider(event.target.value)}>
                    {history.finding.provider.split("|").map(p => <option key={p} value={p}>{p}</option>)}
                  </select>
                </label>
                <label className="text-xs">{t("Proposed close")}
                  <input type="number" step="any" min="0.00000000001" required className={"mt-1 w-full " + inputClass}
                    value={proposedClose} onChange={event => setProposedClose(event.target.value)} />
                </label>
                <label className="text-xs">{t("Correction rationale")}
                  <input className={"mt-1 w-full " + inputClass} value={correctionNote} required maxLength={2000}
                    onChange={event => setCorrectionNote(event.target.value)} />
                </label>
                <Button type="submit" size="sm" className="self-end" disabled={!!busy}>{t("Propose correction")}</Button>
              </form>}
              <p className="text-xs text-muted-foreground">{t("A confirmed finding is necessary. No correction changes raw observations.")}</p>
            </>}
          </section>}
        </CardContent>
      </Card>

      <Card className="gap-3 py-3">
        <CardHeader><CardTitle>{t("Correction overlays")}</CardTitle>
          <CardDescription>{t("Approval only affects the explicit resolved-close view; original OHLCV and index levels remain unchanged.")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {!corrections?.items.length ? <p className="text-sm text-muted-foreground">{t("No correction proposals for this instrument.")}</p> :
            <div className="overflow-x-auto rounded-md border border-border">
              <table className="w-full min-w-[640px] text-left text-xs">
                <thead className="bg-muted text-muted-foreground"><tr>
                  {["Session", "Provider", "Original close", "Proposed close", "State", "Details"].map(label =>
                    <th scope="col" className="px-2 py-2" key={label}>{t(label)}</th>)}
                </tr></thead>
                <tbody>{corrections.items.map(item => <tr key={item.id} className="border-t border-border">
                  <td className="px-2 py-2">{item.session_date}</td>
                  <td className="px-2 py-2">{item.provider}</td>
                  <td className="px-2 py-2 tabular-nums">{item.source_close}</td>
                  <td className="px-2 py-2 tabular-nums">{item.proposed_close}</td>
                  <td className="px-2 py-2">{t(item.state)}</td>
                  <td className="px-2 py-2"><Button variant="ghost" size="sm" onClick={() => setSelectedCorrection(item.id)}>{t("Preview")}</Button></td>
                </tr>)}</tbody>
              </table>
            </div>}
          {selectedCorrection && <>
            <div className="rounded-md border border-border p-3 text-xs">
              <h3 className="mb-2 text-sm font-semibold">{t("Correction preview")}</h3>
              {!preview ? <p>{t("Loading correction preview…")}</p> : <>
                <dl className="grid gap-2 sm:grid-cols-3">
                  <div><dt className="text-muted-foreground">{t("Original close")}</dt><dd>{preview.captured_source_close}</dd></div>
                  <div><dt className="text-muted-foreground">{t("Proposed close")}</dt><dd>{preview.proposed_close}</dd></div>
                  <div><dt className="text-muted-foreground">{t("Relative delta")}</dt><dd>{preview.relative_delta}</dd></div>
                </dl>
                {preview.stale_source && <p role="alert" className="my-2 text-destructive">{t("Provider revision changed; this overlay cannot be approved or applied.")}</p>}
                {preview.approved_conflict && <p role="alert" className="my-2 text-destructive">{t("Another approved overlay already exists.")}</p>}
                <p className="my-2 text-muted-foreground">{t("Review the original value and the proposed change before approval. Reversal restores the original effective view.")}</p>
                {preview.state === "validated" && <label className="my-2 flex items-center gap-2">
                  <input type="checkbox" checked={approvalChecked} onChange={event => setApprovalChecked(event.target.checked)} />
                  {t("I reviewed the original and proposed close values.")}
                </label>}
                <div className="flex flex-wrap items-end gap-2">
                  <label className="min-w-40 flex-1">{t("Decision note")}
                    <input value={correctionNote} onChange={event => setCorrectionNote(event.target.value)}
                      className={"mt-1 w-full " + inputClass} maxLength={2000} />
                  </label>
                  {([
                    ["validate", preview.eligible_for_validation, "Validate proposal"],
                    ["approve", preview.eligible_for_approval && approvalChecked && !!correctionNote.trim(), "Approve overlay"],
                    ["reject", ["proposed", "validated"].includes(preview.state) && !!correctionNote.trim(), "Reject proposal"],
                    ["reverse", preview.state === "approved" && !!correctionNote.trim(), "Reverse overlay"],
                  ] as const).filter(([, enabled, actionLabel]) => enabled || (
                    actionLabel === "Approve overlay" && preview.state === "validated"
                  )).map(([action, enabled, label]) =>
                    <Button key={action} type="button" size="sm" disabled={!enabled || !!busy}
                      variant={action === "approve" ? "default" : "secondary"}
                      onClick={() => void perform("transition", () =>
                        transitionQualityCorrection(selectedCorrection, action as CorrectionAction, correctionNote.trim()),
                        "Correction decision recorded.",
                      )}>{t(label)}</Button>)}
                </div>
              </>}
            </div>
            <div className="text-xs">
              <h4 className="font-semibold">{t("Correction audit history")}</h4>
              {(corrections?.items.find(item => item.id === selectedCorrection)?.events ?? []).map(event =>
                <p key={event.id} className="mt-1 rounded border border-border p-2">
                  {event.occurred_at} · {event.from_state ? t(event.from_state) : t("Created")} → {t(event.to_state)} · {event.note || "—"}
                </p>)}
            </div>
          </>}
        </CardContent>
      </Card>

      <Card className="gap-3 py-3">
        <CardHeader><CardTitle>{t("Source provenance & corporate actions")}</CardTitle>
          <CardDescription>{t("Sourced split/dividend references are not the same as price-jump suspicions. Verification is explicit.")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2 text-xs">
          <div className="flex flex-wrap gap-2">
            {providerCounts.map(item => <Badge variant="outline" key={item.provider}>
              {item.provider}: {item.series_count.toLocaleString(language)} {t("series")}
            </Badge>)}
            {scope === "equity" && <Button size="sm" variant="secondary" disabled={!!busy}
              onClick={() => void perform("verify", () => verifyQualityCorporateActions(code),
                "Corporate actions verified; sourced evidence saved locally.")}>
              <ShieldCheck aria-hidden="true" /> {t("Verify sourced corporate actions")}
            </Button>}
          </div>
          <div className="grid gap-2 md:grid-cols-2">
            <div className="rounded-md border border-border p-3">
              <h3 className="font-semibold">{t("Sourced events")} ({corporate?.sourced.length ?? 0})</h3>
              {(corporate?.sourced ?? []).slice(0, 20).map(event =>
                <p key={event.id} className="mt-2 break-words">{event.session_date} · {event.provider} · {t(event.kind)} · {t("Split factor")}: {event.split_factor} · {t("Dividend")}: {event.cash_dividend}</p>)}
              {!corporate?.sourced.length && <p className="mt-2 text-muted-foreground">{t("No sourced corporate actions are saved locally.")}</p>}
            </div>
            <div className="rounded-md border border-border p-3">
              <h3 className="font-semibold">{t("Suspected events")} ({corporate?.suspected.length ?? 0})</h3>
              {(corporate?.suspected ?? []).slice(0, 20).map(event =>
                <p key={event.finding_id} className="mt-2 break-words">{event.session_date} · {event.provider} · {event.rule}</p>)}
              {!corporate?.suspected.length && <p className="mt-2 text-muted-foreground">{t("No suspected corporate actions in active findings.")}</p>}
            </div>
          </div>
          <p className="text-muted-foreground">{t("No provider credentials or raw provider payloads are exported. No unapproved correction changes market history.")}</p>
        </CardContent>
      </Card>
    </section>
  );
}
