# FinIntel UI foundation — shadcn/ui + Tailwind CSS v4

The Mac desktop window (Tauri 2) and future iPad/Android PWA share React/TypeScript UI components.

## Stack and ownership

- **React 19 + Vite + Tailwind CSS v4** using `@tailwindcss/vite`.
- **shadcn/ui source-owned primitives**: local `src/components/ui/{button,card,badge,skeleton}.tsx`.
- **Base UI** powers the interactive Button. Other initial primitives are semantic presentational components; add more via the shadcn CLI when their interactions are needed.
- **FinIntel semantic tokens** in `src/styles.css`, exposed to Tailwind via `@theme inline` (colors, rings and radius). The initial visual appearance intentionally preserves PR #1.
- **Class utility:** `src/lib/utils.ts` with `clsx` and `tailwind-merge`.

This is a real source-owned shadcn/ui integration, not a linked third-party stylesheet or an embedded demo. The installed component code is part of this repository and may be reviewed/customized. Base-nova CLI configuration lives in `apps/desktop/components.json`.

## Adding UI components

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
