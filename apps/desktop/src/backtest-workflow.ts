import { AnalysisJobError, day, hash, identifier } from "./analysis-jobs-api";
import type { BacktestAssumptions, BacktestJobRequest } from "./backtest-jobs-api";
import { SOURCE_CODES, isAnalysisSource } from "./analysis-workflow";
import type { ModelJobSummary } from "./model-jobs-api";
import { BUILTIN_MODEL_ID, BUILTIN_MODEL_VERSION, type ModelResearchResult } from "./model-research-api";

export const MODEL_REVISION = "sma-crossover-observed-close-v1";
const ENGINE_REVISION = "sma-next-observed-close-v1";

export const backtestDisclosure = "Hypothetical historical research only. No real trades or tradable index fills are represented.";

export const backtestFields = [
  { key: "initial_capital", label: "Initial capital", hint: "Greater than 0, up to 1 trillion." },
  { key: "position_fraction", label: "Position fraction", hint: "Greater than 0, up to 1. A value of 1 means full modeled exposure." },
  { key: "commission_bps", label: "Commission (basis points)", hint: "Enter a cost from 0 to 1,000 basis points." },
  { key: "slippage_bps", label: "Slippage (basis points)", hint: "Enter a cost from 0 to 1,000 basis points." },
  { key: "risk_free_rate_annual", label: "Annual risk-free rate", hint: "Enter a fraction from −0.99 to 1. For example, 0.02 is 2%." },
] as const;

export const fixedAssumptionLabels = [
  ["Execution", "Next observed session close"],
  ["Sizing", "Fixed-fraction long or flat"],
  ["Benchmark", "Frictionless buy-and-hold of the same index"],
  ["Rebalance", "When the signal changes"],
  ["Annualization", "252 observed sessions"],
  ["Orders", "Disabled"],
] as const;

export function sourceEligibility(job: ModelJobSummary, result: ModelResearchResult): string | undefined {
  const { lineage, points } = result;
  const identity = lineage.source_identity;
  if (job.state !== "completed" || job.status !== "completed" || result.status !== "completed") {
    return "Select a completed SMA job.";
  }
  if (job.model_id !== BUILTIN_MODEL_ID || job.model_version !== BUILTIN_MODEL_VERSION ||
    lineage.model_id !== BUILTIN_MODEL_ID || lineage.model_version !== BUILTIN_MODEL_VERSION ||
    lineage.implementation_revision !== MODEL_REVISION ||
    !SOURCE_CODES.some(code => code === identity.code) || identity.instrument_kind !== "index" ||
    identity.provider !== "fred" || identity.adjustment !== "price_index" || identity.return_semantics !== "price_index" ||
    identity.frequency !== "daily" || identity.units !== "index_points") {
    return "This model revision or source is ineligible for backtesting.";
  }
  if (lineage.quality_policy !== "require_complete" || lineage.quality_verified !== true || result.quality_warning !== null) {
    return "Backtesting requires an original verified result created under Require complete.";
  }
  if (!identifier(job.id) || !hash(job.request_sha256) || !hash(result.output_sha256) ||
    !hash(result.reproducibility_sha256) || !hash(lineage.source_snapshot_sha256) ||
    job.output_sha256 !== result.output_sha256 || job.source_snapshot_sha256 !== lineage.source_snapshot_sha256 ||
    points.length !== result.observations || points.length > 2000 ||
    points[0]?.session_date !== lineage.first_observed_session ||
    points.at(-1)?.session_date !== lineage.last_observed_session ||
    points.some(({ session_date }, index) => !day(session_date) ||
      session_date < lineage.requested_start || session_date > lineage.requested_end ||
      (index > 0 && session_date <= points[index - 1].session_date))) {
    return "The completed SMA source failed integrity verification.";
  }
  if (!points.some((point, index) => usableBoundary(point, index, result))) {
    return "This SMA result needs an initialized training signal followed by two observed sessions.";
  }
}

function usableBoundary(point: ModelResearchResult["points"][number], index: number, result: ModelResearchResult): boolean {
  return index + 1 >= result.lineage.parameters.slow_window && index + 2 < result.points.length &&
    point.fast_sma !== null && point.slow_sma !== null &&
    (point.signal_after_close === "long" || point.signal_after_close === "flat");
}

export function trainingBoundaries(result: ModelResearchResult): string[] {
  return result.points.filter((point, index) => usableBoundary(point, index, result)).map(point => point.session_date);
}

export function observedBacktestWindow(result: ModelResearchResult, trainingEnd: string, testEnd: string) {
  const trainingIndex = result.points.findIndex(point => point.session_date === trainingEnd);
  const testIndex = trainingIndex + 1;
  const endIndex = result.points.findIndex(point => point.session_date === testEnd);
  if (trainingIndex < 0 || !usableBoundary(result.points[trainingIndex], trainingIndex, result) || endIndex <= testIndex) return;
  return {
    training_end: trainingEnd, test_start: result.points[testIndex].session_date, test_end: testEnd,
    trainingCount: testIndex, testCount: endIndex - testIndex + 1,
  };
}

export function pinnedBacktestRequest(
  job: ModelJobSummary, result: ModelResearchResult,
  window: NonNullable<ReturnType<typeof observedBacktestWindow>>, assumptions: BacktestAssumptions,
): BacktestJobRequest {
  const { lineage } = result;
  const indexCode = lineage.source_identity.code;
  if (!isAnalysisSource(indexCode)) throw new AnalysisJobError("invalid_input");
  return {
    contract_version: "backtest-job-request-v1",
    source_job_id: job.id,
    source_request_sha256: job.request_sha256,
    source_output_sha256: result.output_sha256,
    source_lineage_sha256: result.reproducibility_sha256,
    source_implementation_revision: MODEL_REVISION,
    engine_revision: ENGINE_REVISION,
    simulation: {
      model_id: BUILTIN_MODEL_ID,
      model_version: BUILTIN_MODEL_VERSION,
      index_code: indexCode,
      expected_snapshot_sha256: lineage.source_snapshot_sha256,
      training_end: window.training_end,
      test_start: window.test_start,
      test_end: window.test_end,
      source_semantics: "fred_price_index_level",
      point_in_time_membership: "not_used",
      assumptions,
    },
  };
}

export function backtestError(code: unknown): string {
  const safe = code instanceof AnalysisJobError ? code.code : code;
  switch (safe) {
    case "managed_required": return "Backtesting requires the managed FinIntel desktop.";
    case "invalid_input": return "Review the source, dates and assumptions before queueing.";
    case "source_changed": return "The original source snapshot no longer matches the local data.";
    case "quality_blocked": return "The original or current source quality requirements are not met.";
    case "resource_limit": return "The backtest exceeds local resource limits.";
    case "queue_full": return "The local job queue is full. Wait for a job to finish.";
    case "job_conflict": return "The job changed. Refresh its details before trying again.";
    case "storage_unavailable": return "Local backtest storage is unavailable.";
    case "invalid_response": return "The saved backtest failed integrity verification.";
    case "job_not_found": return "This backtest job was not found.";
    case "interrupted": return "The local worker stopped before the backtest finished.";
    case "worker_failure": return "The isolated worker could not finish the backtest.";
    default: return "The local backtesting service is unavailable.";
  }
}
