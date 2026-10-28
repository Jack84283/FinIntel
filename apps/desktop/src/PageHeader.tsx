import type { ReactNode } from "react";

/** Identical two-row page heading on every desktop screen. */
interface PageHeaderProps {
  id: string;
  breadcrumb: string;
  title: ReactNode;
  subtitle: ReactNode;
  actions?: ReactNode;
  subtitleActions?: ReactNode;
}

export function PageHeader({ id, breadcrumb, title, subtitle, actions, subtitleActions }: PageHeaderProps) {
  return (
    <header className="grid min-w-0 gap-2 border-b border-border pb-2">
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-sm font-bold tracking-[0.16em] text-[#8aa5c3] uppercase">{breadcrumb}</p>
          <h1 id={id} className="mt-2 text-2xl font-semibold tracking-tight">{title}</h1>
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <p className="min-w-0 text-sm text-muted-foreground">{subtitle}</p>
        {subtitleActions}
      </div>
    </header>
  );
}
