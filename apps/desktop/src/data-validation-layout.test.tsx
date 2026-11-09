import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { DataValidationPage } from "./DataValidationPage";

describe("Data Validation check layout", () => {
  it("groups all six selectable checks as divided rows within one card", () => {
    const markup = renderToStaticMarkup(<DataValidationPage />);
    expect(markup).toContain('aria-label="Validation checks"');
    expect(markup).toContain('class="divide-y divide-border" aria-label="Validation checks"');
    expect(markup.match(/type="checkbox"/g)).toHaveLength(6);
    expect(markup.match(/class="flex cursor-pointer items-start gap-3 py-3 first:pt-0 last:pb-0"/g)).toHaveLength(6);
    expect(markup).not.toContain("items-start gap-3 rounded-md border border-border p-3");
    expect(markup).toContain("Run selected checks");
  });

  it("keeps the corporate-reference and latest-results cards separate", () => {
    const markup = renderToStaticMarkup(<DataValidationPage />);
    expect(markup.match(/data-slot="card"/g)).toHaveLength(3);
    expect(markup).toContain("Split and dividend reference");
    expect(markup).toContain("Latest validation");
    expect(markup).toContain("Verify corporate events");
  });
});
