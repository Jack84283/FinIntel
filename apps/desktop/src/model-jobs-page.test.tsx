import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { ModelJobsPage, verifiedJobSnapshot } from "./ModelJobsPage";
import { type AnalyticsResult } from "./index-intelligence-api";
import { type ModelResearchQuery } from "./model-research-api";
import { translate } from "./localization";

const native = vi.hoisted(() => ({ invoke: vi.fn() }));
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
    expect(page).toContain("getModelJobResult(job.id)");
    expect(page).toContain("cancelModelJob(job.id)");
    expect(page).toContain("retryModelJob(job.id)");
    expect(app).toContain('id: "model-jobs", label: "Model Jobs"');
    expect(app).toContain('screen === "model-jobs"');
  });

  it("translates job controls, operation errors, and states into Chinese", () => {
    for (const key of [
      "Model Jobs", "Queue model job", "Filter job status",
      "Model job details", "Model job history unavailable.",
      "Model job could not be queued.", "Model job queued for retry.",
      "cancel_requested", "source_changed", "worker_failure",
    ]) {
      expect(translate("zh-CN", key), key).not.toBe(key);
    }
  });
});
