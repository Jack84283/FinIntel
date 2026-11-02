import { invoke, isTauri } from "@tauri-apps/api/core";

export type FindingState = "candidate" | "confirmed" | "rejected" | "accepted_correction";
export type CorrectionState = "proposed" | "validated" | "approved" | "rejected" | "reversed";
export type CorrectionAction = "validate" | "approve" | "reject" | "reverse";
export type QualityRoute = "overview" | "findings" | "report" | "history" | "corrections" |
  "correction" | "preview" | "corporate-actions" | "reconciliation";
export type QualityWrite = "scan" | "review" | "propose" | "transition" | "corporate-verify";

export interface QualityFinding {
  id: number;
  instrument_kind: "equity" | "index";
  code: string;
  provider: string;
  session_date: string | null;
  finding_type: string;
  severity: "info" | "warning" | "error";
  confidence: "low" | "medium" | "high";
  rule: string;
  evidence: Record<string, unknown>;
  fingerprint: string;
  review_state: FindingState;
  resolution_note: string | null;
  superseded: boolean;
  superseded_at: string | null;
  updated_at: string;
  created_at: string;
}
export interface QualityOverview {
  code: string | null;
  total: number;
  counts: Record<"severity" | "finding_type" | "review_state" | "instrument_kind", Record<string, number>>;
  scan: {
    status: string;
    stale: boolean;
    complete: boolean;
    partial: boolean;
    findings_examined: number | null;
    findings_stored: number | null;
    total_known_findings: number;
    cursor_phase: string | null;
    cursor_next: number | null;
    validation_reference_date: string;
    validation_reference_date_saved: string | null;
    source_revision_changed: boolean;
    source_catalog_identity_changed: boolean;
  };
}
export interface QualityPage<T> { total: number; limit: number; offset: number; items: T[]; truncated?: boolean }
export interface FindingHistory {
  finding: QualityFinding;
  reviews: { id: number; from_state: string; to_state: string; note: string | null; reviewed_at: string }[];
  source_revision: { created_at: string; updated_at: string; superseded_at: string | null; supersession_reason: string | null };
}
export interface Correction {
  id: string;
  code: string;
  provider: string;
  instrument_kind: "equity" | "index";
  adjustment: string;
  frequency: string;
  return_semantics: string;
  session_date: string;
  source_close: string;
  proposed_close: string;
  rationale: string;
  state: CorrectionState;
  events: { id: string; from_state: string | null; to_state: CorrectionState; note: string | null; occurred_at: string }[];
}
export interface CorrectionPreview {
  correction_id: string;
  state: CorrectionState;
  captured_source_close: string;
  proposed_close: string;
  absolute_delta: string;
  relative_delta: string;
  stale_source: boolean;
  approved_conflict: string | null;
  eligible_for_validation: boolean;
  eligible_for_approval: boolean;
  modifies_stored_prices: false;
}
export interface CorporateActions {
  code: string;
  sourced: { id: string; session_date: string; kind: string; provider: string; split_factor: string; cash_dividend: string; status: string }[];
  suspected: { finding_id: number; session_date: string; rule: string; provider: string; status: string }[];
}

async function queryQuality<T>(route: QualityRoute, query: Record<string, string | number> = {}, id?: string): Promise<T> {
  if (isTauri()) return invoke<T>("quality_read", { route, query, id });
  const path = {
    overview: "/api/v1/quality/overview",
    findings: "/api/v1/quality/findings",
    report: "/api/v1/quality/report",
    corrections: "/api/v1/quality/corrections",
    "corporate-actions": "/api/v1/quality/corporate-actions",
    reconciliation: "/api/v1/quality/reconciliation/summary",
    history: `/api/v1/quality/findings/${encodeURIComponent(id ?? "")}/history`,
    correction: `/api/v1/quality/corrections/${encodeURIComponent(id ?? "")}`,
    preview: `/api/v1/quality/corrections/${encodeURIComponent(id ?? "")}/preview`,
  }[route];
  const params = new URLSearchParams(Object.entries(query).map(([key, value]) => [key, String(value)]));
  const response = await fetch(path + (params.size ? `?${params.toString()}` : ""), { cache: "no-store" });
  return parseResponse<T>(response);
}

async function writeQuality<T>(route: QualityWrite, body: Record<string, unknown>, id?: string, action?: string): Promise<T> {
  if (isTauri()) return invoke<T>("quality_write", { route, body, id, action });
  const path = {
    scan: "/api/v1/quality/scan",
    review: `/api/v1/quality/findings/${encodeURIComponent(id ?? "")}/review`,
    propose: "/api/v1/quality/corrections",
    transition: `/api/v1/quality/corrections/${encodeURIComponent(id ?? "")}/${encodeURIComponent(action ?? "")}`,
    "corporate-verify": "/api/v1/quality/corporate-actions/verify",
  }[route];
  const response = await fetch(path, {
    method: "POST", cache: "no-store", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parseResponse<T>(response);
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const json = await response.json().catch(() => ({})) as { detail?: unknown };
    throw new Error(typeof json.detail === "string" ? json.detail : `Data Quality HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export const fetchQualityOverview = (code: string) => queryQuality<QualityOverview>("overview", { code });
export const fetchQualityFindings = (filters: {
  code: string; provider?: string; finding_type?: string; review_state?: string;
  limit: number; offset: number;
}) => queryQuality<QualityPage<QualityFinding>>(
  "findings", Object.fromEntries(Object.entries(filters).filter(([, value]) => value !== undefined && value !== "")) as Record<string, string | number>,
);
export const fetchFindingHistory = (id: number) => queryQuality<FindingHistory>("history", {}, String(id));
export const fetchQualityCorrections = (code: string, limit = 50, offset = 0) =>
  queryQuality<QualityPage<Correction>>("corrections", { code, limit, offset });
export const fetchCorrection = (id: string) => queryQuality<Correction>("correction", {}, id);
export const fetchCorrectionPreview = (id: string) => queryQuality<CorrectionPreview>("preview", {}, id);
export const fetchCorporateActions = (code: string) => queryQuality<CorporateActions>("corporate-actions", { code });
export const fetchReconciliation = (code: string) =>
  queryQuality<{ provider_series: { provider: string; series_count: number }[]; findings: { total: number; active: number } }>("reconciliation", { code });

export const runQualityScan = (maxFindings = 1000) =>
  writeQuality<Record<string, unknown>>("scan", { max_findings: maxFindings });
export const submitFindingReview = (id: number, state: FindingState, note: string) =>
  writeQuality<QualityFinding>("review", { state, note }, String(id));
export const createQualityCorrection = (findingId: number, provider: string, proposedClose: string, rationale: string) =>
  writeQuality<Correction>("propose", {
    finding_id: findingId, provider, proposed_close: proposedClose, rationale,
  });
export const transitionQualityCorrection = (id: string, action: CorrectionAction, note: string) =>
  writeQuality<Correction>("transition", { note }, id, action);
export const verifyQualityCorporateActions = (symbol: string) =>
  writeQuality<{ evidence_records: number; verified_events: number }>("corporate-verify", { symbol });

const safeEvidenceKeys = new Set([
  "candidate_only", "anomaly_rule_version", "calendar_rule_version", "reconciliation_rule_version",
  "previous_session", "previous_close", "current_close", "observed_close",
  "left_provider", "right_provider", "left_close", "right_close", "relative_difference",
  "absolute_difference", "adjustment", "frequency", "currency", "unit",
  "return_type", "missing_provider", "observed_provider", "expected_split_ratio",
  "observed_close_ratio", "price_ratio", "close_ratio", "split_factor",
  "threshold", "threshold_ratio", "rule_version", "severity", "confidence",
  "stale_days", "expected_sessions", "session_date", "last_session",
  "missing_sessions", "volume", "baseline_volume", "volume_ratio",
  "source_revision", "retrieved_at", "close", "instrument", "provider",
]);

/** Allowlist only public, bounded scalar evidence. Unknown JSON, URLs and secrets never export. */
export function redactedEvidence(input: Record<string, unknown>): Record<string, string | number | boolean> {
  const result: Record<string, string | number | boolean> = {};
  for (const [key, value] of Object.entries(input)) {
    if (!safeEvidenceKeys.has(key)) continue;
    if (typeof value === "boolean") { result[key] = value; continue; }
    if (typeof value === "number" && Number.isFinite(value)) { result[key] = value; continue; }
    if (typeof value !== "string" || value.length > 120 ||
        /(?:https?:\/\/|token|secret|authorization|api[_-]?key|bearer)/i.test(value)) continue;
    result[key] = value;
  }
  return result;
}

export function safeFinding(finding: QualityFinding) {
  return {
    code: finding.code, instrument_kind: finding.instrument_kind, provider: finding.provider,
    session_date: finding.session_date, finding_type: finding.finding_type, rule: finding.rule,
    severity: finding.severity, confidence: finding.confidence, review_state: finding.review_state,
    evidence: redactedEvidence(finding.evidence),
  };
}

export function exportQualityJson(code: string, summary: QualityOverview, findings: QualityFinding[]): string {
  return JSON.stringify({
    format: "finintel-data-quality-redacted-v1",
    scope: code, exported_rows: findings.length, total_matching_rows: summary.total,
    scan: summary.scan, findings: findings.map(safeFinding),
    disclaimer: "Candidate findings are not confirmed defects. Raw prices are never corrected by this export.",
  }, null, 2);
}

function csvCell(value: unknown): string {
  let item = String(value ?? "");
  if (/^[\s\u0000-\u001f]*[=+\-@]/.test(item)) item = "'" + item;
  return '"' + item.replaceAll('"', '""') + '"';
}
export function exportQualityCsv(findings: QualityFinding[]): string {
  const fields = ["code", "instrument_kind", "provider", "session_date", "finding_type", "rule",
    "severity", "confidence", "review_state", "evidence"] as const;
  const rows = findings.map(safeFinding);
  return [fields.join(","), ...rows.map(row =>
    fields.map(key => csvCell(key === "evidence" ? JSON.stringify(row.evidence) : row[key])).join(","),
  )].join("\n");
}
