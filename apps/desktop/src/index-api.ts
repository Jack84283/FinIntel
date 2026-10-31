/** Typed local read-only index snapshots; no provider calls or price-derived membership. */

import { invoke, isTauri } from "@tauri-apps/api/core";

export type IndexCode = "SP500" | "DJIA" | "NDX" | "RUT";
export type MembershipSourceKind = "official_licensed" | "verified_public" | "fund_derived_proxy" | "unknown";
export type MembershipAvailability = "available" | "incomplete" | "proxy_only" | "unverified" | "not_imported";
export type MembershipValidation = "complete" | "incomplete" | "unknown";
export type MembershipConfidence = "high" | "medium" | "low" | "unknown";
export const INDEX_CODES: IndexCode[] = ["SP500", "DJIA", "NDX", "RUT"];

export interface IndexRecord {
  code: IndexCode;
  name: string;
  requested_as_of: string;
  snapshot_as_of: string | null;
  source: string | null;
  members: number | null;
  source_kind: MembershipSourceKind | null;
  source_as_of: string | null;
  expected_count: number | null;
  validation_status: MembershipValidation | null;
  availability_status: MembershipAvailability;
  confidence: MembershipConfidence | null;
  status: "available" | "not_imported";
}

export interface Constituent {
  symbol: string;
  name: string;
  asset_type: string;
  member_indexes: IndexCode[];
}

export interface ConstituentPage {
  code: IndexCode;
  name: string;
  requested_as_of: string;
  snapshot_as_of: string | null;
  source: string | null;
  snapshot_members: number | null;
  source_kind: MembershipSourceKind | null;
  source_as_of: string | null;
  expected_count: number | null;
  validation_status: MembershipValidation | null;
  availability_status: MembershipAvailability;
  confidence: MembershipConfidence | null;
  total: number;
  limit: number;
  offset: number;
  items: Constituent[];
}

export interface MembershipChange {
  symbol: string;
  name: string;
  asset_type: string;
  change: "added" | "removed";
}

export interface MembershipChangePage {
  code: IndexCode;
  name: string;
  from_requested_as_of: string;
  to_requested_as_of: string;
  from_snapshot_as_of: string | null;
  to_snapshot_as_of: string | null;
  from_source: string | null;
  to_source: string | null;
  status: "available" | "unknown";
  total: number;
  limit: number;
  offset: number;
  items: MembershipChange[];
}

export interface SymbolIndexes {
  symbol: string;
  requested_as_of: string;
  indexes: (IndexRecord & { is_member: boolean | null })[];
}

function record(raw: unknown): Record<string, unknown> {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("Invalid index data.");
  return raw as Record<string, unknown>;
}
function isCode(raw: unknown): raw is IndexCode {
  return typeof raw === "string" && INDEX_CODES.some(code => code === raw);
}
function nullableString(raw: unknown): raw is string | null {
  return raw === null || typeof raw === "string";
}
function nullableCount(raw: unknown): raw is number | null {
  return raw === null || (Number.isSafeInteger(raw) && (raw as number) >= 0);
}
function validSourceKind(raw: unknown): raw is MembershipSourceKind {
  return raw === null || raw === "official_licensed" || raw === "verified_public" ||
    raw === "fund_derived_proxy" || raw === "unknown";
}
function validAvailability(raw: unknown): raw is MembershipAvailability {
  return raw === "available" || raw === "incomplete" || raw === "proxy_only" ||
    raw === "unverified" || raw === "not_imported";
}
function validValidation(raw: unknown): raw is MembershipValidation | null {
  return raw === null || raw === "complete" || raw === "incomplete" || raw === "unknown";
}
function validConfidence(raw: unknown): raw is MembershipConfidence | null {
  return raw === null || raw === "high" || raw === "medium" || raw === "low" || raw === "unknown";
}
function validDate(date: string): boolean {
  return date === "" || /^\d{4}-\d{2}-\d{2}$/.test(date);
}
function parseIndex(raw: unknown): IndexRecord {
  const obj = record(raw);
  if (!isCode(obj.code) || typeof obj.name !== "string" ||
      typeof obj.requested_as_of !== "string" || !nullableString(obj.snapshot_as_of) ||
      !nullableString(obj.source) || !nullableCount(obj.members) ||
      !validSourceKind(obj.source_kind) || !nullableString(obj.source_as_of) ||
      !nullableCount(obj.expected_count) || !validValidation(obj.validation_status) ||
      !validAvailability(obj.availability_status) || !validConfidence(obj.confidence) ||
      (obj.status !== "available" && obj.status !== "not_imported")) {
    throw new Error("Invalid index snapshot metadata.");
  }
  return obj as unknown as IndexRecord;
}
async function readLocal(path: string, command: string, args: Record<string, unknown>): Promise<unknown> {
  if (isTauri()) return invoke<unknown>(command, args);
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error("Index lookup returned HTTP " + response.status);
  return response.json();
}

export async function fetchIndexes(asOf = ""): Promise<IndexRecord[]> {
  if (!validDate(asOf)) throw new Error("Invalid as-of date.");
  const params = new URLSearchParams();
  if (asOf) params.set("as_of", asOf);
  const result = await readLocal("/api/v1/market-data/indexes?" + params,
    "index_catalog", { asOf: asOf || null });
  if (!Array.isArray(result) || result.length !== INDEX_CODES.length) {
    throw new Error("Invalid index catalog.");
  }
  return result.map(parseIndex);
}

export async function fetchConstituents(
  code: IndexCode, asOf = "", q = "", offset = 0, limit = 50,
): Promise<ConstituentPage> {
  if (!isCode(code) || !validDate(asOf) || q.length > 80 ||
      !Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(limit) ||
      limit < 1 || limit > 100) throw new Error("Invalid index lookup.");
  const params = new URLSearchParams({ q, limit: String(limit), offset: String(offset) });
  if (asOf) params.set("as_of", asOf);
  const result = record(await readLocal(
    "/api/v1/market-data/indexes/" + code + "/constituents?" + params,
    "index_constituents", { code, asOf: asOf || null, q, limit, offset },
  ));
  if (result.code !== code || !nullableString(result.snapshot_as_of) ||
      !nullableString(result.source) || !nullableCount(result.snapshot_members) ||
      !validSourceKind(result.source_kind) || !nullableString(result.source_as_of) ||
      !nullableCount(result.expected_count) || !validValidation(result.validation_status) ||
      !validAvailability(result.availability_status) || !validConfidence(result.confidence) ||
      typeof result.requested_as_of !== "string" ||
      typeof result.name !== "string" || result.offset !== offset ||
      result.limit !== limit || !Number.isSafeInteger(result.total) ||
      (result.total as number) < 0 || !Array.isArray(result.items) ||
      result.items.length > limit) throw new Error("Malformed index constituent page.");
  const items = result.items.map((value: unknown): Constituent => {
    const item = record(value);
    if (typeof item.symbol !== "string" || typeof item.name !== "string" ||
        typeof item.asset_type !== "string" || !Array.isArray(item.member_indexes) ||
        !item.member_indexes.every(isCode)) throw new Error("Invalid index constituent.");
    return item as unknown as Constituent;
  });
  return { ...result, items } as ConstituentPage;
}

export async function fetchStockIndexes(symbol: string, asOf = ""): Promise<SymbolIndexes> {
  const ticker = symbol.trim().toUpperCase();
  if (!/^[A-Z0-9][A-Z0-9.-]{0,14}$/.test(ticker) || !validDate(asOf)) {
    throw new Error("Enter a valid stock ticker and date.");
  }
  const params = new URLSearchParams();
  if (asOf) params.set("as_of", asOf);
  const result = record(await readLocal(
    "/api/v1/market-data/indexes/symbol/" + ticker + "/memberships?" + params,
    "index_stock_memberships", { symbol: ticker, asOf: asOf || null },
  ));
  if (result.symbol !== ticker || !Array.isArray(result.indexes) ||
      typeof result.requested_as_of !== "string") throw new Error("Invalid index membership result.");
  return {
    symbol: ticker,
    requested_as_of: result.requested_as_of,
    indexes: result.indexes.map((raw: unknown) => {
      const item = record(raw);
      if (item.is_member !== null && typeof item.is_member !== "boolean") {
        throw new Error("Invalid membership flag.");
      }
      return { ...parseIndex(item), is_member: item.is_member as boolean | null };
    }),
  };
}

export async function fetchMembershipChanges(
  code: IndexCode, fromAsOf: string, toAsOf: string, q = "", offset = 0, limit = 50,
): Promise<MembershipChangePage> {
  if (!isCode(code) || !validDate(fromAsOf) || !validDate(toAsOf) || !fromAsOf || !toAsOf ||
      q.length > 80 || !Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(limit) ||
      limit < 1 || limit > 100) throw new Error("Invalid index change lookup.");
  const params = new URLSearchParams({
    from_as_of: fromAsOf, to_as_of: toAsOf, q, limit: String(limit), offset: String(offset),
  });
  const result = record(await readLocal(
    "/api/v1/market-data/indexes/" + code + "/changes?" + params,
    "index_membership_changes", { code, fromAsOf, toAsOf, q, limit, offset },
  ));
  if (result.code !== code || typeof result.name !== "string" ||
      typeof result.from_requested_as_of !== "string" || typeof result.to_requested_as_of !== "string" ||
      !nullableString(result.from_snapshot_as_of) || !nullableString(result.to_snapshot_as_of) ||
      !nullableString(result.from_source) || !nullableString(result.to_source) ||
      (result.status !== "available" && result.status !== "unknown") ||
      !Number.isSafeInteger(result.total) || (result.total as number) < 0 || result.limit !== limit ||
      result.offset !== offset || !Array.isArray(result.items) || result.items.length > limit) {
    throw new Error("Malformed index membership change page.");
  }
  const items = result.items.map((value: unknown): MembershipChange => {
    const item = record(value);
    if (typeof item.symbol !== "string" || typeof item.name !== "string" ||
        typeof item.asset_type !== "string" || (item.change !== "added" && item.change !== "removed")) {
      throw new Error("Invalid index membership change.");
    }
    return item as unknown as MembershipChange;
  });
  return { ...result, items } as MembershipChangePage;
}

/** Acquisition is explicit and only the local backend knows the provider key. */
export async function acquireIndex(code: IndexCode): Promise<{ code: IndexCode; snapshot_as_of: string; members: number; source: string; provenance: string; complete: boolean; unlinked_count: number; source_total: number }> {
  if (!isCode(code)) throw new Error("Invalid index.");
  if (isTauri()) return invoke("index_acquire", { code });
  const url = "/api/v1/market-data/indexes/" + code + "/acquire";
  const response = await fetch(url, {
    method: "POST",
    headers: { "X-FinIntel-Client": "local-workstation" },
  });
  if (!response.ok) {
    const error: unknown = await response.json().catch(() => ({}));
    const detail = error && typeof error === "object" && "detail" in error ? (error as { detail: unknown }).detail : null;
    throw new Error(typeof detail === "string" ? detail : "Index acquisition failed (HTTP " + response.status + ").");
  }
  return response.json();
}
