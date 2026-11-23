import { useRef, useState } from "react";
import { Tabs } from "@base-ui/react/tabs";
import { BacktestWorkspace } from "./BacktestWorkspace";
import type { SmaBacktestSelection } from "./BacktestForm";
import { ModelJobsPage } from "./ModelJobsPage";
import { AnalysisJobsHistory } from "./AnalysisJobsHistory";
import { tab, tabList } from "./AnalysisLibraryPanel";
import { useLocale } from "./localization";

export function ModelJobsWorkspace({ initialJobId }: { initialJobId?: string }) {
  const { t } = useLocale();
  const [backtestSource, setBacktestSource] = useState<SmaBacktestSelection>();
  const [view, setView] = useState(initialJobId ? "analysis" : "legacy");
  const workspaceGeneration = useRef(0);
  function changeView(next: string) {
    if (view === next) return;
    workspaceGeneration.current++;
    setView(next);
  }
  return <Tabs.Root className="min-w-0 space-y-2" value={view} onValueChange={changeView}>
    <Tabs.List className={tabList} aria-label={t("Model Jobs")}>
      <Tabs.Tab className={tab} value="legacy">{t("Legacy SMA Jobs")}</Tabs.Tab>
      <Tabs.Tab className={tab} value="analysis">{t("Analysis Library Jobs")}</Tabs.Tab>
      <Tabs.Tab className={tab} value="backtesting">{t("Backtesting")}</Tabs.Tab>
    </Tabs.List>
    <Tabs.Panel value="legacy">{view === "legacy" && <ModelJobsPage onCreateBacktest={source => { setBacktestSource(source); changeView("backtesting"); }} />}</Tabs.Panel>
    <Tabs.Panel value="analysis">{view === "analysis" && <AnalysisJobsHistory initialJobId={initialJobId} />}</Tabs.Panel>
    <Tabs.Panel value="backtesting">{view === "backtesting" && <BacktestWorkspace initialSource={backtestSource} workspaceGeneration={workspaceGeneration} />}</Tabs.Panel>
  </Tabs.Root>;
}
