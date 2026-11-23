import { beforeEach, describe, expect, it, vi } from "vitest";
import { invoke, isTauri } from "@tauri-apps/api/core";
import archived from "../../../contracts/test-fixtures/durable-backtest.json";
import {
  cancelBacktestJob, getBacktestJob, listBacktestJobs, queueBacktestJob, retryBacktestJob, type BacktestJobRequest,
} from "./backtest-jobs-api";
import { canonicalModelSha256 } from "./model-research-integrity";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(), isTauri: vi.fn(() => true) }));
const ipc = vi.mocked(invoke);
const { result, ...summary } = archived;
const queued = { ...summary, state: "queued", status: "queued", attempts: 0, progress: 0,
  output_sha256: null, source_snapshot_sha256: null, started_at: null, finished_at: null };
const request = archived.request as BacktestJobRequest;

beforeEach(() => {
  ipc.mockReset();
  vi.mocked(isTauri).mockReturnValue(true);
});

describe("managed backtest transport", () => {
  it("uses fixed native commands and validates the same Phase 2C1 report as the backend", async () => {
    ipc.mockResolvedValueOnce(queued);
    await expect(queueBacktestJob(request)).resolves.toEqual(queued);
    expect(ipc).toHaveBeenLastCalledWith("backtest_jobs_create", { request });

    ipc.mockResolvedValueOnce({ total: 11, items: [summary] });
    await expect(listBacktestJobs(2, "completed")).resolves.toEqual({ total: 11, items: [summary] });
    expect(ipc).toHaveBeenLastCalledWith("backtest_jobs_list", { limit: 10, offset: 10, status: "completed" });

    ipc.mockResolvedValueOnce(archived);
    await expect(getBacktestJob(archived.id, true)).resolves.toMatchObject({ result });
    expect(ipc).toHaveBeenLastCalledWith("backtest_jobs_get", { jobId: archived.id, includeResult: true });

    ipc.mockResolvedValueOnce({ state: "cancelled" });
    await expect(cancelBacktestJob(archived.id)).resolves.toEqual({ id: archived.id, status: "cancelled" });
    expect(ipc).toHaveBeenLastCalledWith("backtest_jobs_cancel", { jobId: archived.id });

    ipc.mockResolvedValueOnce(queued);
    await expect(retryBacktestJob(archived.id)).resolves.toEqual(queued);
    expect(ipc).toHaveBeenLastCalledWith("backtest_jobs_retry", { jobId: archived.id });
  });

  it("rejects modified output, resigned same-close fills and mismatched requests", async () => {
    const bad = structuredClone(archived);
    bad.result.report.rows[0].equity_after_cost = "1";
    ipc.mockResolvedValueOnce(bad);
    await expect(getBacktestJob(archived.id, true)).rejects.toMatchObject({ code: "invalid_response" });

    const sameClose = structuredClone(archived);
    sameClose.result.report.transactions[0].signal_as_of_session = sameClose.result.report.transactions[0].hypothetical_fill_session;
    const unsigned = { ...sameClose.result.report } as Partial<typeof result.report>;
    delete unsigned.output_sha256;
    sameClose.output_sha256 = sameClose.result.report.output_sha256 = await canonicalModelSha256(unsigned);
    ipc.mockResolvedValueOnce(sameClose);
    await expect(getBacktestJob(archived.id, true)).rejects.toMatchObject({ code: "invalid_response" });

    ipc.mockResolvedValueOnce({ ...queued, request_sha256: "a".repeat(64) });
    await expect(queueBacktestJob(request)).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("rejects unsupported input and unmanaged calls without side effects and sanitizes errors", async () => {
    await expect(queueBacktestJob({ ...request, simulation: { ...request.simulation, test_end: "2026-09-31" } }))
      .rejects.toMatchObject({ code: "invalid_input" });
    await expect(listBacktestJobs(0)).rejects.toMatchObject({ code: "invalid_input" });
    vi.mocked(isTauri).mockReturnValueOnce(false);
    await expect(getBacktestJob(archived.id)).rejects.toMatchObject({ code: "managed_required" });
    expect(ipc).not.toHaveBeenCalled();
    ipc.mockRejectedValueOnce("source_changed").mockRejectedValueOnce("/private/credentials");
    await expect(retryBacktestJob(archived.id)).rejects.toMatchObject({ code: "source_changed" });
    await expect(retryBacktestJob(archived.id)).rejects.toMatchObject({ code: "backend_unavailable" });
  });
});
