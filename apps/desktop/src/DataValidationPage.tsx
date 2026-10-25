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
  return (
    <section className="space-y-4" aria-labelledby="validation-title">
      <header className="border-b border-border pb-3">
        <p className="text-[10px] font-bold tracking-[0.16em] text-[#8aa5c3] uppercase">FinIntel / Data & Operations</p>
        <h1 id="validation-title" className="mt-2 text-2xl font-semibold">Data Validation</h1>
        <p className="mt-2 text-sm text-muted-foreground">Run read-only quality checks on locally stored historical market data.</p>
      </header>
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
            <pre className="max-h-[560px] overflow-auto rounded-lg bg-muted p-4 text-xs whitespace-pre-wrap break-words">{report}</pre>
          </>}
          {job.status === "idle" && <p className="text-sm text-muted-foreground">No validation job has run in this backend session.</p>}
        </CardContent>
      </Card>
    </section>
  );
}
