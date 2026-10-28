import { PageHeader } from "./PageHeader";
import { currentLanguage, translate, type TranslationKey } from "./localization";
import type { AppLanguage } from "./workspace-preferences";
import { useCallback, useEffect, useState } from "react";
import { Activity, BarChart3, BrainCircuit, BriefcaseBusiness, CircleCheck, CircleOff, Clock3, Database, Download, History, ShieldCheck, RefreshCw, Settings, ListFilter } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { fetchBackendHealth } from "./api";
import { MarketExplorer } from "./MarketExplorer";
import { PreferencesPage } from "./PreferencesPage";
import { ActionHistoryPage } from "./ActionHistoryPage";
import { DataValidationPage } from "./DataValidationPage";
import { IndexMembershipPage } from "./IndexMembershipPage";
import { DataAcquisitionPage, type MarketSelection } from "./DataAcquisitionPage";
import type { BackendHealth } from "./health";

type Screen = "overview" | "market" | "intelligence" | "data-acquisition" | "data-validation" | "index-membership" | "action-history" | "portfolio" | "settings";
const groups = [
  { heading: "Workspace", items: [
    { id: "overview", label: "Overview", icon: Activity },
    { id: "market", label: "Market", icon: BarChart3 },
    { id: "intelligence", label: "Intelligence", icon: BrainCircuit },
  ] },
  { heading: "Data & Operations", items: [
    { id: "data-acquisition", label: "Data Acquisition", icon: Database },
    { id: "data-validation", label: "Data Validation", icon: ShieldCheck },
    { id: "index-membership", label: "Index Constituents", icon: ListFilter },
    { id: "action-history", label: "Action History", icon: History },
  ] },
  { heading: "Workspace & Preferences", items: [
    { id: "portfolio", label: "Portfolio", icon: BriefcaseBusiness },
    { id: "settings", label: "Preferences", icon: Settings },
  ] },
] as const;
const allSections = [...groups[0].items, ...groups[1].items, ...groups[2].items];
const placeholders: Partial<Record<Screen, { title: string; description: string }>> = {
  intelligence: { title: "Intelligence", description: "Proprietary models, local AI, and market trend analysis will appear here." },
  portfolio: { title: "Portfolio", description: "Holdings, performance, allocation, and risk exposure will appear here." },
};
type Connection =
  | { kind: "loading" }
  | { kind: "connected"; health: BackendHealth }
  | { kind: "disconnected"; message: string };

export default function App() {
  const [screen, setScreen] = useState<Screen>("overview");
  const [language, setLanguage] = useState<AppLanguage>(currentLanguage);
  const t = (key: string) => translate(language, key);
  const [marketSelection, setMarketSelection] = useState<MarketSelection | null>(null);
  const [connection, setConnection] = useState<Connection>({ kind: "loading" });

  const openMarkets = (selection?: MarketSelection) => {
    setMarketSelection(selection ?? null);
    setScreen("market");
  };

  const checkHealth = useCallback(async (signal?: { cancelled: boolean }, background = false) => {
    if (!background) setConnection({ kind: "loading" });
    try {
      const health = await fetchBackendHealth();
      if (!signal?.cancelled) setConnection({ kind: "connected", health });
    } catch (error: unknown) {
      if (!signal?.cancelled) {
        setConnection({ kind: "disconnected", message: error instanceof Error ? error.message : String(error) });
      }
    }
  }, []);

  useEffect(() => {
    const signal = { cancelled: false };
    void checkHealth(signal);
    const timer = window.setInterval(() => { void checkHealth(signal, true); }, 5000);
    return () => { signal.cancelled = true; window.clearInterval(timer); };
  }, [checkHealth]);

  return (
    <div className="min-h-screen bg-background text-foreground lg:grid lg:grid-cols-[208px_minmax(0,1fr)]">
      <aside className="hidden min-h-screen flex-col border-r border-border bg-muted px-3 py-6 lg:flex" aria-label={t("Main navigation")}>
        <div className="mx-3 mb-12 grid h-11 w-12 place-items-center rounded-lg border border-[#568ad1] text-xl font-extrabold tracking-tight" aria-hidden="true">FI</div>
        <nav aria-label={t("Sections")} className="flex flex-col gap-1 text-sm">
          {groups.map((group) => (
            <div key={t(group.heading)} className="mb-5 flex flex-col gap-1">
              <span className="mb-2 px-3 text-sm font-bold tracking-[0.16em] text-[#146B82] [html[data-theme=dark]_&]:text-[#73D5E8] uppercase">{t(group.heading)}</span>
              {group.items.map(({ id, label, icon: Icon }) => (
                <button key={id} type="button" aria-current={screen === id ? "page" : undefined} onClick={() => id === "market" ? openMarkets() : setScreen(id)} className={(screen === id ? "bg-secondary font-semibold text-secondary-foreground " : "text-muted-foreground hover:bg-accent hover:text-foreground ") + "flex items-center gap-2 rounded-md px-3 py-3 text-left focus-visible:ring-2 focus-visible:ring-ring"}>
                  <Icon aria-hidden="true" className="size-4" /> {t(label)}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <p className="mt-auto px-2 pt-8 text-[10px] font-medium tracking-widest text-[#62758d]">{t("PRIVATE · LOCAL FIRST")}</p>
      </aside>

      <main className="w-full min-w-0 px-3 py-4 sm:px-4 lg:py-5">
        <nav aria-label={t("Mobile sections")} className="mb-5 flex gap-2 overflow-x-auto pb-2 lg:hidden">
          {allSections.map(({ id, label, icon: Icon }) => (
            <Button key={id} type="button" className="shrink-0" variant={screen === id ? "secondary" : "ghost"} onClick={() => id === "market" ? openMarkets() : setScreen(id)} aria-current={screen === id ? "page" : undefined}>
              <Icon aria-hidden="true" /> {t(label)}
            </Button>
          ))}
        </nav>
        {screen === "market" ? (
          <MarketExplorer initialSelection={marketSelection} onOpenAcquisition={() => setScreen("data-acquisition")} />
        ) : screen === "data-acquisition" ? (
          <DataAcquisitionPage onViewMarket={openMarkets} />
        ) : screen === "data-validation" ? (
          <DataValidationPage />
        ) : screen === "index-membership" ? (
          <IndexMembershipPage />
        ) : screen === "action-history" ? (
          <ActionHistoryPage onViewMarket={openMarkets} />
        ) : screen === "settings" ? (
          <PreferencesPage
            backend={connection.kind === "connected" ? { kind: "connected", version: connection.health.version } : { kind: connection.kind }}
            onRecheckBackend={() => { void checkHealth(); }}
            onOpenAcquisition={() => setScreen("data-acquisition")}
            onLanguageChange={setLanguage}
          />
        ) : screen !== "overview" ? (
          <section aria-labelledby="placeholder-title">
            <PageHeader id="placeholder-title" breadcrumb={`FinIntel / ${t(placeholders[screen]!.title as TranslationKey)}`} title={t(placeholders[screen]!.title as TranslationKey)} subtitle={t(placeholders[screen]!.description as TranslationKey)} actions={<Badge variant="outline">{t("Not implemented")}</Badge>} />
            <Card className="mt-2 max-w-[820px]">
              <CardHeader><CardTitle>{t("Coming in a future milestone")}</CardTitle><CardDescription>{t(placeholders[screen]!.description as TranslationKey)}</CardDescription></CardHeader>
              <CardContent><p className="text-sm text-muted-foreground">{t("This is a navigation placeholder. No live data or actions are available on this page yet.")}</p></CardContent>
            </Card>
          </section>
        ) : (
          <section aria-labelledby="overview-title">
            <PageHeader id="overview-title" breadcrumb={t("FinIntel / Overview")} title={t("Private Financial Intelligence")} subtitle={t("Explore locally stored market data and build your private research workspace.")} actions={<Badge variant="outline">{t("M1 · Local workstation")}</Badge>} />
            <section className="w-full min-w-0 py-6 lg:py-8">
              <p className="text-[10px] font-bold tracking-[0.16em] text-[#8aa5c3] uppercase">{t("Historical research")}</p>
              <h2 className="mt-3 text-3xl font-semibold tracking-tight sm:text-4xl">{t("Your local market workspace.")}</h2>
              <p className="mt-4 w-full whitespace-normal text-sm leading-7 text-muted-foreground">
                {t("Use Data Acquisition to fetch prices and review completed import actions. Open Markets to explore stored OHLCV charts, historical tables, and instrument-level source provenance.")}
              </p>
              <Button type="button" className="mt-5" onClick={() => openMarkets()}><BarChart3 aria-hidden="true" /> {t("Open Market Data Explorer")}</Button>
              <Button type="button" variant="secondary" className="mt-5 ml-2" onClick={() => setScreen("data-acquisition")}><Download aria-hidden="true" /> {t("Open Data Acquisition")}</Button>
            </section>
            <div className="grid gap-2 xl:grid-cols-2">
              <Card aria-labelledby="backend-title">
                <CardHeader>
                  <h3 id="backend-title"><CardTitle>{t("Python backend")}</CardTitle></h3>
                  <CardDescription>{t("Connection through native Tauri IPC or local Vite proxy.")}</CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div role="status" aria-live="polite" className="flex items-center gap-2">
                    {connection.kind === "loading" ? <><Clock3 aria-hidden="true" className="size-4 text-warning" />{t("Checking…")}</>
                      : connection.kind === "connected" ? <><CircleCheck aria-hidden="true" className="size-4 text-positive" />{t("Connected")}</>
                      : <><CircleOff aria-hidden="true" className="size-4 text-destructive" />{t("Unavailable")}</>}
                  </div>
                  {connection.kind === "loading"
                    ? <Skeleton aria-label={t("Loading backend connection")} className="h-4 w-48" />
                    : <p className="text-xs text-muted-foreground">{connection.kind === "connected" ? t("Backend") + " · finintel-backend v" + connection.health.version : connection.message}</p>}
                  <Button type="button" variant="secondary" size="sm" disabled={connection.kind === "loading"} onClick={() => void checkHealth()}><RefreshCw aria-hidden="true" /> {t("Recheck connection")}</Button>
                </CardContent>
              </Card>
              <Card aria-labelledby="progress-title">
                <CardHeader><h3 id="progress-title"><CardTitle>{t("Research foundations")}</CardTitle></h3>
                  <CardDescription>{t("Local data first, quant intelligence later.")}</CardDescription>
                </CardHeader>
                <CardContent className="space-y-3 text-sm">
                  <p className="flex justify-between gap-2">{t("Historical data acquisition")} <Badge variant="success">{t("Implemented")}</Badge></p>
                  <p className="flex justify-between gap-2">{t("SQLite persistence")} <Badge variant="success">{t("Implemented")}</Badge></p>
                  <p className="flex justify-between gap-2">{t("Market Data Explorer")} <Badge variant="outline">{t("In review")}</Badge></p>
                </CardContent>
              </Card>
            </div>
            <footer className="mt-9 text-xs text-muted-foreground">{t("FinIntel · Local-first · No synthetic market values displayed")}</footer>
          </section>
        )}
      </main>
    </div>
  );
}
