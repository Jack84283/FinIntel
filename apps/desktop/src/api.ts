import { invoke, isTauri } from "@tauri-apps/api/core";
import { parseBackendHealth, type BackendHealth } from "./health";

export async function fetchBackendHealth(): Promise<BackendHealth> {
  if (isTauri()) {
    // Webview has no direct permission to contact the Python API.
    for (let attempt = 0; attempt < 16; attempt += 1) {
      try {
        return parseBackendHealth(await invoke<unknown>("backend_health"));
      } catch (error) {
        if (attempt === 15) throw error;
        await new Promise((resolve) => setTimeout(resolve, 300));
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
