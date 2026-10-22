/** Native Tauri IPC on Mac; same read API via Vite proxy in browser development. */

import { invoke, isTauri } from "@tauri-apps/api/core";
import {
  parseDaily,
  parseImports,
  parseInstruments,
  type DailySeries,
  type ImportRecord,
  type MarketInstrument,
} from "./market";

async function getLocal(path: string, command: string, args?: Record<string, unknown>): Promise<unknown> {
  if (isTauri()) return invoke<unknown>(command, args);
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error("Local Python backend returned HTTP " + response.status + ".");
  return response.json();
}

export async function fetchMarketInstruments(): Promise<MarketInstrument[]> {
  return parseInstruments(await getLocal("/api/v1/market-data/instruments", "market_instruments"));
}

export interface DailyQuery {
  symbol: string;
  startDate?: string;
  endDate?: string;
  limit?: number;
}

export async function fetchMarketDaily(query: DailyQuery): Promise<DailySeries> {
  const limit = query.limit ?? 250;
  if (!/^[A-Za-z0-9][A-Za-z0-9.-]{0,14}$/.test(query.symbol)) {
    throw new Error("Enter a valid stock ticker.");
  }
  if (!Number.isInteger(limit) || limit < 1 || limit > 1000) {
    throw new Error("History limit must be 1–1000.");
  }
  if (query.startDate && query.endDate && query.startDate > query.endDate) {
    throw new Error("Start date must not exceed end date.");
  }
  const params = new URLSearchParams({ limit: String(limit), latest: "true" });
  if (query.startDate) params.set("start_date", query.startDate);
  if (query.endDate) params.set("end_date", query.endDate);
  const payload = await getLocal(
    "/api/v1/market-data/" + encodeURIComponent(query.symbol.toUpperCase()) + "/daily?" + params,
    "market_daily",
    {
      symbol: query.symbol.toUpperCase(),
      startDate: query.startDate ?? null,
      endDate: query.endDate ?? null,
      limit,
      latest: true,
    },
  );
  const parsed = parseDaily(payload);
  if (parsed.symbol !== query.symbol.toUpperCase()) {
    throw new Error("Backend returned prices for the wrong instrument.");
  }
  return parsed;
}

export async function fetchMarketImports(limit = 10): Promise<ImportRecord[]> {
  const payload = await getLocal(
    "/api/v1/market-data/imports?limit=" + limit,
    "market_imports",
    { limit },
  );
  return parseImports(payload);
}
