import { useCallback, useEffect, useState } from "react";
import {
  Activity,
  BarChart3,
  CircleCheck,
  CircleOff,
  Clock3,
  Database,
  RefreshCw,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { fetchBackendHealth } from "./api";
import type { BackendHealth } from "./health";

type Connection =
  | { kind: "loading" }
  | { kind: "connected"; health: BackendHealth }
  | { kind: "disconnected"; message: string };

const pending = [
  { number: "02", label: "Public history acquisition", icon: Database },
  { number: "03", label: "SQLite persistence and APIs", icon: Activity },
  { number: "04", label: "Market charts and data grids", icon: BarChart3 },
];

export default function App() {
  const [connection, setConnection] = useState<Connection>({ kind: "loading" });

  const checkHealth = useCallback(async (signal?: { cancelled: boolean }) => {
    setConnection({ kind: "loading" });
    try {
      const health = await fetchBackendHealth();
      if (!signal?.cancelled) setConnection({ kind: "connected", health });
    } catch (error: unknown) {
      if (!signal?.cancelled) {
        setConnection({
          kind: "disconnected",
          message: error instanceof Error ? error.message : "Backend unavailable",
        });
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
          <a href="#overview" aria-current="page" className="flex items-center gap-2 rounded-md bg-secondary px-3 py-3 font-semibold text-secondary-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
            <Activity aria-hidden="true" className="size-4" /> Overview
          </a>
          <span aria-disabled="true" className="flex items-center justify-between rounded-md px-3 py-3 text-muted-foreground/60">Markets <small>PR #4</small></span>
          <span aria-disabled="true" className="flex items-center justify-between rounded-md px-3 py-3 text-muted-foreground/60">Research <small>Later</small></span>
          <span aria-disabled="true" className="flex items-center justify-between rounded-md px-3 py-3 text-muted-foreground/60">AI Desk <small>Later</small></span>
        </nav>
        <p className="mt-auto px-2 pt-8 text-[10px] font-medium tracking-widest text-[#62758d]">PRIVATE · LOCAL FIRST</p>
      </aside>

      <main id="overview" className="mx-auto w-full max-w-[1640px] min-w-0 px-4 py-6 sm:px-6 lg:px-9 lg:py-8">
        <header className="flex flex-wrap items-center justify-between gap-4 border-b border-border pb-5">
          <div>
            <p className="text-[10px] font-bold tracking-[0.16em] text-[#8aa5c3] uppercase">FinIntel / Overview</p>
            <h1 className="mt-2 text-2xl font-semibold tracking-tight">Private Financial Intelligence</h1>
          </div>
          <Badge variant="outline" className="text-[#84a6cd]">Bootstrap v0.1</Badge>
        </header>

        <section aria-labelledby="intro-title" className="max-w-[820px] py-9 lg:py-12">
          <p className="text-[10px] font-bold tracking-[0.16em] text-[#8aa5c3] uppercase">Foundation / PR #1</p>
          <h2 id="intro-title" className="mt-3 text-3xl font-semibold tracking-tight sm:text-4xl">Ready for market intelligence.</h2>
          <p className="mt-4 max-w-[68ch] text-sm leading-7 text-muted-foreground">
            The desktop shell and local Python service are the foundation.
            Public market data, persistence, and charts arrive in upcoming pull requests.
          </p>
        </section>

        <div className="grid gap-4 xl:grid-cols-2">
          <Card className="min-h-[290px]" aria-labelledby="backend-title">
            <CardHeader>
              <h3 id="backend-title"><CardTitle>Python backend</CardTitle></h3>
              <CardAction><Badge variant="outline">Local API</Badge></CardAction>
              <CardDescription className="col-span-2 mt-2">
                Health check via typed Tauri command or Vite development proxy.
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-1 flex-col gap-4">
              <div role="status" aria-live="polite" aria-atomic="true" className="flex items-center gap-2">
                {connection.kind === "loading" ? (
                  <><Clock3 aria-hidden="true" className="size-4 text-warning" /><span className="font-semibold">Checking…</span></>
                ) : connection.kind === "connected" ? (
                  <><CircleCheck aria-hidden="true" className="size-4 text-positive" /><span className="font-semibold">Connected</span></>
                ) : (
                  <><CircleOff aria-hidden="true" className="size-4 text-destructive" /><span className="font-semibold">Unavailable</span></>
                )}
              </div>
              {connection.kind === "loading" ? (
                <Skeleton aria-label="Loading backend connection" className="h-4 w-48" />
              ) : (
                <p className="min-h-9 text-xs leading-6 text-muted-foreground">
                  {connection.kind === "connected"
                    ? `finintel-backend v${connection.health.version}`
                    : `Start the Python service on 127.0.0.1:8765. ${connection.message}`}
                </p>
              )}
              <div className="mt-auto">
                <Button type="button" variant="secondary" size="sm" onClick={() => void checkHealth()} disabled={connection.kind === "loading"}>
                  <RefreshCw aria-hidden="true" />
                  Recheck connection
                </Button>
              </div>
            </CardContent>
          </Card>

          <Card className="min-h-[290px]" aria-labelledby="roadmap-title">
            <CardHeader>
              <h3 id="roadmap-title"><CardTitle>Implementation roadmap</CardTitle></h3>
              <CardAction><Badge variant="outline">M1</Badge></CardAction>
              <CardDescription className="col-span-2 mt-2">Data first, visualization second, intelligence later.</CardDescription>
            </CardHeader>
            <CardContent>
              <ol className="grid gap-4">
                {pending.map((item) => {
                  const Icon = item.icon;
                  return (
                    <li key={item.number} className="flex items-center gap-3 text-sm">
                      <span className="inline-flex min-w-8 items-center justify-center rounded-md bg-secondary px-2 py-1 font-semibold text-secondary-foreground">{item.number}</span>
                      <Icon aria-hidden="true" className="size-4 shrink-0 text-[#8cbafb]" />
                      <span>{item.label}</span>
                    </li>
                  );
                })}
              </ol>
            </CardContent>
          </Card>
        </div>

        <footer className="mt-9 text-xs text-[#72869d]">FinIntel · No cloud backend · No synthetic market data</footer>
      </main>
    </div>
  );
}
