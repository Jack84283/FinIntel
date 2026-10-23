# FinIntel UI foundation — shadcn/ui + Tailwind CSS v4

The Mac desktop window (Tauri 2) and future iPad/Android PWA share React/TypeScript UI components.

## Stack and ownership

- **React 19 + Vite + Tailwind CSS v4** using `@tailwindcss/vite`.
- **shadcn/ui source-owned primitives**: local `src/components/ui/{button,card,badge,skeleton}.tsx`.
- **Base UI** powers the interactive Button. Other initial primitives are semantic presentational components; add more via the shadcn CLI when their interactions are needed.
- **FinIntel semantic tokens** in `src/styles.css`, exposed to Tailwind via `@theme inline` (colors, rings and radius). The initial visual appearance intentionally preserves PR #1.
- **Class utility:** `src/lib/utils.ts` with `clsx` and `tailwind-merge`.

This is a real source-owned shadcn/ui integration, not a linked third-party stylesheet or an embedded demo. The installed component code is part of this repository and may be reviewed/customized. Base-nova CLI configuration lives in `apps/desktop/components.json`.

## Shared page layout and gutters

Use the single shared `<main>` container in `src/App.tsx` for Overview, Markets, and Data Acquisition. **Keep page-edge spacing separate from shadcn Card internals**:

- Compact screens: `px-3` (**12px** horizontal page gutter).
- Tablet and desktop: `sm:px-4` (**16px** horizontal page gutter), including wide desktop windows.
- Vertical: `py-4` (**16px**), increasing to `lg:py-5` (**20px**) on desktop.
- Use `w-full min-w-0` without an arbitrary `max-w` cap, so market tables and charts can use the full content area beside the fixed navigation rail.
- Do not add page gutters again in individual screens. Component padding (`CardContent`, `CardHeader`) remains source-owned shadcn styling; tighter page edges must not compromise card legibility, keyboard focus or responsive layout.

The Overview intro uses `py-6 lg:py-8` to preserve hierarchy with less unused vertical space.

## Dense card layout

FinIntel is a **data-dense financial workstation**. Keep the shared card-to-card
spacing predictable and compact, rather than adding new margins inside
individual page sections:

- Markets' top-level card/section stack uses `space-y-2` (**8px**) instead
  of `space-y-5` (20px).
- The Overview two-card grid and the Markets KPI card grid both use `gap-2`
  (**8px**) instead of `gap-4` (16px).
- Candlestick and volume chart sections use `space-y-2` (**8px**) instead
  of `space-y-4` (16px).
- Keep all `Card`, `CardHeader`, and `CardContent` **internal** padding,
  chart legibility, control hit targets, keyboard focus, and responsive
  wrapping unchanged. These rules only adjust the **space between** peers.
- Extend this 8px inter-card spacing convention to new market, research and
  dashboard pages; do not alter field-to-field or table-row spacing merely
  to reduce card gutters.



Work in the desktop workspace:
```sh
cd apps/desktop
npx shadcn@latest add input dialog tabs select
```

Review all generated component changes and dependencies before committing. Do not let the CLI overwrite FI design decisions without review.

## Boundaries

- Keep native Tauri calls in transport/integration modules, outside reusable React UI primitives. Future PWA components must not depend on Tauri IPC.
- Use dedicated libraries for candlestick/technical charts and virtualized data grids. shadcn cards/menus/forms wrap those features; they do not replace quantitative visualization engines.
- Financial calculations, local data ingestion, and local AI remain in the Python backend.
- UI controls must pass keyboard, touch, large-text, contrast and screen-reader checks. Mobile/tablet layouts are adaptive; the PWA itself is not yet implemented.
- Base UI is the chosen primitive library for new interactive controls; avoid accidental Radix/Base mixing.

## Validation

```sh
npm install
npm run lint
npm run typecheck
npm run test
npm run build
npm run tauri:dev
```

GitHub CI covers React and Tauri compilation. Real-device PWA QA, app bundling and tablet authentication follow in their respective milestones.

Official documentation: https://ui.shadcn.com/docs/installation/vite

## Separation of research and data operations

Keep these top-level screens deliberately distinct:

- **Markets** (read-only): exploration of local historical OHLCV
  observations, period/date filtering, candlestick/volume charts,
  prices, and provider provenance.
- **Data Acquisition** (mutating): explicit fetch controls,
  provider/key status, ticker discovery and the **Action History**
  card of recently completed imports. Never trigger external fetches
  during navigation or history table rendering.
- **Overview**: application status and links to both workspaces.

Reuse the `MarketSelection` context (symbol + provider) when
navigating from a successful acquisition or a history-row **View**
action to Markets. Never conflate separate Alpaca and Alpha Vantage
series for one ticker. Keep the sidebar and compact navigation
consistent, keyboard-accessible, and within the shared 8px
inter-card spacing system.

Action History currently represents completed, persisted
`ingestion_runs` only; failed attempts, cancellation and provider
quota rejections are not stored as durable audit events.
