import { invoke, isTauri } from "@tauri-apps/api/core";

export type DailyProvider = "alpaca_sip" | "tiingo" | "alpha_vantage";
export type DailyJobStatus = "idle" | "scheduled" | "running" | "paused" | "completed" | "partial" | "failed" | "cancelled";

export interface DailySettings {
  enabled: boolean;
  schedule_time: string;
  timezone: string;
  primary_provider: DailyProvider;
  secondary_provider: DailyProvider | null;
  overlap_days: number;
  retry_attempts: number;
  retry_backoff_seconds: number;
  updated_at: string | null;
}

export interface DailyItem {
  kind: "equity" | "index";
  code: string;
  provider: string | null;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  attempts: number;
  started_at: string | null;
  finished_at: string | null;
  last_session: string | null;
  inserted: number;
  updated: number;
  unchanged: number;
  stale: number;
  error_type: string | null;
  error_message: string | null;
  validation_status: string;
  missing_sessions: number;
}

export interface DailyJob {
  id: string | null;
  kind: "automatic" | "manual" | "retry_failed" | null;
  status: DailyJobStatus;
  provider: string;
  secondary_provider: string | null;
  scheduled_for: string | null;
  started_at: string | null;
  finished_at: string | null;
  current_instrument: string | null;
  total_instruments: number;
  completed_instruments: number;
  failed_instruments: number;
  pending_instruments: number;
  successful_imports: number;
  inserted: number;
  updated: number;
  unchanged: number;
  retry_count: number;
  provider_errors: Array<Record<string, unknown>>;
  index_results: Record<string, unknown>;
  duration_ms: number | null;
  items?: DailyItem[];
}

export interface DailyStatus extends DailyJob {
  enabled: boolean;
  scheduled_execution_time: string;
  timezone: string;
  primary_provider: DailyProvider;
  secondary_provider: DailyProvider | null;
  last_execution_time: string | null;
  last_successful_execution: string | null;
  next_scheduled_execution: string;
  last_updated_trading_session: string | null;
  job_id: string | null;
  job: DailyJob;
}

export interface DailyHistoryPage {
  items: DailyJob[];
  total: number;
  limit: number;
  offset: number;
}

const DAILY_PROVIDERS: DailyProvider[] = ["alpaca_sip", "tiingo", "alpha_vantage"];

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Invalid daily acquisition response.");
  }
  return value as Record<string, unknown>;
}

function optionalString(value: unknown): string | null {
  return value === null || typeof value === "string" ? value : null;
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

function provider(value: unknown): DailyProvider {
  return DAILY_PROVIDERS.includes(value as DailyProvider) ? value as DailyProvider : "alpaca_sip";
}

function job(value: unknown): DailyJob {
  const item = record(value);
  const rawItems = Array.isArray(item.items) ? item.items : undefined;
  return {
    id: optionalString(item.id),
    kind: item.kind === "automatic" || item.kind === "manual" || item.kind === "retry_failed" ? item.kind : null,
    status: item.status === "scheduled" || item.status === "running" || item.status === "paused" || item.status === "completed" || item.status === "partial" || item.status === "failed" || item.status === "cancelled" ? item.status : "idle",
    provider: provider(item.provider),
    secondary_provider: optionalString(item.secondary_provider),
    scheduled_for: optionalString(item.scheduled_for),
    started_at: optionalString(item.started_at),
    finished_at: optionalString(item.finished_at),
    current_instrument: optionalString(item.current_instrument),
    total_instruments: count(item.total_instruments),
    completed_instruments: count(item.completed_instruments),
    failed_instruments: count(item.failed_instruments),
    pending_instruments: count(item.pending_instruments),
    successful_imports: count(item.successful_imports),
    inserted: count(item.inserted),
    updated: count(item.updated),
    unchanged: count(item.unchanged),
    retry_count: count(item.retry_count),
    provider_errors: Array.isArray(item.provider_errors) ? item.provider_errors.filter((x): x is Record<string, unknown> => Boolean(x && typeof x === "object" && !Array.isArray(x))) : [],
    index_results: item.index_results && typeof item.index_results === "object" && !Array.isArray(item.index_results) ? item.index_results as Record<string, unknown> : {},
    duration_ms: typeof item.duration_ms === "number" ? item.duration_ms : null,
    items: rawItems as DailyItem[] | undefined,
  };
}

export function parseDailyStatus(value: unknown): DailyStatus {
  const item = record(value);
  const base = job(item);
  const next = item.next_scheduled_execution;
  if (typeof item.enabled !== "boolean" || typeof item.scheduled_execution_time !== "string" ||
      typeof item.timezone !== "string" || typeof next !== "string") {
    throw new Error("Invalid daily acquisition status.");
  }
  return {
    ...base,
    enabled: item.enabled,
    scheduled_execution_time: item.scheduled_execution_time,
    timezone: item.timezone,
    primary_provider: provider(item.primary_provider),
    secondary_provider: DAILY_PROVIDERS.includes(item.secondary_provider as DailyProvider)
      ? item.secondary_provider as DailyProvider
      : null,
    last_execution_time: optionalString(item.last_execution_time),
    last_successful_execution: optionalString(item.last_successful_execution),
    next_scheduled_execution: next,
    last_updated_trading_session: optionalString(item.last_updated_trading_session),
    job_id: optionalString(item.job_id),
    job: job(item.job),
  };
}

export function parseDailySettings(value: unknown): DailySettings {
  const item = record(value);
  const overlapDays = item.overlap_days;
  const retryAttempts = item.retry_attempts;
  const retryBackoffSeconds = item.retry_backoff_seconds;
  if (typeof item.enabled !== "boolean" || typeof item.schedule_time !== "string" ||
      typeof item.timezone !== "string" || !DAILY_PROVIDERS.includes(item.primary_provider as DailyProvider) ||
      (item.secondary_provider !== null && !DAILY_PROVIDERS.includes(item.secondary_provider as DailyProvider)) ||
      typeof overlapDays !== "number" || !Number.isSafeInteger(overlapDays) || overlapDays < 0 ||
      typeof retryAttempts !== "number" || !Number.isSafeInteger(retryAttempts) || retryAttempts < 1 ||
      typeof retryBackoffSeconds !== "number" || retryBackoffSeconds < 0 ||
      (item.updated_at !== null && typeof item.updated_at !== "string")) {
    throw new Error("Invalid daily acquisition settings.");
  }
  return {
    enabled: item.enabled,
    schedule_time: item.schedule_time,
    timezone: item.timezone,
    primary_provider: item.primary_provider as DailyProvider,
    secondary_provider: item.secondary_provider as DailyProvider | null,
    overlap_days: overlapDays,
    retry_attempts: retryAttempts,
    retry_backoff_seconds: retryBackoffSeconds,
    updated_at: item.updated_at as string | null,
  };
}

async function read(path: string, command: string, args: Record<string, unknown> = {}): Promise<unknown> {
  if (isTauri()) return invoke<unknown>(command, args);
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error("Daily acquisition returned HTTP " + response.status + ".");
  return response.json();
}

async function write(path: string, command: string, method: "POST" | "PUT", body?: unknown): Promise<unknown> {
  if (isTauri()) return invoke<unknown>(command, body && typeof body === "object" ? body as Record<string, unknown> : {});
  const response = await fetch(path, {
    method,
    cache: "no-store",
    headers: { "Content-Type": "application/json", "X-FinIntel-Client": "local-workstation" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({})) as { detail?: unknown };
    throw new Error(typeof detail.detail === "string" ? detail.detail : "Daily acquisition request failed.");
  }
  return response.json();
}

export async function fetchDailyStatus(): Promise<DailyStatus> {
  return parseDailyStatus(await read("/api/v1/daily-acquisition/status", "daily_acquisition_status"));
}

export async function fetchDailySettings(): Promise<DailySettings> {
  return parseDailySettings(await read("/api/v1/daily-acquisition/settings", "daily_acquisition_settings"));
}

export async function saveDailySettings(patch: Partial<DailySettings>): Promise<DailySettings> {
  return parseDailySettings(await write("/api/v1/daily-acquisition/settings", "daily_acquisition_save_settings", "PUT", patch));
}

export async function runDailyAcquisition(): Promise<DailyJob> {
  return job(await write("/api/v1/daily-acquisition/run", "daily_acquisition_run", "POST"));
}

export async function controlDailyAcquisition(jobId: string, action: "pause" | "resume" | "cancel" | "retry-failed"): Promise<DailyJob> {
  if (!/^[a-f0-9]{32}$/.test(jobId)) throw new Error("Invalid daily acquisition job ID.");
  const command = action === "pause" ? "daily_acquisition_pause" : action === "resume" ? "daily_acquisition_resume" : action === "cancel" ? "daily_acquisition_cancel" : "daily_acquisition_retry_failed";
  if (isTauri()) return job(await invoke<unknown>(command, { jobId }));
  return job(await write(`/api/v1/daily-acquisition/jobs/${jobId}/${action}`, command, "POST"));
}

export async function searchDailyHistory(query: string, providerValue: string, statusValue: string, kindValue: string, offset: number, limit = 20): Promise<DailyHistoryPage> {
  const params = new URLSearchParams({ q: query, provider: providerValue, status: statusValue, kind: kindValue, offset: String(offset), limit: String(limit) });
  const result = record(await read("/api/v1/daily-acquisition/history?" + params, "daily_acquisition_history", { q: query, provider: providerValue, status: statusValue, kind: kindValue, offset, limit }));
  if (!Number.isSafeInteger(result.total) || result.limit !== limit || result.offset !== offset || !Array.isArray(result.items)) throw new Error("Invalid daily acquisition history.");
  return { items: result.items.map(job), total: result.total as number, limit, offset };
}
