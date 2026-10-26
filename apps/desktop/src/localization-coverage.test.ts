import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const screens = [
  "App.tsx", "AcquisitionCard.tsx", "ActionHistoryPage.tsx", "DataAcquisitionPage.tsx",
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
      for (const match of source.matchAll(/\\bt\\(\\s*["'`]([^"'`]+)["'`]\\s*\\)/g)) {
        const key = match[1];
        if (!catalog.includes(JSON.stringify(key)) && !catalog.includes(`"${key}"`)) {
          missing.push(`${screen}: ${key}`);
        }
      }
    }
    expect(missing).toEqual([]);
  });
});
