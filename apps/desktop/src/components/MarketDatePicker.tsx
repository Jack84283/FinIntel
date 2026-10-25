import { useEffect, useId, useRef, useState } from "react";
import { CalendarDays, ChevronLeft, ChevronRight } from "lucide-react";

interface MarketDatePickerProps {
  label: string;
  value: string;
  onChange: (value: string) => void;
}

const weekdays = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];
const formatDate = (date: Date) =>
  [date.getFullYear(), String(date.getMonth() + 1).padStart(2, "0"), String(date.getDate()).padStart(2, "0")].join("-");
const parseDate = (value: string) => {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return null;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return formatDate(date) === value ? date : null;
};

/** Theme-aware alternative to unstyleable native browser calendar popups. */
export function MarketDatePicker({ label, value, onChange }: MarketDatePickerProps) {
  const [open, setOpen] = useState(false);
  const [month, setMonth] = useState(() => {
    const date = parseDate(value) ?? new Date();
    return new Date(date.getFullYear(), date.getMonth(), 1);
  });
  const wrapper = useRef<HTMLDivElement>(null);
  const listId = useId();

  useEffect(() => {
    if (!open) return;
    const listener = (event: PointerEvent) => {
      if (!wrapper.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", listener);
    return () => document.removeEventListener("pointerdown", listener);
  }, [open]);

  const firstWeekday = month.getDay();
  const lastDay = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();
  const selected = parseDate(value);
  const today = formatDate(new Date());
  const days = Array.from({ length: Math.ceil((firstWeekday + lastDay) / 7) * 7 }, (_, index) => index - firstWeekday + 1);

  return (
    <div ref={wrapper} className="relative grid min-w-0 gap-2 text-sm font-medium text-muted-foreground">
      <label id={listId + "-label"}>{label}</label>
      <button
        type="button"
        aria-labelledby={listId + "-label"}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => {
          if (!open) {
            const date = selected ?? new Date();
            setMonth(new Date(date.getFullYear(), date.getMonth(), 1));
          }
          setOpen(!open);
        }}
        className="flex h-12 w-full min-w-0 items-center justify-between gap-2 rounded-md border border-input bg-muted px-3 text-left text-base text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span className={value ? "" : "text-muted-foreground"}>{value || "Select date"}</span>
        <CalendarDays aria-hidden="true" className="size-5 shrink-0" />
      </button>
      {open && (
        <div
          role="dialog"
          aria-label={label + " calendar"}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.stopPropagation();
              setOpen(false);
            }
          }}
          className="absolute left-0 top-full z-40 mt-1 w-[min(340px,calc(100vw-40px))] rounded-xl border border-border bg-card p-4 text-card-foreground shadow-xl"
        >
          <div className="mb-4 flex items-center justify-between gap-2">
            <button type="button" aria-label="Previous month" className="rounded-md p-2 hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() - 1, 1))}>
              <ChevronLeft className="size-5" aria-hidden="true" />
            </button>
            <strong className="text-lg">{month.toLocaleDateString(undefined, { month: "long", year: "numeric" })}</strong>
            <button type="button" aria-label="Next month" className="rounded-md p-2 hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))}>
              <ChevronRight className="size-5" aria-hidden="true" />
            </button>
          </div>
          <div className="grid grid-cols-7 gap-1 text-center">
            {weekdays.map((day) => <span key={day} className="py-2 text-sm font-semibold text-muted-foreground">{day}</span>)}
            {days.map((day, index) => {
              if (day < 1 || day > lastDay) return <span key={index} />;
              const date = formatDate(new Date(month.getFullYear(), month.getMonth(), day));
              return (
                <button
                  key={date}
                  type="button"
                  aria-label={new Date(month.getFullYear(), month.getMonth(), day).toLocaleDateString(undefined, { dateStyle: "full" })}
                  aria-pressed={date === value}
                  onClick={() => { onChange(date); setOpen(false); }}
                  className={`h-10 rounded-md text-base tabular-nums hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${date === value ? "bg-primary text-primary-foreground hover:bg-primary" : date === today ? "border border-ring" : ""}`}
                >
                  {day}
                </button>
              );
            })}
          </div>
          <div className="mt-3 flex justify-between gap-2 border-t border-border pt-3 text-sm">
            <button type="button" className="rounded-md px-2 py-1 hover:bg-accent" onClick={() => { onChange(""); setOpen(false); }}>Clear date</button>
            <button type="button" className="rounded-md px-2 py-1 hover:bg-accent" onClick={() => { onChange(today); setOpen(false); }}>Today</button>
          </div>
        </div>
      )}
    </div>
  );
}
