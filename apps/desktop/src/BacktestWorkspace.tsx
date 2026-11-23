import { useLayoutEffect, useRef, useState, type RefObject } from "react";
import { isTauri } from "@tauri-apps/api/core";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TablePager, boundedPage } from "./components/TablePagination";
import { PageHeader } from "./PageHeader";
import { BacktestForm, type SmaBacktestSelection } from "./BacktestForm";
import { BacktestJobDetail } from "./BacktestJobDetail";
import { listBacktestJobs, type BacktestJobPage, type BacktestJobSummary } from "./backtest-jobs-api";
import { statuses, type AnalysisJobFilter } from "./analysis-jobs-api";
import { backtestError } from "./backtest-workflow";
import { useLocale } from "./localization";

const historyCap = 10_010;
const headers = ["Created", "Source index", "Model", "Training end", "Test period", "Status", "Progress", "Attempts", "Actions"];

type BacktestWorkspaceProps = {
  initialSource?: SmaBacktestSelection;
  workspaceGeneration?: RefObject<number>;
};

function ManagedBacktests({ initialSource, workspaceGeneration }: BacktestWorkspaceProps) {
  const { t, language } = useLocale();
  const [filter, setFilter] = useState<AnalysisJobFilter>("all");
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<BacktestJobSummary>();
  const [revision, setRevision] = useState(0);
  const [data, setData] = useState<BacktestJobPage>();
  const [error, setError] = useState("");
  const listGeneration = useRef(0);
  const selectionGeneration = useRef(0);
  const historyHeading = useRef<HTMLHeadingElement>(null);

  function refresh() {
    listGeneration.current++;
    setRevision(value => value + 1);
  }
  function select(job?: BacktestJobSummary) {
    if (job?.id === selected?.id && job?.request_sha256 === selected?.request_sha256) return;
    selectionGeneration.current++;
    setSelected(job);
  }
  function navigate(nextFilter: AnalysisJobFilter, nextPage: number) {
    if (nextFilter === filter && nextPage === page) return;
    listGeneration.current++;
    select();
    setData(undefined);
    setError("");
    setFilter(nextFilter);
    setPage(nextPage);
  }

  useLayoutEffect(() => {
    const workspace = workspaceGeneration?.current;
    let active = true;
    let pending = false;
    const load = async () => {
      if (pending) return;
      pending = true;
      const serial = ++listGeneration.current;
      const current = () => active && serial === listGeneration.current && workspace === workspaceGeneration?.current;
      try {
        const next = await listBacktestJobs(page + 1, filter);
        if (!current()) return;
        const bounded = boundedPage(page, Math.min(next.total, historyCap));
        if (bounded !== page) {
          listGeneration.current++;
          selectionGeneration.current++;
          setSelected(undefined);
          setData(undefined);
          setPage(bounded);
        } else {
          setData(next);
          setError("");
        }
      } catch (failure) {
        if (current()) setError(backtestError(failure));
      } finally {
        pending = false;
      }
    };
    void load();
    const timer = window.setInterval(() => { void load(); }, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [page, filter, revision, workspaceGeneration]);

  return <>
    <BacktestForm initialSource={initialSource} workspaceGeneration={workspaceGeneration} onQueued={refresh} onInspect={job => {
      historyHeading.current?.focus();
      select(job);
    }} onHistory={() => historyHeading.current?.focus()} />
    <Card className="min-w-0">
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-2">
        <CardTitle><h2 ref={historyHeading} tabIndex={-1} className="scroll-mt-24 rounded-sm focus-visible:ring-2 focus-visible:ring-ring">{t("Backtest History")}</h2></CardTitle>
        <Button type="button" variant="outline" size="sm" onClick={refresh}>{t("Refresh backtest history")}</Button>
      </CardHeader>
      <CardContent className="space-y-3">
        {selected && <BacktestJobDetail key={selected.id + ":" + selected.request_sha256} initial={selected}
          selectionGeneration={selectionGeneration} workspaceGeneration={workspaceGeneration} onChanged={refresh} onClose={() => select()} />}
        <label className="flex flex-wrap items-center gap-2 text-sm">{t("Filter job status")}
          <select className="h-9 rounded-md border border-input bg-background px-2 focus-visible:ring-2 focus-visible:ring-ring"
            value={filter} onChange={event => navigate(event.target.value as AnalysisJobFilter, 0)}>
            {(["all", ...statuses] as const).map(status => <option key={status} value={status}>{t(status === "all" ? "All" : status)}</option>)}
          </select>
        </label>
        {!data && !error && <p role="status">{t("Loading backtest history…")}</p>}
        {error && <p role="alert" className="text-destructive">{t(error)}</p>}
        {data && <>
          {data.items.length > 0 ? <div className="overflow-x-auto rounded-md border">
            <table aria-label={t("Backtest History")} className="w-full whitespace-nowrap text-left text-sm">
              <thead className="bg-muted"><tr>{headers.map(header => <th scope="col" key={header} className="p-2">{t(header)}</th>)}</tr></thead>
              <tbody>{data.items.map(job => {
                const simulation = job.request.simulation;
                return <tr key={job.id} className="border-t tabular-nums">
                  <td className="p-2">{new Date(job.created_at).toLocaleString(language)}</td>
                  <th scope="row" className="p-2 font-medium">{simulation.index_code}</th>
                  <td className="p-2">{t("SMA crossover")} · {job.model_version}</td>
                  <td className="p-2">{simulation.training_end}</td>
                  <td className="p-2">{simulation.test_start} – {simulation.test_end}</td>
                  <td className="p-2">{t(job.status)}</td>
                  <td className="p-2">{job.progress}%</td>
                  <td className="p-2">{job.attempts}/3</td>
                  <td className="p-2"><Button type="button" size="sm" variant="outline" aria-pressed={job.id === selected?.id}
                    aria-label={t("Inspect backtest") + " · " + job.id} onClick={() => select(job)}>{t("Inspect backtest")}</Button></td>
                </tr>;
              })}</tbody>
            </table>
          </div> : <p role="status">{t("No matching backtests.")}</p>}
          <div role="group" aria-label={t("Backtest history pages")}>
            <TablePager page={page} total={Math.min(data.total, historyCap)} onPageChange={next => navigate(filter, next)} />
          </div>
          {data.total > historyCap && <p className="text-xs text-muted-foreground">{t("History is limited to the first 10,010 entries; narrow the status filter.")}</p>}
        </>}
      </CardContent>
    </Card>
  </>;
}

export function BacktestWorkspace(props: BacktestWorkspaceProps) {
  const { t } = useLocale();
  const focus = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    if (props.initialSource) focus.current?.focus();
  }, [props.initialSource]);
  return <section ref={focus} tabIndex={-1} aria-labelledby="backtesting-title"
    className="min-w-0 space-y-2 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
    <PageHeader id="backtesting-title" breadcrumb={t("FinIntel / Model Jobs")} title={t("Backtesting")}
      subtitle={t("Run a source-pinned historical simulation from a completed SMA job.")} />
    {isTauri() ? <ManagedBacktests {...props} /> :
      <p role="status" className="rounded-md border p-3">{t("Backtesting requires the managed FinIntel desktop.")}</p>}
  </section>;
}
