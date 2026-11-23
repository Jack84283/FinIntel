import { beforeEach, describe, expect, it, vi } from "vitest";
import { invoke, isTauri } from "@tauri-apps/api/core";
import contracts from "../../../contracts/analysis-jobs.json";
import archived from "../../../contracts/test-fixtures/analysis-relative-strength.json";
import {
  cancelAnalysisJob, getAnalysisJob, listAnalysisJobs, queueAnalysisJob, retryAnalysisJob,
  type AnalysisJobRequest, type AnalysisModelId,
} from "./analysis-jobs-api";
import { canonicalModelSha256 } from "./model-research-integrity";

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(),
  isTauri: vi.fn(() => true),
}));
const client = vi.mocked(invoke);
const managed = vi.mocked(isTauri);
const { result: archivedResult, ...archivedSummary } = archived;
const id = archived.id;
const queued = {
  ...archivedSummary,
  state: "queued", status: "queued", attempts: 0, progress: 0,
  output_sha256: null, started_at: null, finished_at: null,
};
const submission = {
  ...archived.request, implementation_revision: archived.implementation_revision,
} as AnalysisJobRequest;

beforeEach(() => {
  client.mockReset();
  managed.mockReturnValue(true);
});

describe("Analysis Library transport", () => {
  it("invokes only the fixed create, list, detail, cancel and retry commands", async () => {
    client.mockResolvedValueOnce(queued);
    await expect(queueAnalysisJob(submission)).resolves.toEqual(queued);
    expect(client).toHaveBeenLastCalledWith("analysis_jobs_create", { request: submission });

    client.mockResolvedValueOnce({ total: 11, items: [archivedSummary] });
    await expect(listAnalysisJobs(2, "completed")).resolves.toEqual({ total: 11, items: [archivedSummary] });
    expect(client).toHaveBeenLastCalledWith("analysis_jobs_list", { limit: 10, offset: 10, status: "completed" });

    client.mockResolvedValueOnce(archivedSummary);
    await expect(getAnalysisJob(id)).resolves.toEqual(archivedSummary);
    expect(client).toHaveBeenLastCalledWith("analysis_jobs_get", { jobId: id, includeResult: false });

    client.mockResolvedValueOnce(archived);
    await expect(getAnalysisJob(id, true)).resolves.toEqual(archived);
    expect(client).toHaveBeenLastCalledWith("analysis_jobs_get", { jobId: id, includeResult: true });

    client.mockResolvedValueOnce({ ...queued, result: null });
    await expect(getAnalysisJob(id, true)).resolves.toMatchObject({ state: "queued", result: null });

    client.mockResolvedValueOnce({ state: "cancelled" });
    await expect(cancelAnalysisJob(id)).resolves.toEqual({ id, status: "cancelled" });
    expect(client).toHaveBeenLastCalledWith("analysis_jobs_cancel", { jobId: id });

    client.mockResolvedValueOnce(queued);
    await expect(retryAnalysisJob(id)).resolves.toEqual(queued);
    expect(client).toHaveBeenLastCalledWith("analysis_jobs_retry", { jobId: id });
  });

  it("rejects invalid pagination, unsafe IDs, missing pins, and unmanaged invocation before side effects", async () => {
    for (const page of [0, 1002, 1.5, NaN]) {
      await expect(listAnalysisJobs(page)).rejects.toMatchObject({ code: "invalid_input" });
    }
    await expect(getAnalysisJob("../anything")).rejects.toMatchObject({ code: "invalid_input" });
    await expect(getAnalysisJob(id + "\n")).rejects.toMatchObject({ code: "invalid_input" });
    await expect(queueAnalysisJob({ ...submission, sources: [] })).rejects.toMatchObject({ code: "invalid_input" });
    await expect(queueAnalysisJob({ ...submission, sources: [submission.sources[0], submission.sources[0]] }))
      .rejects.toMatchObject({ code: "invalid_input" });
    managed.mockReturnValue(false);
    await expect(listAnalysisJobs()).rejects.toMatchObject({ code: "managed_required" });
    expect(client).not.toHaveBeenCalled();
  });

  it("returns only stable errors and rejects malformed summary and pagination", async () => {
    client.mockRejectedValueOnce("source_changed");
    await expect(retryAnalysisJob(id)).rejects.toMatchObject({ code: "source_changed" });
    client.mockRejectedValueOnce("path: /private/secret");
    await expect(retryAnalysisJob(id)).rejects.toMatchObject({ code: "backend_unavailable" });
    client.mockResolvedValueOnce({ total: 2, items: [archivedSummary] });
    await expect(listAnalysisJobs()).rejects.toMatchObject({ code: "invalid_response" });
    client.mockResolvedValueOnce({ total: 2, items: [archivedSummary, archivedSummary] });
    await expect(listAnalysisJobs()).rejects.toMatchObject({ code: "invalid_response" });
    client.mockResolvedValueOnce({ ...archivedSummary, request_sha256: "f".repeat(64) });
    await expect(getAnalysisJob(id)).rejects.toMatchObject({ code: "invalid_response" });
    client.mockResolvedValueOnce({ ...archivedSummary, id: "123e4567-e89b-12d3-a456-426614174001" });
    await expect(getAnalysisJob(id)).rejects.toMatchObject({ code: "invalid_response" });
    client.mockResolvedValueOnce({ state: "unknown" });
    await expect(cancelAnalysisJob(id)).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("rejects reordered source evidence even when its SHA-256 is valid", async () => {
    const evidence = { ...archivedResult.evidence, sources: [...archivedResult.evidence.sources].reverse() };
    client.mockResolvedValueOnce({
      ...archived, result: {
        ...archivedResult, evidence, reproducibility_sha256: await canonicalModelSha256(evidence),
      },
    });
    await expect(getAnalysisJob(id, true)).rejects.toMatchObject({ code: "invalid_response" });

    client.mockResolvedValueOnce({ ...archived, source_snapshot_sha256: archived.request.sources[0].snapshot_sha256 });
    await expect(getAnalysisJob(id, true)).rejects.toMatchObject({ code: "invalid_response" });

    const sources = structuredClone(archivedResult.evidence.sources);
    sources[0].quality.warnings = [];
    const unverified = { ...archivedResult.evidence, sources };
    client.mockResolvedValueOnce({
      ...archived, result: {
        ...archivedResult, evidence: unverified, reproducibility_sha256: await canonicalModelSha256(unverified),
      },
    });
    await expect(getAnalysisJob(id, true)).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("validates the exact submitted snapshot even if the caller mutates during invocation", async () => {
    const caller = structuredClone(submission);
    let resolveResponse!: (value: unknown) => void;
    let invoked!: () => void;
    const reachedIPC = new Promise<void>(resolve => { invoked = resolve; });
    client.mockImplementationOnce(() => new Promise(resolve => {
      resolveResponse = resolve;
      invoked();
    }));
    const pending = queueAnalysisJob(caller);
    await reachedIPC;
    caller.sources[0].snapshot_sha256 = "a".repeat(64);
    caller.window.start = "2024-01-01";
    resolveResponse(queued);
    await expect(pending).resolves.toEqual(queued);
    expect(client).toHaveBeenCalledWith("analysis_jobs_create", { request: submission });
  });

  it("rejects a valid but different pinned request from the create response", async () => {
    const request = { ...queued.request, sources: [...queued.request.sources].reverse() };
    client.mockResolvedValueOnce({ ...queued, request, request_sha256: await canonicalModelSha256(request) });
    await expect(queueAnalysisJob(submission)).rejects.toMatchObject({ code: "invalid_response" });
  });
});

type Point = Record<string, string | number | boolean | string[] | null>;

async function completed(modelId: AnalysisModelId) {
  const model = contracts[modelId];
  const sourceCount = model.min_sources;
  const parameters = Object.fromEntries(Object.entries(model.parameters).map(([key, rule]) => [key, rule.default]));
  if (modelId === "builtin.ema_trend") {
    parameters.fast_window = 1;
    parameters.slow_window = 2;
  }
  if (modelId === "builtin.momentum") parameters.lookback_sessions = 21;
  const size = modelId === "builtin.momentum" ? 22 : 2;
  const sessions = Array.from({ length: size }, (_, i) => `2024-01-${String(i + 2).padStart(2, "0")}`);
  const window = { start: sessions[0], end: sessions[size - 1] };
  const modelIdentity = {
    model_id: modelId, model_version: model.version, implementation_revision: model.revision,
  };
  const pins = archived.request.sources.slice(0, sourceCount);
  const request = {
    ...archived.request, model_id: modelId, sources: pins, window, parameters,
  };
  const evidence = {
    ...archivedResult.evidence,
    model: modelIdentity,
    calculation_version: model.calculation,
    sources: pins.map(pin => ({
      ...pin, observed: { ...window, count: size }, aligned_count: size, excluded_count: 0,
      quality: { status: "verified", warnings: [] },
    })),
    requested: window,
    observed_sessions: sessions,
    parameters,
    output_fields: model.output_fields,
  };
  const points = sessions.map(session => {
    const point: Point = {};
    for (const field of model.output_fields) {
      if (field.name === "session_date") point[field.name] = session;
      else if (field.name === "benchmark") point[field.name] = "ndx";
      else if (field.name === "lookback_sessions") point[field.name] = 21;
      else if (field.name === "lower_threshold") point[field.name] = 30;
      else if (field.name === "upper_threshold") point[field.name] = 70;
      else if (modelId === "builtin.relative_strength" && field.kind === "decimal" &&
        pins.some(pin => field.name.startsWith(pin.identity.code.toLowerCase() + "_"))) point[field.name] = "1";
      else if (field.nullable) point[field.name] = null;
      else if (field.kind === "decimal") point[field.name] = "1";
      else if (field.kind === "enum") point[field.name] = field.allowed_values[0];
      else if (field.kind === "integer") point[field.name] = 0;
      else if (field.kind === "boolean") point[field.name] = false;
      else point[field.name] = [];
    }
    return point;
  });
  const output_sha256 = await canonicalModelSha256(points);
  return {
    ...archivedSummary, ...modelIdentity, request,
    request_sha256: await canonicalModelSha256(request), output_sha256,
    source_snapshot_sha256: sourceCount === 1 ? pins[0].snapshot_sha256 : null,
    result: {
      ...archivedResult, evidence, points, output_sha256,
      reproducibility_sha256: await canonicalModelSha256(evidence),
    },
  };
}

describe("model-specific result contracts", () => {
  it.each([
    ["builtin.ema_trend", "fast_ema", "0"],
    ["builtin.momentum", "lookback_sessions", 63],
    ["builtin.relative_strength", "benchmark", "sp500"],
    ["builtin.rsi_state", "rsi_state", "unknown"],
    ["builtin.risk_profile", "historical_tail_count", "60"],
    ["builtin.market_regime", "reason_codes", ["unknown"]],
  ] as const)("validates %s points independently of their output hash", async (model, field, invalid) => {
    const response = await completed(model);
    client.mockResolvedValueOnce(response);
    await expect(getAnalysisJob(id, true)).resolves.toMatchObject({ model_id: model });

    const points = structuredClone(response.result.points);
    points[0][field] = Array.isArray(invalid) ? [...invalid] : invalid as string | number;
    const output_sha256 = await canonicalModelSha256(points);
    client.mockResolvedValueOnce({
      ...response, output_sha256, result: { ...response.result, points, output_sha256 },
    });
    await expect(getAnalysisJob(id, true)).rejects.toMatchObject({ code: "invalid_response" });
  });

  it.each([
    ["builtin.relative_strength", "ndx_normalized_100", null],
    ["builtin.relative_strength", "djia_relative_strength", "1"],
    ["builtin.risk_profile", "comparison_return", "0.1"],
    ["builtin.risk_profile", "correlation", "1"],
  ] as const)("rejects %s output attributed to the wrong source: %s", async (model, field, invalid) => {
    const response = await completed(model);
    response.result.points[1][field] = invalid;
    response.output_sha256 = response.result.output_sha256 = await canonicalModelSha256(response.result.points);
    client.mockResolvedValueOnce(response);
    await expect(getAnalysisJob(id, true)).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("rejects altered outputs, registered field metadata, and parameter identities", async () => {
    const output = await completed("builtin.ema_trend");
    output.result.points[0].close = "123";
    client.mockResolvedValueOnce(output);
    await expect(getAnalysisJob(id, true)).rejects.toMatchObject({ code: "invalid_response" });

    for (const mutate of [
      (evidence: typeof output.result.evidence) => evidence.parameters.fast_window = 999,
      (evidence: typeof output.result.evidence) => evidence.output_fields[0].units = "invented_unit",
    ]) {
      const response = await completed("builtin.ema_trend");
      response.result.evidence = structuredClone(response.result.evidence);
      mutate(response.result.evidence);
      response.result.reproducibility_sha256 = await canonicalModelSha256(response.result.evidence);
      client.mockResolvedValueOnce(response);
      await expect(getAnalysisJob(id, true)).rejects.toMatchObject({ code: "invalid_response" });
    }
  });
});
