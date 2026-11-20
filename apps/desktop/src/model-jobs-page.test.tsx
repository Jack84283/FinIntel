import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { ModelJobsPage, verifiedJobSnapshot, fetchCurrentModelJobDetails } from "./ModelJobsPage";
import { createModelJobDetailFence } from "./model-job-detail-fence";
import { type AnalyticsResult } from "./index-intelligence-api";
import { type ModelResearchQuery } from "./model-research-api";
import { translate } from "./localization";

const native = vi.hoisted(() => ({ invoke: vi.fn() }));
const jobDetails = vi.hoisted(() => ({ get: vi.fn(), getResult: vi.fn() }));
vi.mock("./model-jobs-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./model-jobs-api")>();
  return { ...actual, getModelJob: jobDetails.get, getModelJobResult: jobDetails.getResult };
});
vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => false,
  invoke: native.invoke,
}));

const query: ModelResearchQuery = {
  code: "SP500", start: "2026-09-01", end: "2026-10-01",
  qualityPolicy: "allow_unverified", fastWindow: 5, slowWindow: 20,
};
const snapshot = "a".repeat(64);
const source = {
  identity: {
    code: "SP500", instrument_kind: "index", provider: "fred",
    adjustment: "price_index", return_semantics: "price_index",
  },
  quality_policy: "allow_unverified",
  observations: 25,
  source: { snapshot_sha256: snapshot },
} as AnalyticsResult;

describe("Phase 2B2 native Model Jobs workspace", () => {
  it("shows explicit source-pinned queue controls and two distinct cards", () => {
    const markup = renderToStaticMarkup(<ModelJobsPage />);
    expect(markup).toContain("Model Jobs");
    expect(markup).toContain("Queue built-in model");
    expect(markup).toContain("Model job history");
    expect(markup).toContain("Durable model jobs require the managed FinIntel desktop.");
    expect(markup.match(/data-slot="card"/g)).toHaveLength(2);
    expect(markup).toContain('aria-labelledby="model-jobs-title"');
    expect(markup).toContain("Filter job status");
    expect(markup).toContain("Queue model job");
    expect(markup).toContain("Start date");
    expect(markup).toContain("End date");
    expect(markup).toContain("SP500");
    expect(markup).not.toContain("VIX");
    expect(native.invoke).not.toHaveBeenCalled();
  });

  it("pins only bounded, compatible, source-backed index history", () => {
    expect(verifiedJobSnapshot(query, source)).toBe(snapshot);
    expect(() => verifiedJobSnapshot(query, {
      ...source, observations: 2001,
    })).toThrow();
    expect(() => verifiedJobSnapshot(query, {
      ...source, observations: 20,
    })).toThrow();
    expect(() => verifiedJobSnapshot(query, {
      ...source, identity: { ...source.identity, code: "DJIA" },
    })).toThrow();
    expect(() => verifiedJobSnapshot(query, {
      ...source, source: { ...source.source, snapshot_sha256: "broken" },
    })).toThrow();
    expect(() => verifiedJobSnapshot(query, {
      ...source, quality_policy: "require_complete",
    })).toThrow();
  });

  it("retains strict, global status filtering and ten-row pagination", () => {
    const page = readFileSync(join(import.meta.dirname, "ModelJobsPage.tsx"), "utf8");
    const app = readFileSync(join(import.meta.dirname, "App.tsx"), "utf8");
    expect(page).toContain("listModelJobs(page + 1, statusFilter)");
    expect(page).toContain("const expectedSnapshot = verifiedJobSnapshot(query, analytics)");
    expect(page).toContain("queueModelJob({ ...query, expectedSnapshot })");
    expect(page).toContain("<TablePager page={page}");
    expect(page).toContain("fetchCurrentModelJobDetails(job.id)");
    expect(page).toContain("switchHistory(event.target.value as ModelJobFilter, 0)");
    expect(page).toContain("if (filter === statusFilter && target === page) return;");
    expect(page).toContain("onPageChange={value => switchHistory(statusFilter, value)}");
    expect(page).toContain("if (page > last) { switchHistory(statusFilter, last); return; }");
    expect(page).toContain("detailFence.isCurrent(job.id, request)");
    expect(page).toContain("generation !== listSerial.current");
    expect(page).toContain('t("Refresh details")');
    expect(page).toContain("cancelModelJob(job.id)");
    expect(page).toContain("retryModelJob(job.id)");
    expect(app).toContain('id: "model-jobs", label: "Model Jobs"');
    expect(app).toContain('screen === "model-jobs"');
  });

  it("refreshes currently queued/running details from ledger and verifies results after completion", async () => {
    const jobId = "123e4567-e89b-42d3-a456-426614174000";
    const queued = { id: jobId, state: "queued", status: "queued" };
    jobDetails.get.mockReset().mockResolvedValue(queued);
    jobDetails.getResult.mockReset();
    const first = await fetchCurrentModelJobDetails(jobId);
    expect(first).toEqual({ job: queued, result: null });
    expect(jobDetails.get).toHaveBeenCalledWith(jobId);
    expect(jobDetails.getResult).not.toHaveBeenCalled();

    // The table row may still say queued, but current ledger state is completed.
    const completed = { ...queued, state: "completed", status: "completed" };
    const verified = { ...completed, result: { observations: 25, points: [] } };
    jobDetails.get.mockResolvedValue(completed);
    jobDetails.getResult.mockResolvedValue(verified);
    const latest = await fetchCurrentModelJobDetails(jobId);
    expect(latest.job).toEqual(verified);
    expect(latest.result).toEqual(verified.result);
    expect(jobDetails.getResult).toHaveBeenCalledWith(jobId);

    jobDetails.getResult.mockRejectedValueOnce(new Error("Output hash mismatch"));
    await expect(fetchCurrentModelJobDetails(jobId)).rejects.toThrow("Output hash mismatch");
  });

  it("fences in-flight detail results across job switches, pagination, filtering and closure", () => {
    const fence = createModelJobDetailFence();
    const first = "123e4567-e89b-42d3-a456-426614174000";
    const second = "123e4567-e89b-42d3-a456-426614174001";
    const requestA = fence.begin(first);
    expect(fence.isCurrent(first, requestA)).toBe(true);

    // Inspecting another job invalidates the old result even before it resolves.
    const requestB = fence.begin(second);
    expect(fence.isCurrent(first, requestA)).toBe(false);
    expect(fence.isCurrent(second, requestB)).toBe(true);

    // The page calls invalidate on history navigation and unmount/close.
    fence.invalidate();
    expect(fence.isCurrent(second, requestB)).toBe(false);
    const nextPageRequest = fence.begin(second);
    expect(fence.isCurrent(second, requestB)).toBe(false);
    expect(fence.isCurrent(second, nextPageRequest)).toBe(true);
    fence.invalidate();
    expect(fence.isCurrent(second, nextPageRequest)).toBe(false);

    // Revisiting the same job must not accept an earlier generation.
    const newRequestA = fence.begin(first);
    expect(fence.isCurrent(first, requestA)).toBe(false);
    expect(fence.isCurrent(first, newRequestA)).toBe(true);
  });

  it("translates job controls, operation errors, and states into Chinese", () => {
    for (const key of [
      "Model Jobs", "Queue model job", "Filter job status", "Refresh details",
      "Model job details", "Model job history unavailable.",
      "Model job could not be queued.", "Model job queued for retry.",
      "cancel_requested", "source_changed", "worker_failure",
    ]) {
      expect(translate("zh-CN", key), key).not.toBe(key);
    }
  });
});
