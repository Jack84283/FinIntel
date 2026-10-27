import { invoke, isTauri } from "@tauri-apps/api/core";
import { parseBackendHealth, type BackendHealth } from "./health";

export async function fetchBackendHealth(): Promise<BackendHealth> {
  if (isTauri()) {
    // Packaged sidecar startup can take longer on a cold launch.
    // Authorization errors mean a mismatched desktop session, not slow startup.
    for (let attempt = 0; attempt < 60; attempt += 1) {
      try {
        return parseBackendHealth(await invoke<unknown>("backend_health"));
      } catch (error) {
        const message = String(error);
        if (message.includes("matching desktop authorization") || attempt === 59) throw error;
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
    }
    throw new Error("Local backend startup failed.");
  }
  // Browser-only dev mode: Vite proxies /api to the loopback Python service.
  const response = await fetch("/api/v1/health", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Backend returned HTTP ${response.status}.`);
  }
  return parseBackendHealth(await response.json());
}
