/** Native-only durable model jobs client for existing Phase 2B1 IPC. No HTTP fallback. */

import { invoke, isTauri } from "@tauri-apps/api/core";
import {
  parseModelResearchResult, validateModelResearchQuery,
  type ModelResearchQuery, type ModelResearchResult,
} from "./model-research-api";

export type ModelJobState = "queued" | "running" | "completed" | "failed" | "cancelled";
export type ModelJobStatus = ModelJobState | "cancel_requested";
export type ModelJobSummary = {
  id: string;
  state: ModelJobState;
  status: ModelJobStatus;
  model_id: "builtin.sma_crossover";
  model_version: "1.0.0";
  request_sha256: string;
  attempts: number;
  progress: number;
  cancel_requested: 0 | 1;
  output_sha256: string | null;
  source_snapshot_sha256: string | null;
  error_code: string | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
};
export type ModelJobPage = { total: number; items: ModelJobSummary[] };
export type PinnedJobQuery = ModelResearchQuery & { expectedSnapshot: string };
export type ModelJobWithResult = ModelJobSummary & { result: ModelResearchResult };

const states = ["queued", "running", "completed", "failed", "cancelled"];
const errors = [
  "interrupted", "invalid_input", "quality_blocked", "source_changed",
  "storage_unavailable", "resource_limit", "worker_failure",
];
const hash = (value: unknown): value is string =>
  typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
const identifier = (value: unknown): value is string =>
  typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
const object = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Malformed managed local model job response.");
  }
  return value as Record<string, unknown>;
};
const timestamp = (value: unknown): boolean =>
  typeof value === "string" && value.length >= 20 && value.length <= 40;

export function parseModelJob(value: unknown): ModelJobSummary {
  const job = object(value);
  if (!identifier(job.id) || !states.includes(job.state as string) ||
    job.status !== (job.state === "running" && job.cancel_requested === 1
      ? "cancel_requested" : job.state) ||
    job.model_id !== "builtin.sma_crossover" || job.model_version !== "1.0.0" ||
    !hash(job.request_sha256) ||
    !Number.isSafeInteger(job.attempts) || (job.attempts as number) < 0 ||
    (job.attempts as number) > 3 || !Number.isSafeInteger(job.progress) ||
    (job.progress as number) < 0 || (job.progress as number) > 100 ||
    ![0, 1].includes(job.cancel_requested as number) ||
    (job.output_sha256 !== null && !hash(job.output_sha256)) ||
    (job.source_snapshot_sha256 !== null && !hash(job.source_snapshot_sha256)) ||
    (job.error_code !== null && !errors.includes(job.error_code as string)) ||
    !timestamp(job.created_at) || !timestamp(job.updated_at) ||
    (job.started_at !== null && !timestamp(job.started_at)) ||
    (job.finished_at !== null && !timestamp(job.finished_at))) {
    throw new Error("Incompatible managed local model job response.");
  }
  return value as ModelJobSummary;
}
async function nativeInvoke(command: string, args: Record<string, unknown>): Promise<unknown> {
  if (!isTauri()) throw new Error("Model jobs require the managed FinIntel desktop.");
  return invoke(command, args);
}
export async function queueModelJob(query: PinnedJobQuery): Promise<ModelJobSummary> {
  validateModelResearchQuery(query);
  if (!hash(query.expectedSnapshot)) throw new Error("Source snapshot pin is required.");
  return parseModelJob(await nativeInvoke("model_jobs_create", {
    code: query.code, start: query.start, end: query.end,
    qualityPolicy: query.qualityPolicy,
    fastWindow: query.fastWindow, slowWindow: query.slowWindow,
    expectedSnapshot: query.expectedSnapshot,
  }));
}
export async function listModelJobs(page = 1): Promise<ModelJobPage> {
  if (!Number.isSafeInteger(page) || page < 1 || page > 1001) {
    throw new Error("Invalid local model job page.");
  }
  const value = object(await nativeInvoke("model_jobs_list", {
    limit: 10, offset: (page - 1) * 10,
  }));
  if (!Number.isSafeInteger(value.total) || (value.total as number) < 0 ||
    !Array.isArray(value.items) || value.items.length > 10 ||
    (value.total as number) < value.items.length) {
    throw new Error("Invalid local model job pagination response.");
  }
  return { total: value.total as number, items: value.items.map(parseModelJob) };
}
export async function getModelJob(id: string): Promise<ModelJobSummary> {
  if (!identifier(id)) throw new Error("Invalid local model job identifier.");
  return parseModelJob(await nativeInvoke("model_jobs_get", {
    jobId: id, includeResult: false,
  }));
}
export async function getModelJobResult(id: string): Promise<ModelJobWithResult> {
  if (!identifier(id)) throw new Error("Invalid local model job identifier.");
  const value = object(await nativeInvoke("model_job_detail", {
    jobId: id, includeResult: true,
  }));
  const job = parseModelJob(value);
  if (job.state !== "completed") throw new Error("Model job has no completed result.");
  const result = object(value.result);
  const lineage = object(result.lineage);
  const parameters = object(lineage.parameters);
  const query: ModelResearchQuery = {
    code: object(lineage.source_identity).code as ModelResearchQuery["code"],
    start: lineage.requested_start as string,
    end: lineage.requested_end as string,
    qualityPolicy: lineage.quality_policy as ModelResearchQuery["qualityPolicy"],
    fastWindow: parameters.fast_window as number,
    slowWindow: parameters.slow_window as number,
    expectedSnapshot: job.source_snapshot_sha256,
  };
  validateModelResearchQuery(query);
  const verified = await parseModelResearchResult(result, query);
  if (verified.output_sha256 !== job.output_sha256 ||
    verified.lineage.source_snapshot_sha256 !== job.source_snapshot_sha256) {
    throw new Error("Model job result is inconsistent with retained source lineage.");
  }
  return { ...job, result: verified };
}
export async function cancelModelJob(id: string): Promise<{ id: string; status: ModelJobStatus }> {
  if (!identifier(id)) throw new Error("Invalid local model job identifier.");
  const result = object(await nativeInvoke("model_jobs_cancel", { jobId: id }));
  if (Object.keys(result).some((key) => key !== "state") ||
    ![...states, "cancel_requested"].includes(result.state as string)) {
    throw new Error("Malformed model job cancellation response.");
  }
  return { id, status: result.state as ModelJobStatus };
}
export async function retryModelJob(id: string): Promise<ModelJobSummary> {
  if (!identifier(id)) throw new Error("Invalid local model job identifier.");
  return parseModelJob(await nativeInvoke("model_jobs_retry", { jobId: id }));
}
