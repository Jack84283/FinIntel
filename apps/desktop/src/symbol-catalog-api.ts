/** Search the local stock/ETF directory, never trigger provider traffic on page load. */

import { invoke, isTauri } from "@tauri-apps/api/core";

export type AssetType = "Stock" | "ETF" | "Index" | "Mutual Fund";
export type AssetFilter = AssetType | "All";

export interface CatalogSymbol {
  symbol: string;
  name: string;
  asset_type: AssetType;
  exchange: string;
}

export interface SymbolSearchResponse {
  source: "starter" | "alpha_vantage" | "equibles_proxy";
  total: number;
  updated_at: string | null;
  results: CatalogSymbol[];
}

export interface CatalogSyncResult {
  status: "synced";
  total: number;
  updated_at: string;
  source: "alpha_vantage";
}

function object(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Invalid local symbol directory response.");
  }
  return value as Record<string, unknown>;
}

export function parseSymbolSearch(value: unknown): SymbolSearchResponse {
  const item = object(value);
  if (
    (item.source !== "starter" && item.source !== "alpha_vantage" && item.source !== "equibles_proxy") ||
    !Number.isSafeInteger(item.total) || (item.total as number) < 0 ||
    (item.updated_at !== null && typeof item.updated_at !== "string") ||
    !Array.isArray(item.results) || item.results.length > 100
  ) {
    throw new Error("Invalid local symbol directory metadata.");
  }
  return {
    source: item.source,
    total: item.total as number,
    updated_at: item.updated_at,
    results: item.results.map((entry: unknown) => {
      const row = object(entry);
      if (
        typeof row.symbol !== "string" ||
        !(/^\^?[A-Z0-9][A-Z0-9.-]{0,14}$/.test(row.symbol)) ||
        typeof row.name !== "string" ||
        typeof row.exchange !== "string" ||
        (row.asset_type !== "Stock" && row.asset_type !== "ETF" && row.asset_type !== "Index" && row.asset_type !== "Mutual Fund")
      ) {
        throw new Error("Invalid stock or ETF directory entry.");
      }
      return {
        symbol: row.symbol,
        name: row.name,
        asset_type: row.asset_type,
        exchange: row.exchange,
      };
    }),
  };
}

export function parseCatalogSync(value: unknown): CatalogSyncResult {
  const data = object(value);
  if (
    data.status !== "synced" || data.source !== "alpha_vantage" ||
    !Number.isSafeInteger(data.total) || (data.total as number) <= 0 ||
    typeof data.updated_at !== "string"
  ) {
    throw new Error("Invalid symbol sync response.");
  }
  return {
    status: "synced", source: "alpha_vantage",
    total: data.total as number, updated_at: data.updated_at,
  };
}

export async function searchSymbols(
  query: string,
  kind: AssetFilter = "All",
  limit = 40,
): Promise<SymbolSearchResponse> {
  if (query.length > 80 || !Number.isInteger(limit) || limit < 1 || limit > 100) {
    throw new Error("Invalid symbol directory search.");
  }
  const type = kind === "All" ? null : kind;
  let data: unknown;
  if (isTauri()) {
    data = await invoke<unknown>("market_symbols", { q: query, assetType: type, limit });
  } else {
    const params = new URLSearchParams({ q: query, limit: String(limit) });
    if (type !== null) params.set("asset_type", type);
    const response = await fetch("/api/v1/market-data/symbols?" + params, { cache: "no-store" });
    if (!response.ok) throw new Error("Local symbol directory is unavailable.");
    data = await response.json();
  }
  return parseSymbolSearch(data);
}

export async function syncSymbols(): Promise<CatalogSyncResult> {
  let data: unknown;
  if (isTauri()) {
    data = await invoke<unknown>("market_sync_symbols");
  } else {
    const response = await fetch("/api/v1/market-data/symbols/sync", {
      method: "POST",
      cache: "no-store",
      headers: { "X-FinIntel-Client": "local-workstation" },
    });
    if (!response.ok) {
      const payload: unknown = await response.json().catch(() => ({}));
      const detail = object(payload).detail;
      throw new Error(
        typeof detail === "string" && detail.length < 400
          ? detail
          : "Local symbol listing sync failed (HTTP " + response.status + ").",
      );
    }
    data = await response.json();
  }
  return parseCatalogSync(data);
}

/** Stock/ETF-only acquisition search; reference indexes and mutual funds
 * remain in the master catalog but are not offered as historical API inputs.
 * Two bounded local queries ensure indexes cannot crowd out results.
 */
export async function searchAcquirableSymbols(
  query: string,
  kind: "All" | "Stock" | "ETF" = "All",
  limit = 40,
): Promise<SymbolSearchResponse> {
  if (kind !== "All") return searchSymbols(query, kind, limit);
  const [stocks, funds] = await Promise.all([
    searchSymbols(query, "Stock", limit),
    searchSymbols(query, "ETF", limit),
  ]);
  return mergeAcquirableResults(stocks, funds, query, limit);
}

export function mergeAcquirableResults(
  stocks: SymbolSearchResponse,
  funds: SymbolSearchResponse,
  query: string,
  limit: number,
): SymbolSearchResponse {
  const prefix = query.trim().toUpperCase();
  const results = [...stocks.results, ...funds.results]
    .filter((entry) => entry.asset_type === "Stock" || entry.asset_type === "ETF")
    .sort((a, b) => {
      const aPrefix = a.symbol.startsWith(prefix) ? 0 : 1;
      const bPrefix = b.symbol.startsWith(prefix) ? 0 : 1;
      return aPrefix - bPrefix || a.symbol.localeCompare(b.symbol);
    })
    .slice(0, limit);
  return { ...stocks, results };
}
