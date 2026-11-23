import { useState } from "react";
import { Tabs } from "@base-ui/react/tabs";
import { ModelJobsPage } from "./ModelJobsPage";
import { AnalysisJobsHistory } from "./AnalysisJobsHistory";
import { tab, tabList } from "./AnalysisLibraryPanel";
import { useLocale } from "./localization";

export function ModelJobsWorkspace({ initialJobId }: { initialJobId?: string }) {
  const { t } = useLocale();
  const [view, setView] = useState(initialJobId ? "analysis" : "legacy");
  return <Tabs.Root className="min-w-0 space-y-2" value={view} onValueChange={setView}>
    <Tabs.List className={tabList} aria-label={t("Model Jobs")}>
      <Tabs.Tab className={tab} value="legacy">{t("Legacy SMA Jobs")}</Tabs.Tab>
      <Tabs.Tab className={tab} value="analysis">{t("Analysis Library Jobs")}</Tabs.Tab>
    </Tabs.List>
    <Tabs.Panel value="legacy">{view === "legacy" && <ModelJobsPage />}</Tabs.Panel>
    <Tabs.Panel value="analysis">{view === "analysis" && <AnalysisJobsHistory initialJobId={initialJobId} />}</Tabs.Panel>
  </Tabs.Root>;
}
