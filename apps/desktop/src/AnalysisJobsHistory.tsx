import { useLayoutEffect, useRef, useState } from "react";
import { isTauri } from "@tauri-apps/api/core";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TablePager, TABLE_PAGE_SIZE } from "./components/TablePagination";
import { PageHeader } from "./PageHeader";
import { AnalysisJobDetail } from "./AnalysisJobDetail";
import { listAnalysisJobs, type AnalysisJobFilter, type AnalysisJobPage } from "./analysis-jobs-api";
import { analysisErrorMessage, MODELS } from "./analysis-workflow";
import { useLocale } from "./localization";

const statuses = ["all", "queued", "running", "cancel_requested", "completed", "failed", "cancelled"] as const;
const offsetLimit = 10_010;
const headers = ["Model", "Sources", "Requested window", "Created", "Status", "Progress", "Attempts", "Actions"];

export function AnalysisJobsHistory({ initialJobId }: { initialJobId?: string }) {
  const { t, language } = useLocale();
  const [filter, setFilter] = useState<AnalysisJobFilter>("all");
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState(initialJobId);
  const [revision, setRevision] = useState(0);
  const [data, setData] = useState<AnalysisJobPage>();
  const [error, setError] = useState("");
  const native = isTauri();
  const generation = useRef(0);
  const selectionGeneration = useRef(0);
  const selectJob = (id?: string) => {
    if (id === selected) return;
    selectionGeneration.current++;
    setSelected(id);
  };

  function navigate(nextFilter: AnalysisJobFilter, nextPage: number) {
    if (nextFilter === filter && nextPage === page) return;
    generation.current++;
    selectJob();
    setData(undefined);
    setError("");
    setFilter(nextFilter);
    setPage(nextPage);
  }

  useLayoutEffect(() => {
    if (!native) return;
    let active = true;
    let pending = false;
    const load = async () => {
      if (pending) return;
      pending = true;
      const serial = ++generation.current;
      const current = () => active && serial === generation.current;
      try {
        const next = await listAnalysisJobs(page + 1, filter);
        if (!current()) return;
        const last = Math.max(0, Math.ceil(Math.min(next.total, offsetLimit) / TABLE_PAGE_SIZE) - 1);
        if (page > last) {
          generation.current++;
          selectionGeneration.current++;
          setSelected(undefined);
          setData(undefined);
          setPage(last);
        } else {
          setData(next);
          setError("");
        }
      } catch (reason) {
        if (current()) setError(analysisErrorMessage(reason));
      } finally {
        pending = false;
      }
    };
    void load();
    const timer = window.setInterval(() => { void load(); }, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [native, page, filter, revision]);

  return <section aria-labelledby="analysis-jobs-title" className="min-w-0 space-y-2">
    <PageHeader id="analysis-jobs-title" breadcrumb={t("FinIntel / Model Jobs")} title={t("Model Jobs")}
      subtitle={t("Run and inspect source-pinned local research jobs. No trades or backtests.")} />
    <Card className="min-w-0">
    <CardHeader><CardTitle>{t("Analysis Library Jobs")}</CardTitle></CardHeader>
    <CardContent className="space-y-3">
      {!native ? <p role="status">{t("Analysis Library jobs require the managed FinIntel desktop.")}</p> : <>
        {selected && <AnalysisJobDetail key={selected} id={selected} initial={data?.items.find(job => job.id === selected)}
          selectionGeneration={selectionGeneration} onClose={() => selectJob()} onChanged={() => { generation.current++; setRevision(value => value + 1); }} />}
        <label className="flex flex-wrap items-center gap-2 text-sm">{t("Filter job status")}
          <select className="h-9 rounded-md border border-input bg-background px-2" value={filter}
            onChange={event => navigate(event.target.value as AnalysisJobFilter, 0)}>
            {statuses.map(status => <option key={status} value={status}>{t(status === "all" ? "All" : status)}</option>)}
          </select>
        </label>
        {!data && !error && <p role="status">{t("Loading model jobs…")}</p>}
        {error && <p role="alert" className="text-destructive">{t(error)}</p>}
        {data && <div className="space-y-2">
          {data.items.length ? <div className="overflow-x-auto rounded-md border">
            <table aria-label={t("Analysis Library Jobs")} className="w-full whitespace-nowrap text-left text-sm">
              <thead className="bg-muted"><tr>{headers.map(header => <th scope="col" key={header} className="p-2">{t(header)}</th>)}</tr></thead>
              <tbody>{data.items.map(job => <tr key={job.id} className="border-t">
                <td className="p-2">{t(MODELS[job.model_id][0])}</td>
                <td className="p-2">{job.request.sources.map(source => source.identity.code).join(" → ")}</td>
                <td className="p-2 tabular-nums">{job.request.window.start} – {job.request.window.end}</td>
                <td className="p-2">{new Date(job.created_at).toLocaleString(language)}</td>
                <td className="p-2">{t(job.status)}</td>
                <td className="p-2 tabular-nums">{job.progress}%</td>
                <td className="p-2 tabular-nums">{job.attempts}/3</td>
                <td className="p-2"><Button type="button" size="sm" variant={job.id === selected ? "secondary" : "outline"}
                  aria-pressed={job.id === selected} onClick={() => selectJob(job.id)}>{t("Open job details")}</Button></td>
              </tr>)}</tbody>
            </table>
          </div> : <p role="status">{t("No matching model jobs.")}</p>}
          <TablePager page={page} total={Math.min(data.total, offsetLimit)} onPageChange={value => navigate(filter, value)} />
          {data.total > offsetLimit && <p className="text-xs text-muted-foreground">{t("History is limited to the first 10,010 entries; narrow the status filter.")}</p>}
        </div>}

      </>}
    </CardContent>
    </Card>
  </section>;
}
