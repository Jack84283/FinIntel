import { useState } from "react";
import { Activity, ArrowUpRight, Check, Database, HardDrive, LockKeyhole, Monitor, Moon, RefreshCw, ShieldCheck, Sun } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { readWorkspacePreferences, saveWorkspacePreferences, type ColorTheme, type WorkspacePreferences } from "./workspace-preferences";

type BackendState =
  | { kind: "loading" }
  | { kind: "connected"; version: string }
  | { kind: "disconnected" };

interface PreferencesPageProps {
  backend: BackendState;
  onRecheckBackend: () => void;
  onOpenAcquisition: () => void;
}

function SettingRow({ title, detail, children }: {
  title: string;
  detail: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-3 py-3 first:pt-0 last:pb-0 sm:flex-row sm:items-center sm:justify-between sm:gap-8">
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium leading-6">{title}</p>
        <p className="mt-1 break-words text-xs leading-5 text-muted-foreground">{detail}</p>
      </div>
      <div className="flex min-w-0 shrink-0 flex-wrap items-center gap-2 sm:justify-end">{children}</div>
    </div>
  );
}

function ThemeOption({ theme, selected, onSelect, label, icon: Icon }: {
  theme: ColorTheme;
  selected: boolean;
  onSelect: (theme: ColorTheme) => void;
  label: string;
  icon: typeof Moon;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={() => onSelect(theme)}
      className={`flex min-w-0 items-center justify-between gap-3 rounded-lg border px-4 py-3 text-left text-sm transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring ${selected ? "border-ring bg-secondary text-secondary-foreground" : "border-border bg-background text-foreground hover:border-input hover:bg-accent"}`}
    >
      <span className="flex min-w-0 items-center gap-3"><Icon aria-hidden="true" className="size-5 shrink-0" /><span className="truncate">{label}</span></span>
      {selected && <Check aria-hidden="true" className="size-4 shrink-0" />}
    </button>
  );
}

export function PreferencesPage({ backend, onRecheckBackend, onOpenAcquisition }: PreferencesPageProps) {
  const [preferences, setPreferences] = useState<WorkspacePreferences>(readWorkspacePreferences);

  const updatePreferences = (changes: Partial<WorkspacePreferences>) => {
    const next = { ...preferences, ...changes };
    setPreferences(next);
    saveWorkspacePreferences(next);
  };

  return (
    <section aria-labelledby="preferences-title" className="min-w-0 space-y-4 pb-6">
      <header className="flex min-w-0 flex-wrap items-start justify-between gap-4 border-b border-border pb-4">
        <div className="min-w-0">
          <p className="text-[10px] font-bold uppercase tracking-[0.16em] text-[#8aa5c3]">FinIntel / Preferences</p>
          <h1 id="preferences-title" className="mt-2 text-2xl font-semibold tracking-tight">Preferences</h1>
          <p className="mt-2 max-w-[64ch] text-sm leading-6 text-muted-foreground">
            Personalize the workspace and review local data settings. Changes to appearance stay on this device.
          </p>
        </div>
        <Badge variant="outline" className="shrink-0">Device-local</Badge>
      </header>

      <div className="grid min-w-0 items-start gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(280px,340px)]">
        <div className="flex min-w-0 flex-col gap-4">
          <Card className="min-w-0 gap-3 py-4">
            <CardHeader className="min-w-0 gap-y-2">
              <CardTitle>Appearance</CardTitle>
              <CardDescription>Keep the interface readable and comfortable at any window size.</CardDescription>
            </CardHeader>
            <CardContent className="min-w-0">
              <div className="border-b border-border pb-6">
                <p className="mb-1 text-sm font-medium">Color theme</p>
                <p className="mb-4 text-xs leading-5 text-muted-foreground">Switch between high-contrast dark and light surfaces.</p>
                <div className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2">
                  <ThemeOption theme="dark" label="Dark" icon={Moon} selected={preferences.theme === "dark"} onSelect={(theme) => updatePreferences({ theme })} />
                  <ThemeOption theme="light" label="Light" icon={Sun} selected={preferences.theme === "light"} onSelect={(theme) => updatePreferences({ theme })} />
                </div>
              </div>
              <SettingRow title="Reduce motion" detail="Minimize interface animations and transitions. Your operating system's reduced-motion preference is also respected.">
                <Button type="button" size="sm" variant={preferences.reduceMotion ? "secondary" : "outline"} aria-pressed={preferences.reduceMotion} onClick={() => updatePreferences({ reduceMotion: !preferences.reduceMotion })}>
                  {preferences.reduceMotion ? "Enabled" : "Disabled"}
                </Button>
              </SettingRow>
            </CardContent>
          </Card>

          <Card className="min-w-0">
            <CardHeader className="min-w-0 gap-y-2">
              <CardTitle>Historical market data</CardTitle>
              <CardDescription>Review acquisition defaults without interrupting a running backfill.</CardDescription>
            </CardHeader>
            <CardContent className="min-w-0 divide-y divide-border">
              <SettingRow title="Background historical window" detail="The local backfill script currently defaults to ten years of available daily data.">
                <Badge variant="secondary">10 years</Badge>
              </SettingRow>
              <SettingRow title="Storage format" detail="Daily OHLCV bars are validated and stored in the local SQLite database.">
                <Badge variant="outline">SQLite</Badge>
              </SettingRow>
              <SettingRow title="Acquisition controls" detail="Select instruments and data providers, or inspect an active import in Data Acquisition.">
                <Button type="button" variant="secondary" size="sm" onClick={onOpenAcquisition}>
                  Open Data Acquisition <ArrowUpRight aria-hidden="true" />
                </Button>
              </SettingRow>
            </CardContent>
          </Card>
        </div>

        <aside className="flex min-w-0 flex-col gap-6" aria-label="Workspace information">
          <Card className="min-w-0">
            <CardHeader className="flex min-w-0 flex-col items-start gap-2">
              <div className="flex min-w-0 items-center gap-2"><Activity aria-hidden="true" className="size-4 text-muted-foreground" /><CardTitle className="leading-5">Local backend</CardTitle></div>
              <CardDescription>Connection to FinIntel's local Python service.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <span aria-hidden="true" className={`size-2 shrink-0 rounded-full ${backend.kind === "connected" ? "bg-positive" : backend.kind === "loading" ? "bg-warning" : "bg-destructive"}`} />
                <span role="status" aria-live="polite" className="min-w-0 break-words text-sm font-medium">
                  {backend.kind === "connected" ? `Connected · v${backend.version}` : backend.kind === "loading" ? "Checking connection…" : "Not connected"}
                </span>
              </div>
              <Button type="button" variant="outline" size="sm" disabled={backend.kind === "loading"} onClick={onRecheckBackend}>
                <RefreshCw aria-hidden="true" /> Recheck
              </Button>
            </CardContent>
          </Card>
          <Card className="min-w-0">
            <CardHeader className="flex min-w-0 flex-col items-start gap-2">
              <div className="flex min-w-0 items-center gap-2"><LockKeyhole aria-hidden="true" className="size-4 text-muted-foreground" /><CardTitle className="leading-5">Privacy & storage</CardTitle></div>
              <CardDescription>Built for a private, local-first financial workspace.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3 text-sm>
              <div className="flex min-w-0 gap-3"><HardDrive aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-muted-foreground" /><p className="min-w-0 leading-6">Historical prices and research data are stored locally.</p></div>
              <div className="flex min-w-0 gap-3"><ShieldCheck aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-muted-foreground" /><p className="min-w-0 leading-6">Provider credentials remain in your backend environment, not in these preferences.</p></div>
              <div className="flex min-w-0 gap-3"><Monitor aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-muted-foreground" /><p className="min-w-0 leading-6">Appearance preferences are saved only on this device.</p></div>
              <div className="flex min-w-0 gap-3"><Database aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-muted-foreground" /><p className="min-w-0 leading-6">Changing the theme or motion preference does not modify market history.</p></div>
            </CardContent>
          </Card>
        </aside>
      </div>
    </section>
  );
}
