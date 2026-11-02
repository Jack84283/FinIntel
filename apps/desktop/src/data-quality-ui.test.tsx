import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { DataQualityPage } from "./DataQualityPage";
import { translate } from "./localization";

describe("Data Quality workspace", () => {
  it("starts index-first at S&P 500 with scan, filters and separate correction review", () => {
    const markup = renderToStaticMarkup(<DataQualityPage />);
    expect(markup).toContain("Data Quality");
    expect(markup).toContain("S&amp;P 500 (SP500)");
    expect(markup).toContain('value="SP500"');
    expect(markup).toContain("Run / resume quality scan");
    expect(markup).toContain("Findings &amp; evidence");
    expect(markup).toContain("Correction overlays");
    expect(markup).toContain("Export CSV");
    expect(markup).toContain('aria-label="Instrument scope"');
    expect(markup).not.toContain("Approved correction applied");
  });

  it("supplies actual Simplified Chinese text for visible controls and safety warnings", () => {
    for (const key of [
      "Data Quality", "Review decision", "Correction overlays", "Approve overlay",
      "Reverse overlay", "Sourced events", "Suspected events", "Partial scan",
      "Results may be incomplete. Candidates are not confirmed defects.",
      "Exports include up to 5,000 filtered findings with allowlisted, redacted evidence.",
      "No provider credentials or raw provider payloads are exported. No unapproved correction changes market history.",
    ]) {
      expect(translate("zh-CN", key), key).not.toBe(key);
    }
  });
});
