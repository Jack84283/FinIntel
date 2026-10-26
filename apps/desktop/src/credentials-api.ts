import { invoke, isTauri } from "@tauri-apps/api/core";

export type ProviderId = "alpha_vantage" | "alpaca_sip" | "tiingo" | "equibles";
export type CredentialField = "api_key" | "key_id" | "secret_key";
export type CredentialStatus = Record<ProviderId, Partial<Record<CredentialField, boolean>>>;

export function credentialsNativeAvailable(): boolean {
  return isTauri();
}

export async function credentialStatuses(): Promise<CredentialStatus> {
  if (!isTauri()) throw new Error("Provider credentials require the native desktop app.");
  return invoke<CredentialStatus>("credential_status");
}
export async function credentialSave(provider: ProviderId, field: CredentialField, secret: string): Promise<void> {
  if (!isTauri()) throw new Error("Provider credentials require the native desktop app.");
  await invoke("credential_set", { provider, field, secret });
}
export async function credentialRemove(provider: ProviderId, field: CredentialField): Promise<void> {
  if (!isTauri()) throw new Error("Provider credentials require the native desktop app.");
  await invoke("credential_delete", { provider, field });
}

export type ConnectionTestResult = {
  status: "connected" | "limited" | "authentication_failed" | "rate_limited" | "unreachable" | "invalid_response" | "not_configured";
  message: string;
  http_status: number | null;
  elapsed_ms: number;
};

export async function credentialTest(provider: ProviderId): Promise<ConnectionTestResult> {
  if (!isTauri()) throw new Error("Provider tests require the native desktop app.");
  return invoke<ConnectionTestResult>("credential_test", { provider });
}
