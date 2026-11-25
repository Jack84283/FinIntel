import { useEffect, useRef } from "react";
import { Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useLocale } from "./localization";
import { marketTabKey, marketTabSymbol, type MarketWorkspace } from "./market-workspace";

export function MarketTabs({ workspace, panelId, describe, onActivate, onClose, onAdd, onCompare }: {
  workspace: MarketWorkspace;
  panelId: string;
  describe: (tab: MarketWorkspace["tabs"][number]) => string;
  onActivate: (key: string) => void;
  onClose: (key: string) => void;
  onAdd: () => void;
  onCompare: () => void;
}) {
  const { t } = useLocale();
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => {
    list.current?.querySelector('[aria-selected="true"]')?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  }, [workspace.active, workspace.comparing]);

  function focusTab(key: string) {
    list.current?.querySelector<HTMLButtonElement>(`[data-market-tab="${key}"]`)?.focus();
  }

  const order = ["comparison", ...workspace.tabs.map(tab => marketTabKey(tab.selection))];
  function navigate(event: React.KeyboardEvent, index: number) {
    const last = order.length - 1;
    const next = event.key === "ArrowRight" ? (index + 1) % order.length
      : event.key === "ArrowLeft" ? (index + last) % order.length
      : event.key === "Home" ? 0 : event.key === "End" ? last : -1;
    if (next < 0) return;
    event.preventDefault();
    focusTab(order[next]);
    if (next === 0) onCompare();
    else onActivate(order[next]);
  }

  return <div className="flex min-w-0 items-center gap-1 border-b border-border">
    <div ref={list} role="tablist" aria-label={t("Market instrument tabs")} className="flex min-w-0 gap-1 overflow-x-auto px-1 py-1">
      <Button type="button" variant={workspace.comparing ? "secondary" : "ghost"} size="sm"
        role="tab" id={`${panelId}-comparison`} data-market-tab="comparison" aria-controls={`${panelId}-comparison-panel`}
        aria-selected={workspace.comparing === true} tabIndex={workspace.comparing ? 0 : -1}
        className="shrink-0" onClick={onCompare} onKeyDown={event => navigate(event, 0)}>{t("Comparison")}</Button>
      {workspace.tabs.map((tab, index) => {
        const key = marketTabKey(tab.selection);
        const symbol = marketTabSymbol(tab.selection);
        const identity = describe(tab);
        const active = !workspace.comparing && workspace.active === key;
        const sharedSymbol = workspace.tabs.some(other => other !== tab && marketTabSymbol(other.selection) === symbol);
        const close = () => {
          if (workspace.tabs.length <= 1) return;
          const adjacent = workspace.tabs[index + 1] ?? workspace.tabs[index - 1];
          focusTab(workspace.comparing ? "comparison" : active ? marketTabKey(adjacent.selection) : workspace.active);
          onClose(key);
        };
        return <div key={key} role="presentation" className={`flex shrink-0 items-center rounded-md ${active ? "bg-secondary text-secondary-foreground" : "text-muted-foreground hover:bg-accent"}`}>
          <Button type="button" variant="ghost" size="sm" role="tab" id={`${panelId}-${key}`}
            data-market-tab={key} aria-controls={panelId} aria-selected={active} aria-label={identity} title={identity}
            tabIndex={active ? 0 : -1} className={active ? "font-semibold" : ""}
            onClick={() => onActivate(key)} onKeyDown={event => {
              if (event.key === "Delete") { event.preventDefault(); close(); return; }
              navigate(event, index + 1);
            }}
          >
            {symbol}{sharedSymbol && tab.selection.kind === "equity" && <span className="text-xs">{describe(tab).split(" · ").at(-1)}</span>}
          </Button>
          {workspace.tabs.length > 1 && <Button type="button" variant="ghost" size="icon-sm"
            aria-label={`${t("Close tab")} · ${identity}`} title={t("Close tab")} onClick={close}>
            <X aria-hidden="true" />
          </Button>}
        </div>;
      })}
    </div>
    <Button type="button" variant="ghost" size="icon-sm" className="shrink-0"
      aria-label={t(workspace.comparing ? "Select comparison instruments" : "Add instrument tab")} title={t(workspace.comparing ? "Select comparison instruments" : "Add instrument tab")} onClick={onAdd}><Plus aria-hidden="true" /></Button>
  </div>;
}
