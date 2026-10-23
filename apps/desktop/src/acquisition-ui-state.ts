/** Pure form-state rules for credential-aware acquisition UX. */

import type { AcquisitionProvider, AcquisitionStatus } from "./market-acquisition-api";
import type { AssetType } from "./symbol-catalog-api";

export function preferredAcquisitionProvider(status: AcquisitionStatus): AcquisitionProvider {
  if (status.alpaca_configured) return "alpaca_sip";
  if (status.key_configured) return "alpha_vantage";
  return "alpaca_sip";
}

export function acquisitionBlockReason({
  symbol,
  selectedType,
  provider,
  status,
  statusError,
}: {
  symbol: string;
  selectedType: AssetType | null;
  provider: AcquisitionProvider;
  status: AcquisitionStatus | null;
  statusError: string;
}): string | null {
  if (selectedType === "Index" || selectedType === "Mutual Fund") {
    return selectedType + " history needs a dedicated provider. Alpaca and Alpha Vantage daily APIs in FinIntel currently support Stock/ETF prices only. For example, SPY tracks the S&P 500 and QQQ tracks the Nasdaq-100, but ETF prices are not index levels.";
  }
  if (!symbol.trim()) {
    return "Select a stock or ETF symbol before fetching market data.";
  }
  if (!/^[A-Z0-9][A-Z0-9.-]{0,14}$/.test(symbol.trim().toUpperCase())) {
    return "Enter a valid stock or ETF ticker (for example AAPL).";
  }
  if (statusError) {
    return "The local Python backend is unavailable. Start or restart it on 127.0.0.1:8765 and select Recheck provider.";
  }
  if (!status) {
    return "The backend connection has not been verified yet. Select Recheck provider.";
  }
  if (provider === "alpaca_sip" && !status.alpaca_configured) {
    return "Alpaca requires FININTEL_ALPACA_API_KEY_ID and FININTEL_ALPACA_API_SECRET_KEY in the Python backend environment. Restart the backend, then select Recheck provider. Or switch to Alpha Vantage if its key is already configured.";
  }
  if (provider === "alpha_vantage" && !status.key_configured) {
    return "Alpha Vantage requires FININTEL_ALPHA_VANTAGE_API_KEY in the Python backend environment. Restart the backend, then select Recheck provider. Or switch to Alpaca if its credentials are configured.";
  }
  return null;
}
