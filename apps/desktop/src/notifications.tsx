import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { AlertTriangle, CheckCircle2, Info, X, XCircle } from "lucide-react";
import { useLocale } from "./localization";

type Severity = "success" | "info" | "warning" | "error";
type Notice = { id: number; message: string; severity: Severity };
type Notify = (message: string, severity?: Severity) => void;
const NotificationContext = createContext<Notify>(() => {});
export const useNotify = () => useContext(NotificationContext);
const durations: Record<Severity, number> = { success: 4000, info: 5000, warning: 8000, error: 10000 };
const icons = { success: CheckCircle2, info: Info, warning: AlertTriangle, error: XCircle };

export function NotificationProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Notice[]>([]);
  const nextId = useRef(0);
  const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>());
  const remaining = useRef(new Map<number, number>());
  const started = useRef(new Map<number, number>());
  const { t } = useLocale();

  const dismiss = useCallback((id: number) => {
    const timer = timers.current.get(id);
    if (timer !== undefined) clearTimeout(timer);
    timers.current.delete(id);
    remaining.current.delete(id);
    started.current.delete(id);
    setItems(previous => previous.filter(item => item.id !== id));
  }, []);

  const start = useCallback((id: number, ms: number) => {
    started.current.set(id, Date.now());
    timers.current.set(id, setTimeout(() => dismiss(id), ms));
  }, [dismiss]);
  const pause = (id: number) => {
    const timer = timers.current.get(id);
    if (timer === undefined) return;
    clearTimeout(timer);
    timers.current.delete(id);
    remaining.current.set(id, Math.max(0, (remaining.current.get(id) ?? 0) - (Date.now() - (started.current.get(id) ?? Date.now()))));
  };
  const resume = (id: number) => {
    if (timers.current.has(id)) return;
    start(id, remaining.current.get(id) ?? 1000);
  };
  const notify = useCallback<Notify>((message, severity = "info") => {
    const id = ++nextId.current;
    remaining.current.set(id, durations[severity]);
    setItems(previous => [...previous.filter(item => !(item.message === message && item.severity === severity)), { id, message, severity }].slice(-3));
    start(id, durations[severity]);
  }, [start]);
  useEffect(() => () => { for (const timer of timers.current.values()) clearTimeout(timer); }, []);

  return <NotificationContext.Provider value={notify}>
    {children}
    <div aria-label={t("Notifications")} className="pointer-events-none fixed right-4 top-4 z-[100] flex w-[min(380px,calc(100vw-2rem))] flex-col gap-2">
      {items.map(item => {
        const Icon = icons[item.severity];
        return <div key={item.id} role={item.severity === "error" ? "alert" : "status"}
          onMouseEnter={() => pause(item.id)} onMouseLeave={() => resume(item.id)}
          onFocusCapture={() => pause(item.id)} onBlurCapture={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) resume(item.id); }}
          data-toast-severity={item.severity}
          className="finintel-toast pointer-events-auto flex items-start gap-2 rounded-lg border p-3 text-sm shadow-lg">
          <Icon aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
          <p className="min-w-0 flex-1 break-words">{item.message}</p>
          <button type="button" aria-label={t("Dismiss notification")} onClick={() => dismiss(item.id)} className="rounded p-1 hover:bg-white/20 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-current"><X aria-hidden="true" className="size-4" /></button>
        </div>;
      })}
    </div>
  </NotificationContext.Provider>;
}
