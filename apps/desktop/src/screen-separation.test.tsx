import { parseMarketWorkspace } from "./market-workspace";
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { renderToStaticMarkup } from "react-dom/server";
import { MarketExplorer } from "./MarketExplorer";
import { DataAcquisitionPage } from "./DataAcquisitionPage";
import { ActionHistoryPage } from "./ActionHistoryPage";

const marketProps = {
  workspace: parseMarketWorkspace(null),
  limitReached: false,
  onActivateTab: () => undefined,
  onShowComparison: () => undefined,

  onCloseTab: () => undefined,
  onChooseInstrument: () => undefined,
  onUpdateView: () => undefined,
  onOpenAcquisition: () => undefined,
};

describe("navigation between research and data operations", () => {
  it("shows SP500 as the initial Market selection", () => {
    const market = renderToStaticMarkup(<MarketExplorer {...marketProps} />);
    expect(market).toContain("SP500");
    expect(market).toContain("Select comparison instruments");
  });

  it("keeps the Market layout responsive on desktop widths", () => {
    const market = renderToStaticMarkup(<MarketExplorer {...marketProps} />);
    expect(market).toContain("min-w-0");
    const source = readFileSync(join(import.meta.dirname, "MarketExplorer.tsx"), "utf8");
    expect(source).toContain("sm:grid-cols-2");
    expect(source).toContain("2xl:grid-cols");
  });

  it("keeps acquisition controls and action history off the Markets screen", () => {
    const market = renderToStaticMarkup(
      <MarketExplorer {...marketProps} />,
    );
    expect(market).toContain("Market Data Explorer");
    expect(market).toContain("Data Acquisition");
    expect(market).not.toContain("Fetch &amp; store");
    expect(market).not.toContain("Action History");
  });

  it("renders two distinct acquisition pages with non-overlapping controls", () => {
    const historical = renderToStaticMarkup(
      <DataAcquisitionPage mode="history" onViewMarket={() => undefined} />,
    );
    const daily = renderToStaticMarkup(
      <DataAcquisitionPage mode="daily" onViewMarket={() => undefined} />,
    );
    expect(historical).toContain("Historical Data");
    expect(historical).toContain("Index history acquisition");
    expect(historical).toContain("Fetch &amp; store");
    expect(historical).not.toContain("Daily acquisition");
    expect(daily).toContain("Daily &amp; Incremental Data");
    expect(daily).toContain("Daily Acquisition");
    expect(daily).not.toContain("Fetch &amp; store");
    expect(daily).not.toContain("Index history acquisition");
    const app = readFileSync(join(import.meta.dirname, "App.tsx"), "utf8");
    expect(app).toContain('id: "daily-acquisition"');
    expect(app).toContain('mode="history"');
    expect(app).toContain('mode="daily"');
  });

  it("keeps fetch controls on Data Acquisition and records on Action History", () => {
    const acquisition = renderToStaticMarkup(
      <DataAcquisitionPage onViewMarket={() => undefined} />,
    );
    expect(acquisition).toContain("Data Acquisition");
    expect(acquisition).toContain("Fetch &amp; store");
    expect(acquisition).toContain("Action History");
    expect(acquisition).not.toContain("Refresh history");
    expect(acquisition).not.toContain("Historical price table");
    const history = renderToStaticMarkup(<ActionHistoryPage onViewMarket={() => undefined} />);
    expect(history).toContain("Completed imports");
    expect(history).not.toContain("Fetch &amp; store");
  });
});
