import { invoke, isTauri } from "@tauri-apps/api/core";
import { parseBackendHealth, type BackendHealth } from "./health";

export async function fetchBackendHealth(): Promise<BackendHealth> {
  if (isTauri()) {
    // Webview has no direct permission to contact the Python API.
    return parseBackendHealth(await invoke<unknown>("backend_health"));
  }
  // Browser-only dev mode: Vite proxies /api to the loopback Python service.
  const response = await fetch("/api/v1/health", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Backend returned HTTP ${response.status}.`);
  }
  return parseBackendHealth(await response.json());
}
