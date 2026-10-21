import { describe, expect, it } from "vitest";
import { parseBackendHealth } from "./health";

describe("parseBackendHealth", () => {
  it("accepts the typed Python health response", () => {
    expect(parseBackendHealth({ status: "ok", service: "finintel-backend", version: "0.1.0" }))
      .toEqual({ status: "ok", service: "finintel-backend", version: "0.1.0" });
  });

  it("rejects invalid and unexpected responses", () => {
    expect(() => parseBackendHealth(null)).toThrow();
    expect(() => parseBackendHealth({ status: "offline", service: "finintel-backend", version: "0.1.0" })).toThrow();
    expect(() => parseBackendHealth({ status: "ok", service: "other", version: "0.1.0" })).toThrow();
  });
});
