/** Read-only, strictly validated contracts for locally persisted historical prices. */

export interface MarketInstrument {
  symbol: string;
  provider: string;
  frequency: string;
  adjustment: string;
  data_kind: string;
  sessions: number;
  first_session: string | null;
  last_session: string | null;
}

export interface DailyBar {
  session_date: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: number;
}

export interface DailySeries {
  symbol: string;
  provider: string;
  frequency: string;
  price_adjustment: string;
  data_kind: string;
  source_url: string | null;
  provider_terms_url: string | null;
  retrieved_at: string | null;
  provider_last_refreshed: string | null;
  has_more: boolean;
  observations: DailyBar[];
}

export interface ImportRecord {
  id: number;
  symbol: string;
  provider: string;
  imported_at: string;
  retrieved_at: string;
  first_session: string;
  last_session: string;
  observations: number;
  inserted: number;
  updated: number;
  unchanged: number;
  stale: number;
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("The local backend returned an invalid market-data response.");
  }
  return value as Record<string, unknown>;
}

function text(value: unknown): string {
  if (typeof value !== "string") throw new Error("Market-data response contains invalid text.");
  return value;
}

function nullableText(value: unknown): string | null {
  return value === null ? null : text(value);
}

function count(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    throw new Error("Market-data response contains an invalid count.");
  }
  return value;
}

function decimal(value: unknown): string {
  const v = text(value);
  if (!/^(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(v) || !Number.isFinite(Number(v)) || Number(v) <= 0) {
    throw new Error("Market-data response contains an invalid historical price.");
  }
  return v;
}

function date(value: unknown): string {
  const v = text(value);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(v) || Number.isNaN(Date.parse(v + "T00:00:00Z"))) {
    throw new Error("Market-data response contains an invalid trading date.");
  }
  return v;
}

function rows(value: unknown, max: number): unknown[] {
  if (!Array.isArray(value) || value.length > max) {
    throw new Error("Market-data response exceeds the expected record limit.");
  }
  return value;
}

export function parseInstruments(value: unknown): MarketInstrument[] {
  return rows(value, 100000).map((entry) => {
    const item = record(entry);
    return {
      symbol: text(item.symbol),
      provider: text(item.provider),
      frequency: text(item.frequency),
      adjustment: text(item.adjustment),
      data_kind: text(item.data_kind),
      sessions: count(item.sessions),
      first_session: nullableText(item.first_session),
      last_session: nullableText(item.last_session),
    };
  });
}

export function parseDaily(value: unknown): DailySeries {
  const item = record(value);
  if (typeof item.has_more !== "boolean") throw new Error("Invalid market history metadata.");
  let previousDate = "";
  const observations = rows(item.observations, 4000).map((entry) => {
    const bar = record(entry);
    const parsed: DailyBar = {
      session_date: date(bar.session_date),
      open: decimal(bar.open),
      high: decimal(bar.high),
      low: decimal(bar.low),
      close: decimal(bar.close),
      volume: count(bar.volume),
    };
    if (
      parsed.session_date <= previousDate ||
      Number(parsed.low) > Math.min(Number(parsed.open), Number(parsed.close)) ||
      Number(parsed.high) < Math.max(Number(parsed.open), Number(parsed.close))
    ) {
      throw new Error("Historical prices failed chronological or OHLC integrity checks.");
    }
    previousDate = parsed.session_date;
    return parsed;
  });
  return {
    symbol: text(item.symbol),
    provider: text(item.provider),
    frequency: text(item.frequency),
    price_adjustment: text(item.price_adjustment),
    data_kind: text(item.data_kind),
    source_url: nullableText(item.source_url),
    provider_terms_url: nullableText(item.provider_terms_url),
    retrieved_at: nullableText(item.retrieved_at),
    provider_last_refreshed: nullableText(item.provider_last_refreshed),
    has_more: item.has_more,
    observations,
  };
}

export function parseImports(value: unknown): ImportRecord[] {
  return rows(value, 100).map((entry) => {
    const item = record(entry);
    return {
      id: count(item.id),
      symbol: text(item.symbol),
      provider: text(item.provider),
      imported_at: text(item.imported_at),
      retrieved_at: text(item.retrieved_at),
      first_session: date(item.first_session),
      last_session: date(item.last_session),
      observations: count(item.observations),
      inserted: count(item.inserted),
      updated: count(item.updated),
      unchanged: count(item.unchanged),
      stale: count(item.stale),
    };
  });
}

export function sortableBars(bars: DailyBar[], field: "session_date" | "close" | "volume", descending: boolean): DailyBar[] {
  const copy = [...bars];
  copy.sort((a, b) => {
    const compared = field === "session_date"
      ? a.session_date.localeCompare(b.session_date)
      : Number(a[field]) - Number(b[field]);
    return descending ? -compared : compared;
  });
  return copy;
}
