import { useCallback, useEffect, useState } from "react";
import { Activity, BarChart3, CircleCheck, CircleOff, Clock3, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { fetchBackendHealth } from "./api";
import { MarketExplorer } from "./MarketExplorer";
import type { BackendHealth } from "./health";

type Screen = "overview" | "markets";
type Connection =
  | { kind: "loading" }
  | { kind: "connected"; health: BackendHealth }
  | { kind: "disconnected"; message: string };

export default function App() {
  const [screen, setScreen] = useState<Screen>("overview");
  const [connection, setConnection] = useState<Connection>({ kind: "loading" });

  const checkHealth = useCallback(async (signal?: { cancelled: boolean }) => {
    setConnection({ kind: "loading" });
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
    return () => { signal.cancelled = true; };
  }, [checkHealth]);

  return (
    <div className="min-h-screen bg-background text-foreground lg:grid lg:grid-cols-[208px_minmax(0,1fr)]">
      <aside className="hidden min-h-screen flex-col border-r border-border bg-muted px-3 py-6 lg:flex" aria-label="Main navigation">
        <div className="mx-3 mb-12 grid h-11 w-12 place-items-center rounded-lg border border-[#568ad1] text-xl font-extrabold tracking-tight" aria-hidden="true">FI</div>
        <span className="mb-3 px-3 text-[10px] font-bold tracking-[0.16em] text-[#8aa5c3] uppercase">Workspace</span>
        <nav aria-label="Sections" className="flex flex-col gap-1 text-sm">
          <button type="button" aria-current={screen === "overview" ? "page" : undefined} onClick={() => setScreen("overview")} className={(screen === "overview" ? "bg-secondary font-semibold text-secondary-foreground " : "text-muted-foreground hover:bg-accent hover:text-foreground ") + "flex items-center gap-2 rounded-md px-3 py-3 text-left focus-visible:ring-2 focus-visible:ring-ring"}>
            <Activity aria-hidden="true" className="size-4" /> Overview
          </button>
          <button type="button" aria-current={screen === "markets" ? "page" : undefined} onClick={() => setScreen("markets")} className={(screen === "markets" ? "bg-secondary font-semibold text-secondary-foreground " : "text-muted-foreground hover:bg-accent hover:text-foreground ") + "flex items-center gap-2 rounded-md px-3 py-3 text-left focus-visible:ring-2 focus-visible:ring-ring"}>
            <BarChart3 aria-hidden="true" className="size-4" /> Markets
          </button>
          <span aria-disabled="true" className="rounded-md px-3 py-3 text-muted-foreground/60">Research <small>Later</small></span>
          <span aria-disabled="true" className="rounded-md px-3 py-3 text-muted-foreground/60">AI Desk <small>Later</small></span>
        </nav>
        <p className="mt-auto px-2 pt-8 text-[10px] font-medium tracking-widest text-[#62758d]">PRIVATE · LOCAL FIRST</p>
      </aside>

      <main className="mx-auto w-full max-w-[1640px] min-w-0 px-4 py-6 sm:px-6 lg:px-9 lg:py-8">
        <nav aria-label="Mobile sections" className="mb-5 flex gap-2 lg:hidden">
          <Button type="button" variant={screen === "overview" ? "secondary" : "ghost"} onClick={() => setScreen("overview")} aria-current={screen === "overview" ? "page" : undefined}><Activity aria-hidden="true" /> Overview</Button>
          <Button type="button" variant={screen === "markets" ? "secondary" : "ghost"} onClick={() => setScreen("markets")} aria-current={screen === "markets" ? "page" : undefined}><BarChart3 aria-hidden="true" /> Markets</Button>
        </nav>
        {screen === "markets" ? <MarketExplorer /> : (
          <section aria-labelledby="overview-title">
            <header className="flex flex-wrap items-center justify-between gap-4 border-b border-border pb-5">
              <div>
                <p className="text-[10px] font-bold tracking-[0.16em] text-[#8aa5c3] uppercase">FinIntel / Overview</p>
                <h1 id="overview-title" className="mt-2 text-2xl font-semibold tracking-tight">Private Financial Intelligence</h1>
              </div>
              <Badge variant="outline">M1 · Local workstation</Badge>
            </header>
            <section className="max-w-[820px] py-9 lg:py-12">
              <p className="text-[10px] font-bold tracking-[0.16em] text-[#8aa5c3] uppercase">Historical research</p>
              <h2 className="mt-3 text-3xl font-semibold tracking-tight sm:text-4xl">Your local market workspace.</h2>
              <p className="mt-4 max-w-[68ch] text-sm leading-7 text-muted-foreground">
                Historical stock prices are acquired and stored locally. Open Markets to view the
                real OHLCV records already imported into SQLite, along with their source history.
              </p>
              <Button type="button" className="mt-5" onClick={() => setScreen("markets")}><BarChart3 aria-hidden="true" /> Open Market Data Explorer</Button>
            </section>
            <div className="grid gap-4 xl:grid-cols-2">
              <Card aria-labelledby="backend-title">
                <CardHeader>
                  <h3 id="backend-title"><CardTitle>Python backend</CardTitle></h3>
                  <CardDescription>Connection through native Tauri IPC or local Vite proxy.</CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div role="status" aria-live="polite" className="flex items-center gap-2">
                    {connection.kind === "loading" ? <><Clock3 aria-hidden="true" className="size-4 text-warning" />Checking…</>
                      : connection.kind === "connected" ? <><CircleCheck aria-hidden="true" className="size-4 text-positive" />Connected</>
                      : <><CircleOff aria-hidden="true" className="size-4 text-destructive" />Unavailable</>}
                  </div>
                  {connection.kind === "loading"
                    ? <Skeleton aria-label="Loading backend connection" className="h-4 w-48" />
                    : <p className="text-xs text-muted-foreground">{connection.kind === "connected" ? "finintel-backend v" + connection.health.version : connection.message}</p>}
                  <Button type="button" variant="secondary" size="sm" disabled={connection.kind === "loading"} onClick={() => void checkHealth()}><RefreshCw aria-hidden="true" /> Recheck connection</Button>
                </CardContent>
              </Card>
              <Card aria-labelledby="progress-title">
                <CardHeader><h3 id="progress-title"><CardTitle>Research foundations</CardTitle></h3>
                  <CardDescription>Local data first, quant intelligence later.</CardDescription>
                </CardHeader>
                <CardContent className="space-y-3 text-sm">
                  <p className="flex justify-between gap-2">Historical data acquisition <Badge variant="success">Implemented</Badge></p>
                  <p className="flex justify-between gap-2">SQLite persistence <Badge variant="success">Implemented</Badge></p>
                  <p className="flex justify-between gap-2">Market Data Explorer <Badge variant="outline">In review</Badge></p>
                </CardContent>
              </Card>
            </div>
            <footer className="mt-9 text-xs text-muted-foreground">FinIntel · Local-first · No synthetic market values displayed</footer>
          </section>
        )}
      </main>
    </div>
  );
}
