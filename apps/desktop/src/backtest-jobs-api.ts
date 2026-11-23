/** Typed native-only access to isolated, source-pinned backtesting jobs. */
import { invoke, isTauri } from "@tauri-apps/api/core";
import {
  AnalysisJobError, contains, day, hash, identifier, integer, object, requireValid, states, statuses,
  timestamp, transportCodes, workerCodes, type AnalysisSourceCode, type AnalysisJobFilter,
} from "./analysis-jobs-api";
import type { ModelJobStatus, ModelJobSummary } from "./model-jobs-api";
import { canonicalModelSha256 } from "./model-research-integrity";

const ENGINE = "sma-next-observed-close-v1";
const SIMULATOR = "local-backtest-research-v1";
const MODEL = "builtin.sma_crossover";
const REVISION = "sma-crossover-observed-close-v1";
const CAUTION = "Hypothetical FRED index-price-level exposure, not a tradable fill or dividend-inclusive return; benchmark ignores all transaction costs.";

export type BacktestAssumptions = {
  initial_capital: string;
  position_fraction: string;
  commission_bps: string;
  slippage_bps: string;
  risk_free_rate_annual: string;
  execution: "next_observed_session_close";
  sizing: "fixed_fraction_long_or_flat";
  benchmark: "frictionless_same_index_buy_hold";
  rebalance: "on_signal_change";
  annualization_sessions: 252;
  no_orders: true;
};

export const defaultBacktestAssumptions: Readonly<BacktestAssumptions> = {
  initial_capital: "10000", position_fraction: "1", commission_bps: "0", slippage_bps: "0",
  risk_free_rate_annual: "0", execution: "next_observed_session_close", sizing: "fixed_fraction_long_or_flat",
  benchmark: "frictionless_same_index_buy_hold", rebalance: "on_signal_change", annualization_sessions: 252, no_orders: true,
};

export type BacktestJobRequest = {
  contract_version: "backtest-job-request-v1";
  source_job_id: string;
  source_request_sha256: string;
  source_output_sha256: string;
  source_lineage_sha256: string;
  source_implementation_revision: typeof REVISION;
  engine_revision: typeof ENGINE;
  simulation: {
    model_id: typeof MODEL;
    model_version: "1.0.0";
    index_code: AnalysisSourceCode;
    expected_snapshot_sha256: string;
    training_end: string;
    test_start: string;
    test_end: string;
    source_semantics: "fred_price_index_level";
    point_in_time_membership: "not_used";
    assumptions: BacktestAssumptions;
  };
};

export type BacktestJobSummary = ModelJobSummary & { request: BacktestJobRequest };
export type BacktestJobDetail = BacktestJobSummary & { result: BacktestJobResult | null };
export type BacktestJobPage = { items: BacktestJobSummary[]; total: number };

const equityFields = [
  "equity_after_cost", "frictionless_benchmark_equity", "exposure_after_close", "transaction_cost", "drawdown",
] as const;
const transactionFields = ["exposure_before", "exposure_after", "turnover_fraction", "cost"] as const;
const metricFields = [
  "cumulative_return", "annualized_return", "max_drawdown", "annualized_volatility", "risk_free_rate_annual",
  "turnover_fraction", "transaction_cost_total", "frictionless_benchmark_cumulative_return", "relative_to_frictionless_benchmark",
] as const;

export type BacktestEquityRow = Record<typeof equityFields[number], string> & {
  session_date: string; executed_from_prior_signal: string;
};
export type BacktestTransaction = Record<typeof transactionFields[number], string> & {
  signal_as_of_session: string; hypothetical_fill_session: string;
};
export type BacktestMetrics = Record<typeof metricFields[number], string> & { sharpe: string | null; transactions: number };
export type BacktestReport = {
  contract: typeof SIMULATOR;
  engine_revision: typeof ENGINE;
  model_id: typeof MODEL;
  model_version: "1.0.0";
  input_sha256: string;
  output_sha256: string;
  source_snapshot_sha256: string;
  source_semantics: "fred_price_index_level";
  point_in_time_membership: "not_used";
  signal_timing: "after_observed_close";
  execution_assumption: "next_observed_session_close";
  tradeable_instrument: false;
  research_only: true;
  no_network: true;
  no_orders: true;
  persisted: false; // Original pure engine report, retained inside the durable envelope.
  caution: typeof CAUTION;
  test_sessions: number;
  rows: BacktestEquityRow[];
  transactions: BacktestTransaction[];
  metrics: BacktestMetrics;
};
export type BacktestJobResult = {
  contract_version: "backtest-job-result-v1";
  request_sha256: string;
  evaluated_at_utc: string;
  report: BacktestReport;
};

const requestKeys = [
  "contract_version", "source_job_id", "source_request_sha256", "source_output_sha256",
  "source_lineage_sha256", "source_implementation_revision", "engine_revision", "simulation",
];
const summaryKeys = [
  "id", "state", "status", "model_id", "model_version", "request", "request_sha256", "attempts",
  "progress", "cancel_requested", "output_sha256", "source_snapshot_sha256", "error_code",
  "created_at", "updated_at", "started_at", "finished_at",
];
const assumptionBounds = {
  initial_capital: [1n, 1_000_000_000_000_000_000n], position_fraction: [1n, 1_000_000n],
  commission_bps: [0n, 1_000_000_000n], slippage_bps: [0n, 1_000_000_000n],
  risk_free_rate_annual: [-990_000n, 1_000_000n],
} as const;

const decimal = (v: unknown): v is string =>
  typeof v === "string" && v.length <= 512 && /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(v) && v.trim() === v;

export type BacktestDecimalField = keyof typeof assumptionBounds;

export function validBacktestDecimal(key: BacktestDecimalField, value: unknown): boolean {
  if (!decimal(value) || value.length > 20) return false;
  const [whole, fraction = ""] = value.split(".");
  if (fraction.length > 6) return false;
  const scaled = BigInt(whole + fraction.padEnd(6, "0"));
  const [min, max] = assumptionBounds[key];
  return scaled >= min && scaled <= max;
}

function checkedRequest(value: unknown): BacktestJobRequest {
  const v = object(value, requestKeys);
  requireValid(v.contract_version === "backtest-job-request-v1" && identifier(v.source_job_id) &&
    hash(v.source_request_sha256) && hash(v.source_output_sha256) && hash(v.source_lineage_sha256) &&
    v.source_implementation_revision === REVISION && v.engine_revision === ENGINE);
  const s = object(v.simulation, [
    "model_id", "model_version", "index_code", "expected_snapshot_sha256", "training_end", "test_start",
    "test_end", "source_semantics", "point_in_time_membership", "assumptions",
  ]);
  requireValid(s.model_id === MODEL && s.model_version === "1.0.0" && contains(["SP500", "DJIA", "NASDAQCOM", "NDX"], s.index_code) &&
    hash(s.expected_snapshot_sha256) && day(s.training_end) && day(s.test_start) && day(s.test_end) &&
    s.training_end < s.test_start && s.test_start < s.test_end && s.source_semantics === "fred_price_index_level" &&
    s.point_in_time_membership === "not_used");
  const assumptions = object(s.assumptions, Object.keys(defaultBacktestAssumptions));
  for (const key of Object.keys(assumptionBounds) as BacktestDecimalField[]) {
    requireValid(validBacktestDecimal(key, assumptions[key]));
  }
  for (const [key, val] of Object.entries(defaultBacktestAssumptions)) {
    if (!Object.hasOwn(assumptionBounds, key)) requireValid(assumptions[key] === val);
  }
  return v as unknown as BacktestJobRequest;
}

async function checkReport(raw: unknown, job: BacktestJobSummary): Promise<void> {
  const v = object(raw, ["contract_version", "request_sha256", "evaluated_at_utc", "report"]);
  requireValid(v.contract_version === "backtest-job-result-v1" && timestamp(v.evaluated_at_utc) &&
    v.request_sha256 === job.request_sha256);
  const r = object(v.report, [
    "contract", "engine_revision", "model_id", "model_version", "input_sha256", "output_sha256",
    "source_snapshot_sha256", "source_semantics", "point_in_time_membership", "signal_timing",
    "execution_assumption", "tradeable_instrument", "research_only", "no_network", "no_orders",
    "persisted", "caution", "test_sessions", "rows", "transactions", "metrics",
  ]);
  requireValid(r.contract === SIMULATOR && r.engine_revision === ENGINE && r.model_id === MODEL &&
    r.model_version === "1.0.0" && r.output_sha256 === job.output_sha256 &&
    r.source_snapshot_sha256 === job.source_snapshot_sha256 && r.source_semantics === "fred_price_index_level" &&
    r.point_in_time_membership === "not_used" && r.signal_timing === "after_observed_close" &&
    r.execution_assumption === "next_observed_session_close" && r.tradeable_instrument === false &&
    r.research_only === true && r.no_network === true && r.no_orders === true && r.persisted === false &&
    r.caution === CAUTION && integer(r.test_sessions, 2, 2000) && Array.isArray(r.rows) &&
    r.rows.length === r.test_sessions && Array.isArray(r.transactions) && r.transactions.length <= r.rows.length);
  const simulation = job.request.simulation;
  const fills = new Map<string, string>();
  let previous = simulation.training_end;
  for (const [i, rawRow] of r.rows.entries()) {
    const row = object(rawRow, ["session_date", "executed_from_prior_signal", ...equityFields]);
    requireValid(day(row.session_date) && row.session_date > previous && row.executed_from_prior_signal === previous &&
      (i > 0 || row.session_date === simulation.test_start) && equityFields.every(key => decimal(row[key])));
    fills.set(row.session_date, previous);
    previous = row.session_date;
  }
  requireValid(previous === simulation.test_end);
  previous = "";
  for (const rawTx of r.transactions) {
    const tx = object(rawTx, ["signal_as_of_session", "hypothetical_fill_session", ...transactionFields]);
    requireValid(day(tx.hypothetical_fill_session) && day(tx.signal_as_of_session) &&
      tx.hypothetical_fill_session > previous && fills.get(tx.hypothetical_fill_session) === tx.signal_as_of_session &&
      transactionFields.every(key => decimal(tx[key])));
    previous = tx.hypothetical_fill_session;
  }
  const metrics = object(r.metrics, [...metricFields, "sharpe", "transactions"]);
  requireValid(metricFields.every(key => decimal(metrics[key])) &&
    (metrics.sharpe === null || decimal(metrics.sharpe)) && metrics.transactions === r.transactions.length);
  const { output_sha256, ...report } = r;
  const [inputHash, outputHash] = await Promise.all([
    canonicalModelSha256({
      backtest_contract: SIMULATOR, engine_revision: ENGINE, request: simulation,
      model_result_sha256: job.request.source_output_sha256, model_lineage_sha256: job.request.source_lineage_sha256,
    }),
    canonicalModelSha256(report),
  ]);
  requireValid(r.input_sha256 === inputHash && output_sha256 === outputHash);
}

async function parseSummary(raw: unknown, expected?: string, include = false): Promise<BacktestJobSummary> {
  const v = object(raw, include ? [...summaryKeys, "result"] : summaryKeys);
  const request = checkedRequest(v.request);
  requireValid(identifier(v.id) && (expected === undefined || v.id === expected) && contains(states, v.state) &&
    v.model_id === MODEL && v.model_version === "1.0.0" && integer(v.attempts, 0, 3) &&
    integer(v.progress, 0, 100) && integer(v.cancel_requested, 0, 1) &&
    v.status === (v.state === "running" && v.cancel_requested === 1 ? "cancel_requested" : v.state) &&
    (v.error_code === null || contains(workerCodes, v.error_code)) &&
    (v.state === "failed") === (v.error_code !== null) && timestamp(v.created_at) && timestamp(v.updated_at) &&
    (v.started_at === null || timestamp(v.started_at)) && (v.finished_at === null || timestamp(v.finished_at)) &&
    v.request_sha256 === await canonicalModelSha256(request));
  const job = v as unknown as BacktestJobSummary;
  if (job.state === "completed") {
    requireValid(job.progress === 100 && hash(job.output_sha256) &&
      job.source_snapshot_sha256 === request.simulation.expected_snapshot_sha256);
    if (include) await checkReport(v.result, job);
  } else {
    requireValid(job.output_sha256 === null && job.source_snapshot_sha256 === null && (!include || v.result === null));
  }
  return job;
}

type Command = "backtest_jobs_create" | "backtest_jobs_list" | "backtest_jobs_get" |
  "backtest_jobs_cancel" | "backtest_jobs_retry";

async function native(command: Command, args: Record<string, unknown>): Promise<unknown> {
  requireValid(isTauri(), "managed_required");
  try {
    return await invoke(command, args);
  } catch (error) {
    throw new AnalysisJobError(contains(transportCodes, error) ? error : "backend_unavailable");
  }
}

export async function queueBacktestJob(input: BacktestJobRequest): Promise<BacktestJobSummary> {
  let request: BacktestJobRequest;
  try {
    request = checkedRequest(structuredClone(input));
  } catch {
    throw new AnalysisJobError("invalid_input");
  }
  const expected = await canonicalModelSha256(request);
  const job = await parseSummary(await native("backtest_jobs_create", { request }));
  requireValid(job.request_sha256 === expected && job.state === "queued" && job.attempts === 0);
  return job;
}

export async function listBacktestJobs(page = 1, filter: AnalysisJobFilter = "all"): Promise<BacktestJobPage> {
  requireValid(integer(page, 1, 1001) && (filter === "all" || contains(statuses, filter)), "invalid_input");
  const offset = (page - 1) * 10;
  const v = object(await native("backtest_jobs_list", { limit: 10, offset, ...(filter === "all" ? {} : { status: filter }) }), ["total", "items"]);
  requireValid(integer(v.total, 0) && Array.isArray(v.items) && v.items.length === Math.min(10, Math.max(0, v.total - offset)));
  const items = await Promise.all(v.items.map(item => parseSummary(item)));
  requireValid(new Set(items.map(item => item.id)).size === items.length &&
    (filter === "all" || items.every(item => item.status === filter)));
  return { items, total: v.total };
}

export function getBacktestJob(id: string, includeResult?: false): Promise<BacktestJobSummary>;
export function getBacktestJob(id: string, includeResult: true): Promise<BacktestJobDetail>;
export async function getBacktestJob(id: string, includeResult = false): Promise<BacktestJobSummary | BacktestJobDetail> {
  requireValid(identifier(id) && typeof includeResult === "boolean", "invalid_input");
  return parseSummary(await native("backtest_jobs_get", { jobId: id, includeResult }), id, includeResult);
}

export async function cancelBacktestJob(id: string): Promise<{ id: string; status: ModelJobStatus }> {
  requireValid(identifier(id), "invalid_input");
  const result = object(await native("backtest_jobs_cancel", { jobId: id }), ["state"]);
  requireValid(contains(statuses, result.state));
  return { id, status: result.state };
}

export async function retryBacktestJob(id: string): Promise<BacktestJobSummary> {
  requireValid(identifier(id), "invalid_input");
  return parseSummary(await native("backtest_jobs_retry", { jobId: id }), id);
}
