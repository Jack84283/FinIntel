import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { MarketExplorer } from "./MarketExplorer";
import { DataAcquisitionPage } from "./DataAcquisitionPage";

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

  it("groups fetch controls and successful ingestion history on Data Acquisition", () => {
    const acquisition = renderToStaticMarkup(
      <DataAcquisitionPage onViewMarket={() => undefined} />,
    );
    expect(acquisition).toContain("Data Acquisition");
    expect(acquisition).toContain("Fetch &amp; store");
    expect(acquisition).toContain("Action History");
    expect(acquisition).not.toContain("Historical price table");
    expect(acquisition).toContain("failed requests are not yet recorded");
  });
});
