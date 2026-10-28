import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { translate } from "./localization";

const screens = [
  "App.tsx", "AcquisitionCard.tsx", "ActionHistoryPage.tsx", "DataAcquisitionPage.tsx",
  "DatabaseBackupCard.tsx",
  "TechnicalIndicatorsCard.tsx", "IndexHistoryCard.tsx",
  "DataValidationPage.tsx", "IndexMembershipPage.tsx", "MarketExplorer.tsx",
  "PreferencesPage.tsx", "ProviderCredentialsCard.tsx", "SymbolSelector.tsx",
  "components/MarketDatePicker.tsx",
];

describe("localization catalog coverage", () => {
  it("provides English or shared Chinese translations for all literal t() keys", () => {
    const catalog = readFileSync(join(import.meta.dirname, "localization.ts"), "utf8");
    const missing: string[] = [];
    for (const screen of screens) {
      const source = readFileSync(join(import.meta.dirname, screen), "utf8");
      for (const match of source.matchAll(/\bt\(\s*["'`]([^"'`]+)["'`]\s*\)/g)) {
        const key = match[1];
        if (!catalog.includes(JSON.stringify(key)) && !catalog.includes(`"${key}"`)) {
          missing.push(`${screen}: ${key}`);
        }
      }
    }
    expect(missing).toEqual([]);
  });
});

describe("Chinese translations on commonly overlooked routes", () => {
  it("translates chart, search, market, acquisition, and toast text", () => {
    const keys = [
      "Recheck connection", "No observations in this date range.",
      "Historical", "candlestick chart showing", "intervals",
      "No sessions in selected range", "historical end-of-day data",
      "Local symbol search unavailable", "Stored",
      "instruments are reference entries only. Select a Stock or ETF to fetch daily prices from",
      "Credential operation failed. See details below.",
      "Connection test failed. See details below.",
    ];
    for (const key of keys) {
      expect(translate("zh-CN", key), key).not.toBe(key);
    }
  });

  it("does not leave literal button and status strings on key routes", () => {
    for (const screen of ["App.tsx", "DataAcquisitionPage.tsx", "market-chart.tsx"]) {
      const source = readFileSync(join(import.meta.dirname, screen), "utf8");
      expect(source).not.toMatch(/>\\s*Recheck connection\\s*</);
      expect(source).not.toMatch(/>\\s*No observations in this date range\\.\\s*</);
      expect(source).not.toMatch(/>\\s*Stored\\s*\\{/);
    }
  });
});

describe("Chinese UI checklist for sidebar and Market", () => {
  it("translates the ten reported groups of terms", () => {
    const keys = [
      "Workspace", "Data & Operations", "Workspace & Preferences",
      "Recheck connection", "Reload local data", "Data Acquisition",
      "loaded sessions", "Compared with prior loaded session",
      "Bounded result · more stored", "Available stored data",
      "Display is limited to 4,000 latest records. Use date filters to examine older stored sessions.",
      "historical end-of-day data", "raw unadjusted",
      "Trading volume", "aggregation", "Chart represents",
      "actual daily sessions across", "loaded rows",
    ];
    for (const key of keys) expect(translate("zh-CN", key), key).not.toBe(key);
  });
});
