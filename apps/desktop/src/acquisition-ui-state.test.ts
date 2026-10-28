import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { translate } from "./localization";
import { acquisitionBlockReason, preferredAcquisitionProvider } from "./acquisition-ui-state";
import type { AcquisitionStatus } from "./market-acquisition-api";

const status: AcquisitionStatus = {
  provider: "alpha_vantage",
  key_configured: true,
  alpaca_configured: false,
  tiingo_configured: false,
  tiingo_key_environment_variable: "FININTEL_TIINGO_API_KEY",
  requires_explicit_action: true,
  api_key_environment_variable: "FININTEL_ALPHA_VANTAGE_API_KEY",
  alpaca_key_environment_variable: "FININTEL_ALPACA_API_KEY_ID",
  alpaca_secret_environment_variable: "FININTEL_ALPACA_API_SECRET_KEY",
  full_requires_premium: true,
};

describe("acquisition button eligibility", () => {
  it("defaults to the configured provider instead of unconfigured Alpaca", () => {
    expect(preferredAcquisitionProvider(status)).toBe("alpha_vantage");
    expect(preferredAcquisitionProvider({ ...status, alpaca_configured: true })).toBe("alpaca_sip");
    expect(preferredAcquisitionProvider({ ...status, key_configured: false })).toBe("alpaca_sip");
  });

  it("allows the configured Alpha Vantage source", () => {
    expect(acquisitionBlockReason({
      symbol: "AAPL", selectedType: "Stock", provider: "alpha_vantage",
      status, statusError: "",
    })).toBeNull();
  });

  it("provides the exact missing-symbol message used for warning toast and inline error", () => {
    const reason = acquisitionBlockReason({
      symbol: "", selectedType: null, provider: "alpha_vantage",
      status, statusError: "",
    });
    expect(reason).toBe("Select a stock or ETF symbol before fetching market data.");
    expect(translate("zh-CN", reason!)).toBe("获取市场数据前，请先选择股票或 ETF 代码。");
    const card = readFileSync(join(import.meta.dirname, "AcquisitionCard.tsx"), "utf8");
    expect(card).toContain('notify(t(blockReason), "warning")');
    expect(card).toContain('setError(t(blockReason))');
    expect(card).toContain('type="submit"');
  });

  it("explains missing provider credentials rather than silently disabling", () => {
    const reason = acquisitionBlockReason({
      symbol: "SPY", selectedType: "ETF", provider: "alpaca_sip",
      status, statusError: "",
    });
    expect(reason).toContain("FININTEL_ALPACA_API_KEY_ID");
    expect(reason).toContain("Alpha Vantage");
  });

  it("explains missing tickers, unsupported types and offline backend", () => {
    expect(acquisitionBlockReason({
      symbol: "", selectedType: null, provider: "alpha_vantage",
      status, statusError: "",
    })).toContain("Select a stock");
    expect(acquisitionBlockReason({
      symbol: "FXAIX", selectedType: "Mutual Fund", provider: "alpha_vantage",
      status, statusError: "",
    })).toContain("dedicated provider");
    expect(acquisitionBlockReason({
      symbol: "AAPL", selectedType: "Stock", provider: "alpaca_sip",
      status: null, statusError: "offline",
    })).toContain("Python backend");
  });
});
