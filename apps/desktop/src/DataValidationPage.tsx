import { PageHeader } from "./PageHeader";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { fetchValidationStatus, startValidation, type ValidationCheck, type ValidationJob } from "./validation-api";

const options: { id: ValidationCheck; label: string; detail: string }[] = [
  { id: "coverage", label: "P0 · Historical coverage", detail: "Observed ranges and potential weekday gaps (holidays not excluded)" },
  { id: "integrity", label: "P0 · Price integrity", detail: "Streaming OHLCV and chronological validation" },
  { id: "corporate", label: "P1 · Corporate-action screening", detail: "Potential split-like jumps; not confirmed corporate actions" },
  { id: "provider", label: "P1 · Provider consistency", detail: "Compare overlapping dates with identical adjustment labels" },
  { id: "performance", label: "P2 · Query performance", detail: "Report time spent reading series and slowest queries" },
];

export function DataValidationPage() {
  const [selected, setSelected] = useState<ValidationCheck[]>(options.map((option) => option.id));
  const [job, setJob] = useState<ValidationJob>({ status: "idle" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    const refresh = async () => {
      try {
        const result = await fetchValidationStatus();
        if (active) setJob(result);
      } catch (cause) {
        if (active) setError(String(cause));
      }
    };
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 2000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  const run = async () => {
    setBusy(true);
    setError("");
    try {
      setJob(await startValidation(selected));
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  };
  const running = job.status === "running" || job.status === "queued";
  const report = job.results ? JSON.stringify(job.results, null, 2) : null;
  const results = (job.results ?? {}) as Record<string, Record<string, unknown>>;
  const metric = (section: string, field: string): string => {
    const value = results[section]?.[field];
    return typeof value === "number" ? value.toLocaleString() : "—";
  };
  const rows = (section: string, field: string): Record<string, unknown>[] => {
    const value = results[section]?.[field];
    return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> =>
      typeof item === "object" && item !== null && !Array.isArray(item)) : [];
  };
  const display = (value: unknown) => value == null ? "—" : String(value);
  const detailTable = (section: string, field: string, columns: string[], heading: string) => {
    const items = rows(section, field);
    return <details className="rounded-md border border-border p-3">
      <summary className="cursor-pointer text-sm font-medium">{heading} ({items.length} examples)</summary>
      {items.length ? <div className="mt-3 overflow-x-auto"><table className="w-full text-left text-xs">
        <thead><tr>{columns.map((column) => <th className="border-b border-border p-2" key={column}>{column.replaceAll("_", " ")}</th>)}</tr></thead>
        <tbody>{items.map((item, index) => <tr key={index} className="border-b border-border/50">
          {columns.map((column) => <td key={column} className="p-2">{display(item[column])}</td>)}
        </tr>)}</tbody>
      </table></div> : <p className="mt-2 text-xs text-muted-foreground">No examples reported.</p>}
    </details>;
  };
  return (
    <section className="space-y-2" aria-labelledby="validation-title">
      <PageHeader id="validation-title" breadcrumb="FinIntel / Data & Operations" title="Data Validation" subtitle="Run read-only quality checks on locally stored historical market data." />
      <Card>
        <CardHeader>
          <CardTitle>Validation checks</CardTitle>
          <CardDescription>Choose checks to run. Historical scans may take several minutes; Markets remains available.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {options.map((option) => (
            <label key={option.id} className="flex cursor-pointer items-start gap-3 rounded-md border border-border p-3">
              <input type="checkbox" className="mt-1 size-4" checked={selected.includes(option.id)}
                disabled={running}
                onChange={(event) => setSelected((previous) =>
                  event.target.checked ? [...previous, option.id] : previous.filter((id) => id !== option.id))} />
              <span><span className="block text-sm font-medium">{option.label}</span>
                <span className="block text-xs text-muted-foreground">{option.detail}</span></span>
            </label>
          ))}
          <Button onClick={() => { void run(); }} disabled={busy || running || selected.length === 0}>
            {running ? "Validation running…" : busy ? "Starting…" : "Run selected checks"}
          </Button>
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Latest validation</CardTitle>
          <CardDescription>Status: {job.status} {job.stage ? "· " + job.stage : ""}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {job.started_at && <p className="text-xs text-muted-foreground">Started: {new Date(job.started_at).toLocaleString()}</p>}
          {job.finished_at && <p className="text-xs text-muted-foreground">Finished: {new Date(job.finished_at).toLocaleString()}</p>}
          {running && <p role="status" className="text-sm">The local backend is processing the selected checks. You can navigate to other pages.</p>}
          {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
          {job.error && <p role="alert" className="text-sm text-destructive">{job.error}</p>}
          {report && <>
            <div className="flex justify-end">
              <Button variant="secondary" onClick={() => {
                const url = URL.createObjectURL(new Blob([report], { type: "application/json" }));
                const link = document.createElement("a");
                link.href = url; link.download = "finintel-validation-report.json"; link.click();
                window.setTimeout(() => URL.revokeObjectURL(url), 1000);
              }}>Export JSON</Button>
            </div>
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
              {[
                ["Series", metric("coverage", "series_count")],
                ["Price records", metric("integrity", "price_rows")],
                ["Integrity issues", String(Object.values((results.integrity?.issues ?? {}) as Record<string, number>).reduce((a, b) => a + (typeof b === "number" ? b : 0), 0))],
                ["Potential missing SPY sessions", metric("coverage", "reference_session_gap_candidates")],
              ].map(([label, value]) => <div className="rounded-md border border-border p-3" key={label}>
                <p className="text-xs text-muted-foreground">{label}</p><p className="mt-1 text-xl font-semibold tabular-nums">{value}</p>
              </div>)}
            </div>
            {results.coverage && <div className="space-y-2">
              <h3 className="font-semibold">P0 · Coverage</h3>
              <p className="text-sm text-muted-foreground">Less than nine years observed: {metric("coverage", "series_with_less_than_9_years_observed")}. Weekday gaps are candidates, not confirmed missing sessions.</p>
              {detailTable("coverage", "reference_gap_examples", ["symbol", "provider", "previous", "next", "reference_sessions_missing"], "SPY reference gap candidates")}
              {detailTable("coverage", "coverage_examples", ["symbol", "provider", "rows", "first", "last"], "Observed coverage")}
            </div>}
            {results.integrity && <div className="space-y-2"><h3 className="font-semibold">P0 · Integrity</h3>
              <p className="text-sm">Checked {metric("integrity", "price_rows")} price rows · {display(results.integrity.integrity)}</p>
              {detailTable("integrity", "issue_examples", ["series_id", "date", "issue"], "Price anomalies")}
            </div>}
            {results.corporate && <div className="space-y-2"><h3 className="font-semibold">P1 · Corporate actions</h3>
              <p className="text-sm">Large jump candidates: {metric("corporate", "corporate_action_jump_candidate_count")}. These are not confirmed splits or dividends.</p>
              {detailTable("corporate", "corporate_action_jump_candidates", ["symbol", "provider", "date", "close_ratio", "adjustment"], "Jump candidates")}
            </div>}
            {results.provider && <div className="space-y-2"><h3 className="font-semibold">P1 · Provider comparisons</h3>
              <p className="text-sm">Compared pairs: {metric("provider", "cross_provider_pairs_compared")} · Differences over 1%: {metric("provider", "cross_provider_close_divergences_over_1pct")}</p>
              {detailTable("provider", "cross_provider_comparisons", ["symbol", "overlap_sessions", "close_divergence_over_1pct"], "Provider differences")}
            </div>}
            {results.performance && <div className="space-y-2"><h3 className="font-semibold">P2 · Performance</h3>
              <p className="text-sm">Diagnostic runtime: {metric("performance", "elapsed_seconds")} seconds</p>
              {detailTable("performance", "slowest_series_queries_seconds", ["series", "seconds"], "Slowest series queries")}
            </div>}
            <details className="rounded-md border border-border p-3"><summary className="cursor-pointer text-sm">Raw JSON report</summary>
              <pre className="mt-2 max-h-[560px] overflow-auto rounded-lg bg-muted p-4 text-xs whitespace-pre-wrap break-words">{report}</pre>
            </details>
          </>}
          {job.status === "idle" && <p className="text-sm text-muted-foreground">No validation job has run in this backend session.</p>}
        </CardContent>
      </Card>
    </section>
  );
}
