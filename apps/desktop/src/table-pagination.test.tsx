import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { boundedPage, filterRows, LocalDataTable, pageRows, TablePager, TABLE_PAGE_SIZE } from "./components/TablePagination";

const rows = Array.from({ length: 26 }, (_, index) => ({
  id: index + 1, name: index < 12 ? "Alpha" : index < 18 ? "Beta" : "Gamma",
}));

describe("unified ten-row data table controls", () => {
  it("uses exactly ten rows per page, preserves source ordering and clamps out-of-range pages", () => {
    expect(TABLE_PAGE_SIZE).toBe(10);
    expect(pageRows(rows, 0).map(row => row.id)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
    expect(pageRows(rows, 1).map(row => row.id)).toEqual([11, 12, 13, 14, 15, 16, 17, 18, 19, 20]);
    expect(pageRows(rows, 2).map(row => row.id)).toEqual([21, 22, 23, 24, 25, 26]);
    expect(pageRows(rows, 999).map(row => row.id)).toEqual([21, 22, 23, 24, 25, 26]);
    expect(boundedPage(-5, 26)).toBe(0);
    expect(boundedPage(500, 0)).toBe(0);
    expect(boundedPage(Number.NaN, 26)).toBe(0);
    expect(rows).toHaveLength(26);
  });

  it("filters the whole loaded collection before paging, not just the current page", () => {
    const beta = filterRows(rows, "  beta ", row => row.name);
    expect(beta).toHaveLength(6);
    expect(pageRows(beta, 0).map(row => row.id)).toEqual([13, 14, 15, 16, 17, 18]);
    const alpha = filterRows(rows, "ALPHA", row => row.name);
    expect(alpha).toHaveLength(12);
    expect(pageRows(alpha, 1).map(row => row.id)).toEqual([11, 12]);
    expect(filterRows(rows, "missing", row => row.name)).toEqual([]);
  });

  it("renders a labeled page jump, Next and Previous buttons and filter field", () => {
    const pager = renderToStaticMarkup(<TablePager page={1} total={26} onPageChange={() => undefined} />);
    expect(pager).toContain("Go to page");
    expect(pager).toContain("Previous");
    expect(pager).toContain("Next");
    expect(pager).toContain("11–20");
    const content = renderToStaticMarkup(
      <LocalDataTable rows={rows} label="Test records" searchable={row => row.name}>
        {visible => <table><tbody>{visible.map(row => <tr key={row.id}><td>{row.id}</td></tr>)}</tbody></table>}
      </LocalDataTable>,
    );
    expect(content).toContain("Search table values");
    expect(content).toContain("Test records");
    expect((content.match(/<tr>/g) ?? [])).toHaveLength(10);
  });
});
