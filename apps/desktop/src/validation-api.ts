import { invoke, isTauri } from "@tauri-apps/api/core";

export type ValidationCheck = "coverage" | "integrity" | "corporate" | "provider" | "performance";
export interface ValidationJob {
  id?: string;
  checks?: ValidationCheck[];
  status: "idle" | "running" | "queued" | "completed" | "failed";
  stage?: string;
  started_at?: string;
  finished_at?: string | null;
  results?: Record<string, unknown> | null;
  error?: string | null;
}

async function request(path: string, command: string, checks?: ValidationCheck[]): Promise<ValidationJob> {
  if (isTauri()) return invoke<ValidationJob>(command, checks ? { checks } : undefined);
  const response = await fetch(path, checks ? {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ checks }),
  } : { cache: "no-store" });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail ?? `Backend HTTP ${response.status}`);
  }
  return response.json();
}

export const fetchValidationStatus = () => request("/api/v1/validation/status", "validation_status");
export const startValidation = (checks: ValidationCheck[]) =>
  request("/api/v1/validation/run", "validation_start", checks);
