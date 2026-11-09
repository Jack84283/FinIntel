import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { renderToStaticMarkup } from "react-dom/server";
import { PageHeader } from "./PageHeader";
import { DataAcquisitionPage } from "./DataAcquisitionPage";
import { translate } from "./localization";

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

  it("keeps desktop navigation compact without reducing readable labels", () => {
    const source = readFileSync(join(import.meta.dirname, "App.tsx"), "utf8");
    expect(source).toContain('bg-muted px-3 py-4 focus-visible');
    expect(source).toContain('className="mx-3 mb-6 grid h-11');
    expect(source).toContain('className="mb-3 flex flex-col gap-1"');
    expect(source).toContain('className="mb-1 px-3 text-sm font-bold');
    expect(source).toContain("rounded-md px-3 py-2 text-left");
    expect(source).toContain('className="flex flex-col gap-1 text-sm"');
    expect(source).toContain('lg:overflow-y-auto lg:overscroll-y-contain');
  });

  it("starts the sticky desktop page header flush against the scroll pane top", () => {
    const source = readFileSync(join(import.meta.dirname, "App.tsx"), "utf8");
    const main = source.match(/<main\b[^>]*>/)?.[0] ?? "";
    expect(main).toContain("py-4");
    expect(main).toContain("lg:pt-0 lg:pb-5");
    expect(main).not.toContain("lg:py-5");
    const header = renderToStaticMarkup(
      <PageHeader id="header-top" breadcrumb="FinIntel" title="Header" subtitle="Subtitle" />,
    );
    expect(header).toContain("sticky top-0");
  });

  it("provides keyboard-focusable, visibly focused independent scroll regions and bilingual names", () => {
    const source = readFileSync(join(import.meta.dirname, "App.tsx"), "utf8");
    const sidebar = source.match(/<aside\b[^>]*>/)?.[0] ?? "";
    const content = source.match(/<main\b[^>]*>/)?.[0] ?? "";
    for (const scroller of [sidebar, content]) {
      expect(scroller).toContain("tabIndex={0}");
      expect(scroller).toContain("focus-visible:ring-2");
      expect(scroller).toContain("focus-visible:ring-inset");
      expect(scroller).toContain("focus-visible:ring-ring");
    }
    expect(sidebar).toContain('aria-label={t("Main navigation")}');
    expect(content).toContain('aria-label={t("Main content")}');
    expect(translate("en", "Main content")).toBe("Main content");
    expect(translate("zh-CN", "Main content")).toBe("主要内容");
    expect(translate("zh-CN", "Main navigation")).toBe("主导航");
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
