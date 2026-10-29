import type { IndexCode } from "./index-history-api";
import type { AcquisitionProvider } from "./market-acquisition-api";

export interface EquityMarketSelection {
  kind: "equity";
  symbol: string;
  provider: AcquisitionProvider;
}

export interface IndexMarketSelection {
  kind: "index";
  code: IndexCode;
}

export type MarketSelection = EquityMarketSelection | IndexMarketSelection;

export const DEFAULT_MARKET_SELECTION: IndexMarketSelection = {
  kind: "index",
  code: "SP500",
};

export function resolveMarketSelection(
  current: MarketSelection | null | undefined,
  requested?: MarketSelection,
): MarketSelection {
  return requested ?? current ?? DEFAULT_MARKET_SELECTION;
}
