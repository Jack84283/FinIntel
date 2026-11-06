import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  cancelModelJob, getModelJob, listModelJobs, parseModelJob,
  queueModelJob, retryModelJob,
} from "./model-jobs-api";

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
