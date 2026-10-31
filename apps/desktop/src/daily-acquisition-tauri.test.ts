import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { invokeMock, isTauriMock } = vi.hoisted(() => ({
  invokeMock: vi.fn(),
  isTauriMock: vi.fn(),
}));

vi.mock("@tauri-apps/api/core", () => ({
  invoke: invokeMock,
  isTauri: isTauriMock,
}));

import { searchDailyHistory } from "./daily-acquisition-api";

const emptyHistory = { items: [], total: 0, limit: 20, offset: 0 };

describe("daily acquisition history transport", () => {
  beforeEach(() => {
    invokeMock.mockResolvedValue(emptyHistory);
    isTauriMock.mockReturnValue(true);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("uses the camelCase Tauri argument, including an empty date filter", async () => {
    await searchDailyHistory("", "", "", "", "", 0);

    expect(invokeMock).toHaveBeenCalledWith("daily_acquisition_history", {
      q: "",
      provider: "",
      status: "",
      kind: "",
      executionDate: "",
      offset: 0,
      limit: 20,
    });
  });

  it("keeps the snake_case execution_date query for HTTP", async () => {
    isTauriMock.mockReturnValue(false);
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => emptyHistory }));
    vi.stubGlobal("fetch", fetchMock);

    await searchDailyHistory("", "", "", "", "", 0);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/daily-acquisition/history?q=&provider=&status=&kind=&execution_date=&offset=0&limit=20",
      { cache: "no-store" },
    );
  });
});
