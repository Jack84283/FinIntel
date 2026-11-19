import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  cancelModelJob, getModelJob, getModelJobResult, listModelJobs, parseModelJob,
  queueModelJob, retryModelJob,
} from "./model-jobs-api";
import { canonicalModelSha256 } from "./model-research-integrity";
import { type ModelResearchResult } from "./model-research-api";

const native = vi.hoisted(() => ({ enabled: true, invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => native.enabled,
  invoke: native.invoke,
}));

const id = "123e4567-e89b-42d3-a456-426614174000";
const sample = () => ({
  id, state: "queued", status: "queued",
  model_id: "builtin.sma_crossover", model_version: "1.0.0",
  request_sha256: "a".repeat(64), attempts: 0, progress: 0,
  cancel_requested: 0, output_sha256: null, source_snapshot_sha256: null,
  error_code: null, created_at: "2026-11-05T00:00:00+00:00",
  updated_at: "2026-11-05T00:00:00+00:00",
  started_at: null, finished_at: null,
});
beforeEach(() => { native.enabled = true; native.invoke.mockReset(); });

describe("fixed managed durable-model-job IPC", () => {
  it("requires source-pinned built-in enqueue and never fetches an HTTP endpoint", async () => {
    native.invoke.mockResolvedValue(sample());
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const query = {
      code: "SP500" as const,
      start: "2026-09-01", end: "2026-10-01",
      qualityPolicy: "allow_unverified" as const,
      fastWindow: 5, slowWindow: 20,
      expectedSnapshot: "b".repeat(64),
    };
    expect((await queueModelJob(query)).state).toBe("queued");
    expect(native.invoke).toHaveBeenCalledWith("model_jobs_create", {
      code: "SP500", start: query.start, end: query.end,
      qualityPolicy: query.qualityPolicy, fastWindow: 5, slowWindow: 20,
      expectedSnapshot: query.expectedSnapshot,
    });
    await expect(queueModelJob({ ...query, expectedSnapshot: "invalid" })).rejects.toThrow();
    await expect(queueModelJob({ ...query, code: "VIX" })).rejects.toThrow();
    expect(fetcher).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("checks page size, UUIDs, and rejects forged response metadata", async () => {
    native.invoke.mockResolvedValue({ total: 11, items: [sample()] });
    expect((await listModelJobs(2)).total).toBe(11);
    expect(native.invoke).toHaveBeenCalledWith("model_jobs_list", {
      limit: 10, offset: 10,
    });
    await expect(listModelJobs(1002)).rejects.toThrow();
    native.invoke.mockResolvedValue({ total: 1, items: [sample()] });
    expect((await listModelJobs(1, "queued")).total).toBe(1);
    expect(native.invoke).toHaveBeenCalledWith("model_jobs_list", {
      limit: 10, offset: 0, status: "queued",
    });
    await expect(listModelJobs(1, "invalid" as "queued")).rejects.toThrow("filter");
    await expect(getModelJob("../private")).rejects.toThrow();
    expect(() => parseModelJob({ ...sample(), model_id: "custom.secret" })).toThrow();
    expect(() => parseModelJob({ ...sample(), status: "completed" })).toThrow();
    expect(() => parseModelJob({
      ...sample(), state: "running", status: "cancel_requested",
      cancel_requested: 1, attempts: 1,
    })).not.toThrow();
    native.invoke.mockResolvedValue(sample());
    expect((await getModelJob(id)).id).toBe(id);
  });

  it("cancels and retries only by validated UUID on fixed commands", async () => {
    native.invoke.mockResolvedValueOnce({ state: "cancelled" });
    expect((await cancelModelJob(id)).status).toBe("cancelled");
    expect(native.invoke).toHaveBeenCalledWith("model_jobs_cancel", { jobId: id });
    native.invoke.mockResolvedValueOnce(sample());
    expect((await retryModelJob(id)).state).toBe("queued");
    expect(native.invoke).toHaveBeenCalledWith("model_jobs_retry", { jobId: id });
    await expect(retryModelJob("http://localhost:8765/evil")).rejects.toThrow();
    native.enabled = false;
    await expect(getModelJob(id)).rejects.toThrow("managed FinIntel desktop");
  });
});

async function verifiedCompletedJob(): Promise<Record<string, unknown>> {
  const points: ModelResearchResult["points"] = Array.from({ length: 25 }, (_, index) => ({
    session_date: new Date(Date.UTC(2026, 8, index + 1)).toISOString().slice(0, 10),
    close: "100",
    fast_sma: index >= 4 ? "100" : null,
    slow_sma: index >= 19 ? "100" : null,
    signal_after_close: index >= 19 ? "flat" : null,
  }));
  const lineage: ModelResearchResult["lineage"] = {
    model_id: "builtin.sma_crossover", model_version: "1.0.0",
    implementation_revision: "sma-crossover-observed-close-v1",
    parameters: { fast_window: 5, slow_window: 20 },
    source_identity: {
      instrument_kind: "index", code: "SP500", provider: "fred",
      adjustment: "price_index", return_semantics: "price_index",
      frequency: "daily", units: "index_points",
    },
    source_snapshot_sha256: "a".repeat(64),
    analysis_calculation_version: "index-analytics-v2",
    requested_start: "2026-09-01", requested_end: "2026-09-30",
    first_observed_session: points[0].session_date,
    last_observed_session: points.at(-1)!.session_date,
    quality_policy: "allow_unverified", quality_status: "verified",
    quality_verified: true, point_in_time_membership: "not_used",
    dividends_included: false, corporate_action_adjustment: "none_index_price_level",
  };
  const output_sha256 = await canonicalModelSha256(points);
  const result: ModelResearchResult = {
    execution_contract: "model-research-evaluation-v1", lineage,
    reproducibility_sha256: await canonicalModelSha256(lineage),
    output_sha256, evaluated_at_utc: "2026-11-09T00:00:00+00:00",
    status: "completed", observations: 25, points, quality_warning: null,
    read_only: true, no_network: true, no_synthetic_sessions: true,
    signal_after_close_only: true, backtest_validated: false, persistent_job: false,
  };
  return {
    ...sample(), state: "completed", status: "completed",
    attempts: 1, source_snapshot_sha256: lineage.source_snapshot_sha256,
    output_sha256, result,
  };
}

describe("completed model job result retrieval", () => {
  it("uses registered model_jobs_get IPC and validates persisted result lineage", async () => {
    const payload = await verifiedCompletedJob();
    native.invoke.mockResolvedValue(payload);
    const job = await getModelJobResult(id);
    expect(native.invoke).toHaveBeenCalledExactlyOnceWith("model_jobs_get", {
      jobId: id, includeResult: true,
    });
    expect(job.result.points).toHaveLength(25);
    expect(job.result.lineage.source_snapshot_sha256).toBe("a".repeat(64));
  });

  it("refuses non-completed or corrupted results rather than exposing unverified output", async () => {
    native.invoke.mockResolvedValue({ ...sample(), result: null });
    await expect(getModelJobResult(id)).rejects.toThrow("no completed result");
    const valid = await verifiedCompletedJob();
    native.invoke.mockResolvedValue({ ...valid, output_sha256: "f".repeat(64) });
    await expect(getModelJobResult(id)).rejects.toThrow("inconsistent");
    native.invoke.mockResolvedValue({
      ...valid,
      result: { ...(valid.result as ModelResearchResult), points: [] },
    });
    await expect(getModelJobResult(id)).rejects.toThrow();
    await expect(getModelJobResult("../private")).rejects.toThrow("identifier");
  });
});
