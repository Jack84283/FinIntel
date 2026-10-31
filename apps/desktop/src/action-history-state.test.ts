import { describe, expect, it } from "vitest";
import { isCurrentDetailRequest } from "./action-history-state";

describe("Action History detail requests", () => {
  it("ignores responses from rows expanded before the current row", () => {
    expect(isCurrentDetailRequest(4, 3)).toBe(false);
    expect(isCurrentDetailRequest(4, 4)).toBe(true);
  });
});
