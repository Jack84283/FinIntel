import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { IndexLineChart } from "./market-chart";

describe("index chart", () => {
  it("renders a close-only line chart without OHLCV visualizations", () => {
    const markup = renderToStaticMarkup(<IndexLineChart observations={[
      { session_date: "2026-10-26", close: "6800.25" },
      { session_date: "2026-10-27", close: "6878.25" },
    ]} />);

    expect(markup).toContain("Historical index closing-level line chart");
    expect(markup).toContain("2026-10-27: 6878.25");
    expect(markup).not.toContain("candlestick");
    expect(markup).not.toContain("Trading volume");
  });
});
