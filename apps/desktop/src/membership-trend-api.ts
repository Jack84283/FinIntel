import { invoke, isTauri } from "@tauri-apps/api/core";
import { INDEX_CODES, type IndexCode } from "./index-api";
import { validOverlapDate } from "./membership-overlap-api";

export type TrendEvidence =
  "verified_snapshot" | "source_date_mismatch" | "proxy_only" | "incomplete" | "unverified";
export type TrendChangeStatus = "first_observed" | "verified_pair" | "indicative_pair";
export type TrendQuery = {
  code: IndexCode; fromAsOf: string; toAsOf: string; limit: number; offset: number;
};
export type TrendPoint = {
  snapshot_as_of: string; source: string; source_as_of: string | null;
  source_kind: "official_licensed" | "verified_public" | "fund_derived_proxy" | "unknown";
  validation_status: "complete" | "incomplete" | "unknown";
  availability_status: "available" | "incomplete" | "proxy_only" | "unverified" | "not_imported";
  confidence: "high" | "medium" | "low" | "unknown";
  expected_count: number | null;
  member_count: number;
  evidence_status: TrendEvidence;
  previous_snapshot_as_of: string | null;
  previous_evidence_status: TrendEvidence | null;
  added_members: number | null;
  removed_members: number | null;
  change_status: TrendChangeStatus;
};
export type TrendResult = {
  code: IndexCode; name: string; from_as_of: string; to_as_of: string;
  total_snapshots: number; limit: number; offset: number; snapshots: TrendPoint[];
  snapshot_only: true; no_interpolation: true; no_lookahead: true; read_only: true;
};
const statuses: readonly TrendEvidence[] = [
  "verified_snapshot", "source_date_mismatch", "proxy_only", "incomplete", "unverified",
];
const count = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const nullableCount = (value: unknown) => value === null || count(value);
const isDateOrNull = (value: unknown): value is string | null =>
  value === null || typeof value === "string" && validOverlapDate(value);
function item(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Invalid local constituent trend response.");
  }
  return value as Record<string, unknown>;
}
export function validateTrendQuery(query: TrendQuery): void {
  if (!INDEX_CODES.includes(query.code) ||
    !validOverlapDate(query.fromAsOf) || !validOverlapDate(query.toAsOf) ||
    query.fromAsOf > query.toAsOf ||
    !count(query.limit) || query.limit < 1 || query.limit > 100 ||
    !count(query.offset) || query.offset > 1_000_000) {
    throw new Error("Invalid constituent snapshot trend selection.");
  }
}
function actualEvidence(p: Record<string, unknown>): TrendEvidence {
  if (p.source_kind === "fund_derived_proxy") return "proxy_only";
  if (p.validation_status === "incomplete") return "incomplete";
  if (!(p.source_kind === "official_licensed" || p.source_kind === "verified_public") ||
      p.validation_status !== "complete" || p.availability_status !== "available") return "unverified";
  return p.source_as_of !== p.snapshot_as_of ? "source_date_mismatch" : "verified_snapshot";
}
/** Fail closed rather than drawing invented daily/interpolated constituent history. */
export function parseTrendResult(payload: unknown, query: TrendQuery): TrendResult {
  const response = item(payload);
  if (response.code !== query.code || response.from_as_of !== query.fromAsOf ||
      response.to_as_of !== query.toAsOf || response.offset !== query.offset ||
      response.limit !== query.limit || typeof response.name !== "string" ||
      !count(response.total_snapshots) ||
      response.snapshot_only !== true || response.no_interpolation !== true ||
      response.no_lookahead !== true || response.read_only !== true ||
      !Array.isArray(response.snapshots) ||
      response.snapshots.length !== Math.min(query.limit, Math.max(0, response.total_snapshots - query.offset))) {
    throw new Error("Mismatched constituent trend selection or coverage.");
  }
  let prior: Record<string, unknown> | null = null;
  for (let i = 0; i < response.snapshots.length; i++) {
    const p = item(response.snapshots[i]);
    if (typeof p.snapshot_as_of !== "string" || !validOverlapDate(p.snapshot_as_of) ||
      p.snapshot_as_of < query.fromAsOf || p.snapshot_as_of > query.toAsOf ||
      (prior !== null && (p.snapshot_as_of as string) <= (prior.snapshot_as_of as string)) ||
      typeof p.source !== "string" || !p.source.trim() ||
      !isDateOrNull(p.source_as_of) ||
      !["official_licensed", "verified_public", "fund_derived_proxy", "unknown"].includes(String(p.source_kind)) ||
      !["complete", "incomplete", "unknown"].includes(String(p.validation_status)) ||
      !["available", "incomplete", "proxy_only", "unverified", "not_imported"].includes(String(p.availability_status)) ||
      !["high", "medium", "low", "unknown"].includes(String(p.confidence)) ||
      !nullableCount(p.expected_count) || !count(p.member_count) || p.member_count === 0 ||
      !statuses.includes(p.evidence_status as TrendEvidence) ||
      p.evidence_status !== actualEvidence(p) ||
      !isDateOrNull(p.previous_snapshot_as_of) ||
      (p.previous_snapshot_as_of !== null && (p.previous_snapshot_as_of as string) >= (p.snapshot_as_of as string)) ||
      !(p.previous_evidence_status === null || statuses.includes(p.previous_evidence_status as TrendEvidence)) ||
      !nullableCount(p.added_members) || !nullableCount(p.removed_members) ||
      !["first_observed", "verified_pair", "indicative_pair"].includes(String(p.change_status))) {
      throw new Error("Invalid dated membership trend evidence.");
    }
    const isFirst = query.offset === 0 && i === 0;
    if (isFirst) {
      if (p.previous_snapshot_as_of !== null || p.previous_evidence_status !== null ||
          p.added_members !== null || p.removed_members !== null || p.change_status !== "first_observed") {
        throw new Error("First snapshot cannot invent earlier changes.");
      }
    } else {
      const expectedStatus: TrendChangeStatus =
        p.previous_evidence_status === "verified_snapshot" && p.evidence_status === "verified_snapshot"
          ? "verified_pair" : "indicative_pair";
      if (p.previous_snapshot_as_of === null || p.previous_evidence_status === null ||
          !count(p.added_members) || !count(p.removed_members) || p.change_status !== expectedStatus ||
          (prior !== null && (p.previous_snapshot_as_of !== prior.snapshot_as_of ||
            p.previous_evidence_status !== prior.evidence_status ||
            p.member_count !== (prior.member_count as number) + (p.added_members as number) - (p.removed_members as number)))) {
        throw new Error("Inconsistent sourced membership changes.");
      }
    }
    prior = p;
  }
  return payload as TrendResult;
}
export async function fetchMembershipTrend(query: TrendQuery): Promise<TrendResult> {
  validateTrendQuery(query);
  let payload: unknown;
  if (isTauri()) {
    payload = await invoke("index_membership_trend", {
      code: query.code, fromAsOf: query.fromAsOf, toAsOf: query.toAsOf,
      limit: query.limit, offset: query.offset,
    });
  } else {
    const params = new URLSearchParams({
      code: query.code, from_as_of: query.fromAsOf, to_as_of: query.toAsOf,
      limit: String(query.limit), offset: String(query.offset),
    });
    const response = await fetch("/api/v1/market-data/indexes/snapshot-trend?" + params, {
      cache: "no-store",
    });
    if (!response.ok) {
      const error: unknown = await response.json().catch(() => null);
      const detail = error && typeof error === "object" && "detail" in error &&
        typeof error.detail === "string" ? error.detail : "HTTP " + response.status;
      throw new Error(detail);
    }
    payload = await response.json();
  }
  return parseTrendResult(payload, query);
}
