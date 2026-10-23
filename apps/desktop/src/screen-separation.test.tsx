import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { MarketExplorer } from "./MarketExplorer";
import { DataAcquisitionPage } from "./DataAcquisitionPage";
import { ActionHistoryPage } from "./ActionHistoryPage";

describe("navigation between research and data operations", () => {
  it("keeps acquisition controls and action history off the Markets screen", () => {
    const market = renderToStaticMarkup(
      <MarketExplorer onOpenAcquisition={() => undefined} />,
    );
    expect(market).toContain("Market Data Explorer");
    expect(market).toContain("Data Acquisition");
    expect(market).not.toContain("Fetch &amp; store");
    expect(market).not.toContain("Action History");
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
