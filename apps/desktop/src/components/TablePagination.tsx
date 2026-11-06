import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { useLocale } from "../localization";

export const TABLE_PAGE_SIZE = 10;

/** Clamp user-selected pages while preserving the 0-based API pagination contract. */
export function boundedPage(page: number, total: number): number {
  const last = Math.max(0, Math.ceil(Math.max(0, total) / TABLE_PAGE_SIZE) - 1);
  return Number.isSafeInteger(page) ? Math.max(0, Math.min(last, page)) : 0;
}
export function filterRows<T>(rows: readonly T[], query: string, searchable: (row: T) => string): T[] {
  const needle = query.trim().toLocaleLowerCase();
  return needle ? rows.filter(row => searchable(row).toLocaleLowerCase().includes(needle)) : [...rows];
}
export function pageRows<T>(rows: readonly T[], page: number): T[] {
  const safe = boundedPage(page, rows.length);
  return rows.slice(safe * TABLE_PAGE_SIZE, (safe + 1) * TABLE_PAGE_SIZE);
}

/** Pager for both server-side and in-memory tables. Page jump commits on Go or Enter. */
export function TablePager({ page, total, onPageChange, disabled = false }: {
  page: number; total: number; onPageChange: (page: number) => void; disabled?: boolean;
}) {
  const { t, language } = useLocale();
  const count = Math.max(1, Math.ceil(Math.max(0, total) / TABLE_PAGE_SIZE));
  const current = boundedPage(page, total);
  const [requested, setRequested] = useState(String(current + 1));
  useEffect(() => { setRequested(String(current + 1)); }, [current]);
  const go = () => {
    const number = Number(requested);
    if (Number.isSafeInteger(number) && number >= 1 && number <= count) onPageChange(number - 1);
    else setRequested(String(current + 1));
  };
  return <nav aria-label={t("Table pagination")} className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
    <span aria-live="polite">{t("Showing")} {total ? current * TABLE_PAGE_SIZE + 1 : 0}–{Math.min(total, (current + 1) * TABLE_PAGE_SIZE)} {t("of")} {total.toLocaleString(language)} · {t("Page")} {current + 1} {t("of")} {count}</span>
    <div className="flex flex-wrap items-center gap-2">
      <Button type="button" variant="outline" size="sm" disabled={disabled || current === 0}
        onClick={() => onPageChange(current - 1)}>{t("Previous")}</Button>
      <label className="flex items-center gap-1">{t("Go to page")}
        <input type="number" inputMode="numeric" aria-label={t("Go to page")}
          className="h-9 w-20 rounded-md border border-input bg-background px-2 text-sm text-foreground"
          value={requested} min={1} max={count} disabled={disabled}
          onChange={event => setRequested(event.target.value)}
          onKeyDown={event => { if (event.key === "Enter") { event.preventDefault(); go(); } }} />
      </label>
      <Button type="button" variant="outline" size="sm" disabled={disabled}
        onClick={go}>{t("Go")}</Button>
      <Button type="button" variant="outline" size="sm" disabled={disabled || current + 1 >= count}
        onClick={() => onPageChange(current + 1)}>{t("Next")}</Button>
    </div>
  </nav>;
}

/** In-memory tables filter the entire currently loaded result, before pagination. */
export function LocalDataTable<T>({ rows, searchable, children, label }: {
  rows: readonly T[]; searchable: (row: T) => string;
  children: (visible: T[]) => ReactNode; label: string;
}) {
  const { t } = useLocale();
  const [filter, setFilter] = useState("");
  const [page, setPage] = useState(0);
  const filtered = useMemo(() => filterRows(rows, filter, searchable), [rows, filter, searchable]);
  const current = boundedPage(page, filtered.length);
  useEffect(() => { if (page !== current) setPage(current); }, [page, current]);
  return <div className="space-y-2">
    <label className="flex max-w-md flex-col gap-1 text-xs text-muted-foreground">
      {t("Filter table")} — {label}
      <input type="search" value={filter} aria-label={t("Filter table") + " — " + label}
        placeholder={t("Search table values")} onChange={event => { setFilter(event.target.value); setPage(0); }}
        className="h-9 rounded-md border border-input bg-background px-3 text-sm text-foreground" />
    </label>
    {children(pageRows(filtered, current))}
    {filtered.length === 0 && <p role="status" className="text-xs text-muted-foreground">{t("No rows match these filters.")}</p>}
    <TablePager total={filtered.length} page={current} onPageChange={setPage} />
  </div>;
}
