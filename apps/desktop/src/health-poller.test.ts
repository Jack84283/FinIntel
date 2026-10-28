import { afterEach, describe, expect, it, vi } from "vitest";
import { createHealthPoller, type HealthConnection } from "./health-poller";

const healthy = { status: "ok" as const, service: "finintel-backend" as const, version: "test" };

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

async function flush() {
  await Promise.resolve();
  await Promise.resolve();
}

describe("createHealthPoller", () => {
  afterEach(() => vi.useRealTimers());

  it("ignores a delayed background failure after a manual recovery", async () => {
    vi.useFakeTimers();
    const initial = deferred<typeof healthy>();
    const background = deferred<typeof healthy>();
    const manual = deferred<typeof healthy>();
    const check = vi.fn()
      .mockReturnValueOnce(initial.promise)
      .mockReturnValueOnce(background.promise)
      .mockReturnValueOnce(manual.promise);
    const states: HealthConnection[] = [];
    const poller = createHealthPoller(check, (state) => states.push(state));

    initial.resolve(healthy);
    await flush();
    vi.advanceTimersByTime(5000);
    await flush();
    expect(check).toHaveBeenCalledTimes(2);

    const manualRun = poller.checkNow();
    expect(states.at(-1)).toEqual({ kind: "loading" });
    manual.resolve({ ...healthy, version: "recovered" });
    await manualRun;
    background.reject(new Error("stale outage"));
    await flush();

    expect(states.at(-1)).toEqual({
      kind: "connected",
      health: { ...healthy, version: "recovered" },
    });
    poller.dispose();
  });

  it("does not overlap background requests and recovers after a connection loss", async () => {
    vi.useFakeTimers();
    const initial = deferred<typeof healthy>();
    const outage = deferred<typeof healthy>();
    const recovery = deferred<typeof healthy>();
    const check = vi.fn()
      .mockReturnValueOnce(initial.promise)
      .mockReturnValueOnce(outage.promise)
      .mockReturnValueOnce(recovery.promise);
    const states: HealthConnection[] = [];
    const poller = createHealthPoller(check, (state) => states.push(state));

    initial.resolve(healthy);
    await flush();
    vi.advanceTimersByTime(5000);
    await flush();
    vi.advanceTimersByTime(5000);
    await flush();
    expect(check).toHaveBeenCalledTimes(2);

    outage.reject(new Error("connection lost"));
    await flush();
    expect(states.at(-1)).toEqual({ kind: "disconnected", message: "connection lost" });

    vi.advanceTimersByTime(5000);
    await flush();
    expect(check).toHaveBeenCalledTimes(3);
    recovery.resolve({ ...healthy, version: "recovered" });
    await flush();
    expect(states.at(-1)?.kind).toBe("connected");
    poller.dispose();
  });

  it("stops polling and ignores completion after disposal", async () => {
    vi.useFakeTimers();
    const initial = deferred<typeof healthy>();
    const check = vi.fn().mockReturnValue(initial.promise);
    const states: HealthConnection[] = [];
    const poller = createHealthPoller(check, (state) => states.push(state));
    poller.dispose();
    vi.advanceTimersByTime(15000);
    initial.resolve(healthy);
    await flush();
    expect(check).toHaveBeenCalledTimes(1);
    expect(states).toEqual([{ kind: "loading" }]);
  });
});
