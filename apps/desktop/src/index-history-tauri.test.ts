import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { invokeMock, isTauriMock } = vi.hoisted(() => ({
  invokeMock: vi.fn(),
  isTauriMock: vi.fn(),
}));

vi.mock("@tauri-apps/api/core", () => ({
  invoke: invokeMock,
  isTauri: isTauriMock,
}));

import { acquireIndexHistory } from "./index-history-api";

describe("managed index history acquisition", () => {
  beforeEach(() => {
    isTauriMock.mockReturnValue(true);
    invokeMock.mockResolvedValue({
      code: "SP500",
      provider: "fred",
      status: "stored",
      sessions: 2,
      inserted: 2,
      updated: 0,
      unchanged: 0,
      first_session: "2026-10-21",
      last_session: "2026-10-23",
      source_url: "https://fred.stlouisfed.org/series/SP500",
    });
  });

  afterEach(() => vi.restoreAllMocks());

  it("passes an explicit bounded backfill range to Tauri", async () => {
    await acquireIndexHistory("SP500", "2026-10-21", "2026-10-23");

    expect(invokeMock).toHaveBeenCalledWith("index_history_acquire", {
      code: "SP500",
      start: "2026-10-21",
      end: "2026-10-23",
    });
  });

  it("rejects reversed ranges before invoking the native command", async () => {
    await expect(acquireIndexHistory("SP500", "2026-10-23", "2026-10-21"))
      .rejects.toThrow("Invalid index history date range.");
    expect(invokeMock).not.toHaveBeenCalled();
  });
});
