import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { renderToStaticMarkup } from "react-dom/server";
import { PageHeader } from "./PageHeader";
import { DataAcquisitionPage } from "./DataAcquisitionPage";

describe("independent desktop navigation and page headers", () => {
  it("keeps the shared header visible above the page's scrolling content", () => {
    const markup = renderToStaticMarkup(
      <PageHeader
        id="layout-test-title"
        breadcrumb="FinIntel / Test"
        title="Test"
        subtitle="Description"
      />,
    );
    expect(markup).toContain("sticky top-0 z-20");
    expect(markup).toContain("bg-background/95");
    expect(markup).toContain('id="layout-test-title"');
  });

  it("widens the desktop sidebar and gives navigation and content independent scroll containers", () => {
    const app = readFileSync(join(import.meta.dirname, "App.tsx"), "utf8");
    expect(app).toContain("lg:grid-cols-[256px_minmax(0,1fr)]");
    expect(app).toContain("lg:h-dvh lg:min-h-0 lg:grid-cols");
    expect(app).toContain("lg:overflow-hidden");
    expect(app).toContain("overflow-y-auto overscroll-y-contain");
    expect(app).toContain('ref={mainScrollRef}');
    expect(app).toContain("lg:overflow-y-auto lg:overscroll-y-contain");
    expect(app).toContain("mainScrollRef.current.scrollTop = 0");
    expect(app).toContain("[screen]");
  });

  it("keeps full-width Daily guidance when stacked and the historical wide-screen columns", () => {
    const daily = renderToStaticMarkup(
      <DataAcquisitionPage mode="daily" onViewMarket={() => undefined} />,
    );
    const history = renderToStaticMarkup(
      <DataAcquisitionPage mode="history" onViewMarket={() => undefined} />,
    );
    expect(daily).toContain('class="grid min-w-0 items-start gap-2 2xl:grid-cols-[minmax(0,1fr)_300px]"');
    expect(daily).toContain('class="grid min-w-0 gap-2 grid-cols-1" aria-label="Acquisition guidance"');
    expect(daily).toContain("Data integrity");
    expect(history).toContain("2xl:grid-cols-[minmax(0,1fr)_300px]");
    expect(history).toContain("sm:grid-cols-2 2xl:grid-cols-1");
  });
});
