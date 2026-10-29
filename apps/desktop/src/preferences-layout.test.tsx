import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { PreferencesPage } from "./PreferencesPage";

describe("Preferences layout", () => {
  it("keeps Historical market data in the right-column card order", () => {
    const markup = renderToStaticMarkup(
      <PreferencesPage
        backend={{ kind: "connected", version: "test" }}
        onRecheckBackend={() => undefined}
        onOpenAcquisition={() => undefined}
        onLanguageChange={() => undefined}
      />,
    );

    const leftAppearance = markup.indexOf("Appearance");
    const leftCredentials = markup.indexOf("Data provider API keys");
    const leftBackup = markup.indexOf("Database backup &amp; restore");
    const rightBackend = markup.indexOf("Local backend");
    const rightHistorical = markup.indexOf("Historical market data");
    const rightPrivacy = markup.indexOf("Privacy &amp; storage");

    expect(leftAppearance).toBeLessThan(leftCredentials);
    expect(leftCredentials).toBeLessThan(leftBackup);
    expect(leftBackup).toBeLessThan(rightBackend);
    expect(rightBackend).toBeLessThan(rightHistorical);
    expect(rightHistorical).toBeLessThan(rightPrivacy);
    expect(markup).toContain("flex min-w-0 flex-col gap-2");
  });
});
