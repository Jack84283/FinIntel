import type { BackendHealth } from "./health";

export type HealthConnection =
  | { kind: "loading" }
  | { kind: "connected"; health: BackendHealth }
  | { kind: "disconnected"; message: string };

type HealthCheck = () => Promise<BackendHealth>;
type StateListener = (connection: HealthConnection) => void;

/** Poll backend health without allowing stale background results to win. */
export function createHealthPoller(
  check: HealthCheck,
  onState: StateListener,
  intervalMs = 5000,
) {
  let disposed = false;
  let sequence = 0;
  let latestManualSequence = 0;
  let manualInFlight = false;
  let backgroundInFlight = false;

  const run = async (background: boolean): Promise<void> => {
    if (disposed || (background && (manualInFlight || backgroundInFlight))) return;
    const requestSequence = ++sequence;
    if (background) {
      backgroundInFlight = true;
    } else {
      manualInFlight = true;
      latestManualSequence = requestSequence;
      onState({ kind: "loading" });
    }
    try {
      const health = await check();
      if (
        !disposed
        && requestSequence === sequence
        && !(background && latestManualSequence > requestSequence)
      ) {
        onState({ kind: "connected", health });
      }
    } catch (error: unknown) {
      if (
        !disposed
        && requestSequence === sequence
        && !(background && latestManualSequence > requestSequence)
      ) {
        onState({
          kind: "disconnected",
          message: error instanceof Error ? error.message : String(error),
        });
      }
    } finally {
      if (background) backgroundInFlight = false;
      else manualInFlight = false;
    }
  };

  const timer = globalThis.setInterval(() => { void run(true); }, intervalMs);
  void run(false);

  return {
    checkNow: () => run(false),
    dispose: () => {
      disposed = true;
      globalThis.clearInterval(timer);
    },
  };
}
