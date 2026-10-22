/** Explicit acquisition requests; no secret API key ever enters the webview. */

import { invoke, isTauri } from "@tauri-apps/api/core";

export type OutputSize = "compact" | "full";

export interface AcquisitionStatus {
  provider: "alpha_vantage";
  key_configured: boolean;
  requires_explicit_action: true;
  api_key_environment_variable: string;
  full_requires_premium: true;
}

export interface AcquisitionResult {
  status: "stored";
  symbol: string;
  provider: string;
  frequency: string;
  price_adjustment: string;
  first_session: string;
  last_session: string;
  retrieved_at: string;
  observations: number;
  inserted: number;
  updated: number;
  unchanged: number;
  stale: number;
  import_id: number;
  not_live_quotes: true;
}

function object(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Unexpected acquisition response from local backend.");
  }
  return value as Record<string, unknown>;
}

function numberField(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    throw new Error("Invalid ingestion count from local backend.");
  }
  return value;
}

function stringField(value: unknown): string {
  if (typeof value !== "string" || !value) {
    throw new Error("Invalid acquisition metadata from local backend.");
  }
  return value;
}

export function parseAcquisitionStatus(value: unknown): AcquisitionStatus {
  const item = object(value);
  if (
    item.provider !== "alpha_vantage" ||
    typeof item.key_configured !== "boolean" ||
    item.requires_explicit_action !== true ||
    item.api_key_environment_variable !== "FININTEL_ALPHA_VANTAGE_API_KEY" ||
    item.full_requires_premium !== true
  ) {
    throw new Error("Invalid acquisition status response.");
  }
  return {
    provider: "alpha_vantage",
    key_configured: item.key_configured,
    requires_explicit_action: true,
    api_key_environment_variable: "FININTEL_ALPHA_VANTAGE_API_KEY",
    full_requires_premium: true,
  };
}

export function parseAcquisitionResult(value: unknown): AcquisitionResult {
  const item = object(value);
  if (item.status !== "stored" || item.not_live_quotes !== true) {
    throw new Error("Unexpected acquisition result.");
  }
  const symbol = stringField(item.symbol);
  if (!/^[A-Z0-9][A-Z0-9.-]{0,14}$/.test(symbol)) {
    throw new Error("Invalid acquired instrument.");
  }
  return {
    status: "stored",
    symbol,
    provider: stringField(item.provider),
    frequency: stringField(item.frequency),
    price_adjustment: stringField(item.price_adjustment),
    first_session: stringField(item.first_session),
    last_session: stringField(item.last_session),
    retrieved_at: stringField(item.retrieved_at),
    observations: numberField(item.observations),
    inserted: numberField(item.inserted),
    updated: numberField(item.updated),
    unchanged: numberField(item.unchanged),
    stale: numberField(item.stale),
    import_id: numberField(item.import_id),
    not_live_quotes: true,
  };
}

async function errorDetail(response: Response): Promise<string> {
  try {
    const json: unknown = await response.json();
    const value = object(json).detail;
    return typeof value === "string" && value.length <= 400
      ? value : "Market data request did not succeed.";
  } catch {
    return "Market data request did not succeed.";
  }
}

export async function fetchAcquisitionStatus(): Promise<AcquisitionStatus> {
  if (isTauri()) {
    return parseAcquisitionStatus(await invoke<unknown>("market_acquisition_status"));
  }
  const response = await fetch("/api/v1/market-data/acquisition/status", { cache: "no-store" });
  if (!response.ok) throw new Error("Local acquisition status is unavailable.");
  return parseAcquisitionStatus(await response.json());
}

export async function acquireMarketDaily(symbol: string, outputsize: OutputSize): Promise<AcquisitionResult> {
  const ticker = symbol.trim().toUpperCase();
  if (!/^[A-Z0-9][A-Z0-9.-]{0,14}$/.test(ticker)) {
    throw new Error("Enter a valid equity ticker (for example AAPL).");
  }
  if (outputsize !== "compact" && outputsize !== "full") {
    throw new Error("Select a valid history size.");
  }
  let result: unknown;
  if (isTauri()) {
    result = await invoke<unknown>("market_acquire_daily", { symbol: ticker, outputsize });
  } else {
    const response = await fetch("/api/v1/market-data/acquisition", {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json", "X-FinIntel-Client": "local-workstation" },
      body: JSON.stringify({ symbol: ticker, outputsize }),
    });
    if (!response.ok) throw new Error("HTTP " + response.status + ": " + await errorDetail(response));
    result = await response.json();
  }
  const parsed = parseAcquisitionResult(result);
  if (parsed.symbol !== ticker) throw new Error("Provider returned a mismatched ticker.");
  return parsed;
}
