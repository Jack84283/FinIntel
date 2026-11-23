import contracts from "../../../contracts/analysis-jobs.json";
import {
  AnalysisJobError, type AnalysisJobRequest, type AnalysisModelId, type AnalysisSourceCode,
} from "./analysis-jobs-api";
import { fetchIndexAnalytics, type AnalyticsResult } from "./index-intelligence-api";

type Rule = {
  default: number | string;
  minimum?: number;
  maximum?: number;
  exclusiveMaximum?: number;
  enum?: number[];
  pattern?: string;
};

export const SOURCE_CODES = ["SP500", "DJIA", "NASDAQCOM", "NDX"] as const;
export const HISTORY_LIMIT = 2000;

export const FAMILIES = {
  "Trend & Momentum": ["builtin.ema_trend", "builtin.momentum", "builtin.relative_strength", "builtin.rsi_state"],
  Risk: ["builtin.risk_profile"],
  "Market Regime": ["builtin.market_regime"],
} as const satisfies Record<string, readonly AnalysisModelId[]>;

export const MODELS = {
  "builtin.ema_trend": ["EMA Trend", "Compare fast and slow exponential averages of observed closes."],
  "builtin.momentum": ["Trailing Momentum", "Measure price change over an observed-session lookback."],
  "builtin.relative_strength": ["Relative Strength", "Compare price indexes on common observed sessions."],
  "builtin.rsi_state": ["RSI Characterization", "Describe historical RSI against configured thresholds."],
  "builtin.risk_profile": ["Historical Risk Profile", "Inspect historical volatility, drawdown and tail losses."],
  "builtin.market_regime": ["Deterministic Market Regime", "Describe historical trend and volatility independently."],
} as const satisfies Record<AnalysisModelId, readonly [string, string]>;

export const PARAMETERS: Record<string, string> = {
  fast_window: "Fast EMA window", slow_window: "Slow EMA window",
  lookback_sessions: "Observed-session lookback", window: "RSI calculation window",
  lower_threshold: "Lower RSI threshold", upper_threshold: "Upper RSI threshold",
  risk_window: "Risk calculation window", tail_window: "VaR and ES history window",
  short_window: "Short SMA window", long_window: "Long SMA window",
  neutrality_band: "Neutrality band (fraction, 0.02 = 2%)",
  volatility_window: "Realized-volatility window", calibration_window: "Calibration history",
};

export const SOURCE_NAMES: Record<AnalysisSourceCode, string> = {
  SP500: "S&P 500", DJIA: "Dow Jones Industrial Average",
  NASDAQCOM: "Nasdaq Composite", NDX: "Nasdaq-100",
};

export function parameterRules(model: AnalysisModelId): Record<string, Rule> {
  return contracts[model].parameters;
}

export function parameterDefaults(model: AnalysisModelId): Record<string, string> {
  return Object.fromEntries(Object.entries(parameterRules(model)).map(([name, rule]) => [name, String(rule.default)]));
}

export function validateParameters(model: AnalysisModelId, draft: Record<string, string>) {
  const parameters: Record<string, number | string> = {};
  const errors: Record<string, string> = {};
  for (const [name, rule] of Object.entries(parameterRules(model))) {
    const raw = draft[name] ?? "";
    if (rule.pattern) {
      if (!new RegExp(rule.pattern).test(raw)) errors[name] = "Enter a decimal fraction in the allowed format.";
      else parameters[name] = raw;
      continue;
    }
    const value = Number(raw);
    if (!/^\d+$/.test(raw) || !Number.isSafeInteger(value) ||
        value < (rule.minimum ?? 0) || value > (rule.maximum ?? Infinity) ||
        value >= (rule.exclusiveMaximum ?? Infinity) || (rule.enum && !rule.enum.includes(value))) {
      errors[name] = "Choose a supported whole number.";
    } else parameters[name] = value;
  }
  const lessThan = (lower: string, upper: string) => {
    if (Number(parameters[lower]) >= Number(parameters[upper])) errors[upper] = "This value must exceed the preceding bound.";
  };
  if (model === "builtin.ema_trend") lessThan("fast_window", "slow_window");
  if (model === "builtin.rsi_state") lessThan("lower_threshold", "upper_threshold");
  if (model === "builtin.market_regime") {
    lessThan("short_window", "long_window");
    if (Number(parameters.volatility_window) + Number(parameters.calibration_window) >= HISTORY_LIMIT) {
      errors.calibration_window = "Volatility and calibration windows together must be below 2,000.";
    }
  }
  return { parameters, errors };
}

/** Mirrors AL-05B's minimum-history gate. Optional full calibration is shown separately. */
export function minimumHistory(model: AnalysisModelId, p: Record<string, number | string>) {
  if (model === "builtin.ema_trend") return Number(p.slow_window);
  if (model === "builtin.momentum") return Number(p.lookback_sessions) + 1;
  if (model === "builtin.relative_strength") return 2;
  return 1;
}

export function isAnalysisSource(code: string): code is AnalysisSourceCode {
  return SOURCE_CODES.some(source => source === code);
}

export async function inspectSources(codes: readonly AnalysisSourceCode[], window: AnalysisJobRequest["window"]) {
  return Promise.all(codes.map(async code => {
    // Read with the preview override so the user can see the quality state even
    // under a strict execution policy. Only the job request sets execution policy.
    const data = await fetchIndexAnalytics({ code, comparisonCode: null, ...window, window: 20, qualityPolicy: "allow_unverified" });
    if (data.identity.code !== code || data.identity.frequency !== "daily" ||
        data.identity.instrument_kind !== "index" || data.identity.provider !== "fred" ||
        data.identity.adjustment !== "price_index" || data.identity.return_semantics !== "price_index" ||
        data.identity.units !== "index_points" ||
        data.request.start !== window.start || data.request.end !== window.end ||
        data.points.some(point => point.session_date < window.start || point.session_date > window.end) ||
        !/^[a-f0-9]{64}$/.test(data.source.snapshot_sha256)) {
      throw new AnalysisJobError("invalid_response");
    }
    return data;
  }));
}

export function commonSessions(results: readonly AnalyticsResult[]): number {
  const sets = results.slice(1).map(result => new Set(result.points.map(point => point.session_date)));
  return results[0]?.points.filter(point => sets.every(set => set.has(point.session_date))).length ?? 0;
}

export function analysisRequest(
  model: AnalysisModelId, sources: readonly AnalyticsResult[], window: AnalysisJobRequest["window"],
  qualityPolicy: AnalysisJobRequest["quality_policy"], parameters: Record<string, number | string>,
): AnalysisJobRequest {
  return {
    contract_version: "analysis-model-request-v1",
    model_id: model, model_version: contracts[model].version,
    implementation_revision: contracts[model].revision,
    sources: sources.map(source => ({ identity: source.identity, snapshot_sha256: source.source.snapshot_sha256 })),
    window, quality_policy: qualityPolicy, parameters,
  } as AnalysisJobRequest; // The checked-in registry and validateParameters select this arm.
}

export function analysisErrorMessage(error: unknown): string {
  const code = error instanceof AnalysisJobError ? error.code : "backend_unavailable";
  switch (code) {
    case "invalid_response": return "Result or source evidence failed integrity validation.";
    case "source_changed": return "The original source snapshot has changed. Review current sources before submitting a new job.";
    case "managed_required": return "Analysis Library jobs require the managed FinIntel desktop.";
    case "invalid_input": return "The local service rejected these analysis inputs.";
    case "queue_full": return "The local model queue is full. Wait for a job to finish.";
    case "job_conflict": return "This job changed. Refresh its details before trying again.";
    case "job_not_found": return "This Analysis Library job was not found.";
    case "quality_blocked": return "Run a current, complete Data Quality scan for every selected source.";
    case "resource_limit": return "The analysis exceeds local resource limits.";
    case "storage_unavailable": return "Local analysis storage is unavailable.";
    case "backend_unavailable": return "The local analysis service is unavailable.";
  }
}
