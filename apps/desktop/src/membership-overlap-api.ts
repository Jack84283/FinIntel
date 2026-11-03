import { invoke, isTauri } from "@tauri-apps/api/core";
import { INDEX_CODES, type IndexCode, type IndexRecord } from "./index-api";

export type OverlapStatus =
  "verified_snapshot" | "earlier_snapshot" | "proxy_only" |
  "incomplete" | "unverified" | "unavailable";
export type OverlapSource = Pick<IndexRecord,
  "code" | "name" | "requested_as_of" | "snapshot_as_of" | "source" |
  "source_kind" | "source_as_of" | "validation_status" |
  "availability_status" | "confidence" | "expected_count"> & {
    member_count: number | null;
  };
export type OverlapMember = { symbol: string; name: string; asset_type: string };
export type OverlapResult = {
  left_code: IndexCode;
  right_code: IndexCode;
  requested_as_of: string;
  sources: OverlapSource[];
  status: OverlapStatus;
  common_members: number | null;
  union_members: number | null;
  jaccard_ratio: string | null;
  limit: number;
  offset: number;
  query: string;
  filtered_total: number | null;
  items: OverlapMember[];
  snapshot_only: true;
  no_lookahead: true;
  read_only: true;
};
export type OverlapQuery = {
  left: IndexCode; right: IndexCode; asOf: string;
  q: string; limit: number; offset: number;
};
const statusSet: readonly OverlapStatus[] = [
  "verified_snapshot", "earlier_snapshot", "proxy_only",
  "incomplete", "unverified", "unavailable",
];
export function validOverlapDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(value + "T00:00:00Z");
  return Number.isFinite(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === value;
}
export function validateOverlapQuery(query: OverlapQuery): void {
  if (!INDEX_CODES.includes(query.left) || !INDEX_CODES.includes(query.right) ||
    query.left === query.right || !validOverlapDate(query.asOf) ||
    [...query.q].length > 80 || [...query.q].some(ch => ch.charCodeAt(0) < 32 || ch.charCodeAt(0) === 127) ||
    !Number.isSafeInteger(query.offset) || query.offset < 0 || query.offset > 1_000_000 ||
    !Number.isSafeInteger(query.limit) || query.limit < 1 || query.limit > 100) {
    throw new Error("Invalid point-in-time overlap selection.");
  }
}
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Invalid local membership overlap response.");
  }
  return value as Record<string, unknown>;
}
const nonnegative = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const nullableCount = (value: unknown) => value === null || nonnegative(value);
const nullableString = (value: unknown) => value === null || typeof value === "string";
const trustedSource = (kind: unknown, validation: unknown, availability: unknown) =>
  (kind === "verified_public" || kind === "official_licensed") &&
  validation === "complete" && availability === "available";
const sourceStatus = (source: Record<string, unknown>, requestedDate: string): boolean =>
  typeof source.name === "string" &&
  typeof source.requested_as_of === "string" && source.requested_as_of === requestedDate &&
  nullableString(source.snapshot_as_of) &&
  (source.snapshot_as_of === null ||
    validOverlapDate(source.snapshot_as_of as string) && (source.snapshot_as_of as string) <= requestedDate) &&
  nullableString(source.source) && nullableString(source.source_as_of) &&
  (source.source_as_of === null ||
    validOverlapDate(source.source_as_of as string) && (source.source_as_of as string) <= requestedDate) &&
  [null, "official_licensed", "verified_public", "fund_derived_proxy", "unknown"].includes(source.source_kind as string | null) &&
  [null, "complete", "incomplete", "unknown"].includes(source.validation_status as string | null) &&
  ["available", "incomplete", "proxy_only", "unverified", "not_imported"].includes(source.availability_status as string) &&
  [null, "high", "medium", "low", "unknown"].includes(source.confidence as string | null) &&
  nullableCount(source.expected_count) && nullableCount(source.member_count);

export function parseOverlapResult(payload: unknown, query: OverlapQuery): OverlapResult {
  const value = object(payload);
  if (value.left_code !== query.left || value.right_code !== query.right ||
    value.requested_as_of !== query.asOf || value.limit !== query.limit ||
    value.offset !== query.offset || value.query !== query.q ||
    !statusSet.includes(value.status as OverlapStatus) ||
    value.snapshot_only !== true || value.no_lookahead !== true || value.read_only !== true ||
    !Array.isArray(value.sources) || value.sources.length !== 2 ||
    !nullableCount(value.common_members) || !nullableCount(value.union_members) ||
    !nullableCount(value.filtered_total) || !Array.isArray(value.items) ||
    value.items.length > query.limit) {
    throw new Error("Mismatched membership overlap response.");
  }
  const sources = value.sources.map(object);
  if (sources.some((source, i) => source.code !== [query.left, query.right][i] ||
    !sourceStatus(source, query.asOf))) throw new Error("Invalid sourced membership evidence.");
  const missing = sources.some(source => source.snapshot_as_of === null);
  if (missing) {
    if (value.status !== "unavailable" || value.common_members !== null ||
      value.union_members !== null || value.jaccard_ratio !== null ||
      value.filtered_total !== null || value.items.length) {
      throw new Error("Unavailable membership cannot claim zero or any computed overlap.");
    }
  } else {
    const leftMembers = sources[0].member_count;
    const rightMembers = sources[1].member_count;
    if (value.status === "unavailable" || !nonnegative(value.common_members) ||
      !nonnegative(value.union_members) || !nonnegative(value.filtered_total) ||
      !nonnegative(leftMembers) || !nonnegative(rightMembers) ||
      (value.common_members as number) > Math.min(leftMembers as number, rightMembers as number) ||
      value.union_members !== (leftMembers as number) + (rightMembers as number) - (value.common_members as number) ||
      (value.filtered_total as number) > (value.common_members as number) ||
      (value.union_members as number) < (value.common_members as number) ||
      typeof value.jaccard_ratio !== "string" || value.jaccard_ratio.trim() === "" ||
      !Number.isFinite(Number(value.jaccard_ratio)) || Number(value.jaccard_ratio) < 0 ||
      Number(value.jaccard_ratio) > 1 ||
      Math.abs(Number(value.jaccard_ratio) -
        (value.common_members as number) / (value.union_members as number)) > 1e-10 ||
      (value.offset as number) + value.items.length > (value.filtered_total as number) && value.items.length > 0) {
      throw new Error("Invalid sourced overlap counts.");
    }
    const [left, right] = sources;
    const trusted = sources.every(source => trustedSource(
      source.source_kind, source.validation_status, source.availability_status,
    ));
    const expected: OverlapStatus = sources.some(s => s.source_kind === "fund_derived_proxy")
      ? "proxy_only"
      : sources.some(s => s.validation_status === "incomplete")
        ? "incomplete"
        : !trusted ? "unverified"
          : left.snapshot_as_of !== query.asOf || right.snapshot_as_of !== query.asOf ||
          left.source_as_of !== query.asOf || right.source_as_of !== query.asOf
            ? "earlier_snapshot" : "verified_snapshot";
    if (value.status !== expected) throw new Error("Overlap confidence does not match provenance.");
  }
  const seen = new Set<string>();
  let previous = "";
  for (const raw of value.items) {
    const item = object(raw);
    if (typeof item.symbol !== "string" || !/^[A-Z0-9][A-Z0-9.-]{0,19}$/.test(item.symbol) ||
      typeof item.name !== "string" || !item.name.trim() ||
      typeof item.asset_type !== "string" || !item.asset_type ||
      seen.has(item.symbol) || (previous && item.symbol <= previous)) {
      throw new Error("Invalid overlap constituent list.");
    }
    seen.add(item.symbol);
    previous = item.symbol;
  }
  return payload as OverlapResult;
}
export async function fetchMembershipOverlap(query: OverlapQuery): Promise<OverlapResult> {
  validateOverlapQuery(query);
  let payload: unknown;
  if (isTauri()) {
    payload = await invoke("index_membership_overlap", {
      left: query.left, right: query.right, asOf: query.asOf,
      q: query.q, limit: query.limit, offset: query.offset,
    });
  } else {
    const params = new URLSearchParams({
      left: query.left, right: query.right, as_of: query.asOf,
      q: query.q, limit: String(query.limit), offset: String(query.offset),
    });
    const response = await fetch("/api/v1/market-data/indexes/overlap?" + params, {
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
  return parseOverlapResult(payload, query);
}
