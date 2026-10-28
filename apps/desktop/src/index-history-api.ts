import { invoke, isTauri } from "@tauri-apps/api/core";

export type IndexCode = "SP500" | "DJIA" | "NASDAQCOM" | "NDX" | "VIX";
export const INDEX_CODES: readonly IndexCode[] = ["SP500", "DJIA", "NASDAQCOM", "NDX", "VIX"];

export type IndexCatalogRecord = {
  code: IndexCode;
  name: string;
  fred_series: string;
  provider: "fred";
  source_url: string;
  sessions: number;
  first_session: string | null;
  last_session: string | null;
  data_kind: "daily_close_only";
};

export type IndexCloseRecord = { session_date: string; close: string };
export type IndexHistory = {
  code: IndexCode;
  name: string;
  data_kind: "daily_close_only";
  unit: "index_points";
  provider: "fred";
  source_url: string;
  has_more: boolean;
  observations: IndexCloseRecord[];
};
export type IndexImportResult = {
  code: IndexCode;
  provider: "fred";
  status: "stored";
  sessions: number;
  inserted: number;
  updated: number;
  unchanged: number;
  first_session: string;
  last_session: string;
  source_url: string;
};

function assertRecord(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Invalid index history response.");
  }
  return value as Record<string, unknown>;
}

function validCode(value: unknown): value is IndexCode {
  return typeof value === "string" && INDEX_CODES.some(code => code === value);
}

function parseCatalog(value: unknown): IndexCatalogRecord[] {
  if (!Array.isArray(value) || value.length > INDEX_CODES.length) {
    throw new Error("Invalid index catalog.");
  }
  return value.map(item => {
    const row = assertRecord(item);
    if (!validCode(row.code) || typeof row.name !== "string" ||
        typeof row.fred_series !== "string" || typeof row.source_url !== "string" ||
        row.provider !== "fred" || row.data_kind !== "daily_close_only" ||
        !Number.isSafeInteger(row.sessions) || (row.sessions as number) < 0 ||
        !(row.first_session === null || typeof row.first_session === "string") ||
        !(row.last_session === null || typeof row.last_session === "string")) {
      throw new Error("Invalid index catalog record.");
    }
    return item as IndexCatalogRecord;
  });
}

async function readonlyGet(path: string, command: string, args?: Record<string, unknown>): Promise<unknown> {
  if (isTauri()) return invoke(command, args);
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error("Index history backend returned HTTP " + response.status);
  return response.json();
}

export async function listIndexHistory(): Promise<IndexCatalogRecord[]> {
  return parseCatalog(await readonlyGet("/api/v1/market-data/index-history", "index_history_catalog"));
}

export async function readIndexHistory(code: IndexCode, limit = 250): Promise<IndexHistory> {
  if (!validCode(code) || !Number.isInteger(limit) || limit < 1 || limit > 10000) {
    throw new Error("Invalid index history selection.");
  }
  const payload = await readonlyGet(
    "/api/v1/market-data/index-history/" + code + "/daily?limit=" + limit,
    "index_history_daily", { code, limit },
  );
  const record = assertRecord(payload);
  if (record.code !== code || record.provider !== "fred" ||
      record.unit !== "index_points" || record.data_kind !== "daily_close_only" ||
      !Array.isArray(record.observations) || typeof record.has_more !== "boolean" ||
      typeof record.name !== "string" || typeof record.source_url !== "string") {
    throw new Error("Invalid local index history series.");
  }
  const observations = record.observations.map(item => {
    const row = assertRecord(item);
    if (typeof row.session_date !== "string" ||
        !/^\d{4}-\d{2}-\d{2}$/.test(row.session_date) ||
        typeof row.close !== "string" || !Number.isFinite(Number(row.close)) ||
        Number(row.close) <= 0) throw new Error("Invalid index close observation.");
    return { session_date: row.session_date, close: row.close };
  });
  if (observations.some((row, i) => i > 0 && row.session_date <= observations[i - 1].session_date)) {
    throw new Error("Index close observations are out of order.");
  }
  return { ...record, observations } as IndexHistory;
}

export async function acquireIndexHistory(code: IndexCode): Promise<IndexImportResult> {
  if (!validCode(code) || !isTauri()) {
    throw new Error("Index downloads require the managed desktop.");
  }
  const raw = assertRecord(await invoke("index_history_acquire", { code }));
  if (raw.code !== code || raw.status !== "stored" || raw.provider !== "fred" ||
      !Number.isSafeInteger(raw.sessions) || !Number.isSafeInteger(raw.inserted) ||
      typeof raw.last_session !== "string") {
    throw new Error("Index download returned an unexpected response.");
  }
  return raw as IndexImportResult;
}
