import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  credentialRemove, credentialSave, credentialStatuses, credentialTest, credentialsNativeAvailable,
  type ConnectionTestResult,
  type CredentialField, type CredentialStatus, type ProviderId,
} from "./credentials-api";
import { useLocale } from "./localization";
import { useNotify } from "./notifications";

const providers: { id: ProviderId; title: string; fields: { id: CredentialField; label: string }[] }[] = [
  { id: "alpha_vantage", title: "Alpha Vantage", fields: [{ id: "api_key", label: "API key" }] },
  { id: "alpaca_sip", title: "Alpaca SIP", fields: [{ id: "key_id", label: "Key ID" }, { id: "secret_key", label: "Secret key" }] },
  { id: "tiingo", title: "Tiingo", fields: [{ id: "api_key", label: "API key" }] },
  { id: "equibles", title: "Equibles", fields: [{ id: "api_key", label: "API key" }] },
];

export function ProviderCredentialsCard() {
  const { t } = useLocale();
  const notify = useNotify();
  const [status, setStatus] = useState<CredentialStatus | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState<ProviderId | null>(null);
  const [testResults, setTestResults] = useState<Partial<Record<ProviderId, ConnectionTestResult>>>({});
  const [error, setError] = useState("");
  const native = credentialsNativeAvailable();

  const reload = useCallback(async () => {
    if (!native) return;
    try {
      setStatus(await credentialStatuses());
      setError("");
    } catch (reason) {
      setStatus(null);
      setError(String(reason));
      throw reason;
    }
  }, [native]);

  useEffect(() => { void reload().catch(() => {}); }, [reload]);

  const modify = async (provider: ProviderId, field: CredentialField, remove: boolean) => {
    const key = provider + ":" + field;
    const secret = draft[key] || "";
    if (!remove && !secret.trim()) return;
    setBusy(true);
    setError("");
    try {
      if (remove) await credentialRemove(provider, field);
      else await credentialSave(provider, field, secret);
      setTestResults(previous => ({ ...previous, [provider]: undefined }));
      await reload();
      notify(t(remove ? "Credential deleted." : "Credential saved securely."), "success");
    } catch (reason) {
      setError(String(reason));
      notify(t("Credential operation failed. See details below."), "error");
    } finally {
      setDraft(previous => ({ ...previous, [key]: "" }));
      setBusy(false);
    }
  };

  const test = async (provider: ProviderId) => {
    setTesting(provider);
    setError("");
    setTestResults(previous => ({ ...previous, [provider]: undefined }));
    try {
      const result = await credentialTest(provider);
      setTestResults(previous => ({ ...previous, [provider]: result }));
      notify(`${provider}: ${t(result.status)}`, result.status === "connected" ? "success" : result.status === "rate_limited" || result.status === "limited" ? "warning" : "error");
    } catch (reason) {
      setError(String(reason));
      notify(t("Connection test failed. See details below."), "error");
    } finally {
      setTesting(null);
    }
  };

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>{t("Data provider API keys")}</CardTitle>
        <CardDescription>{t("Credentials are stored in your OS credential vault, never in SQLite.")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {!native && <p className="text-sm text-muted-foreground">{t("Use the packaged FinIntel desktop to manage API keys.")}</p>}
        {native && <>
          {providers.map(provider => (
            <div key={provider.id} className="space-y-3 border-b border-border pb-3 last:border-0">
              <h3 className="text-sm font-semibold">{provider.title}</h3>
              {provider.fields.map((field, fieldIndex) => {
                const key = provider.id + ":" + field.id;
                const configured = status?.[provider.id]?.[field.id] === true;
                return (
                  <div key={key} className="grid gap-2 sm:grid-cols-[100px_minmax(120px,1fr)_auto_auto_auto] sm:items-center">
                    <label htmlFor={"credential-" + key} className="text-xs">{t(field.label)}</label>
                    <input id={"credential-" + key} type="password" autoComplete="off" spellCheck={false}
                      maxLength={4096} value={draft[key] || ""}
                      onChange={event => setDraft(previous => ({ ...previous, [key]: event.target.value }))}
                      placeholder={t(configured ? "Enter replacement key" : "Enter API credential")}
                      className="h-9 min-w-0 rounded-md border border-input bg-background px-3 text-sm" />
                    <Button type="button" size="sm" variant="secondary" disabled={busy || !(draft[key] || "").trim()}
                      onClick={() => { void modify(provider.id, field.id, false); }}>{t("Save")}</Button>
                    <Button type="button" size="sm" variant="outline" disabled={busy || !configured}
                      onClick={() => { void modify(provider.id, field.id, true); }}>{t("Delete")}</Button>
                    {fieldIndex === provider.fields.length - 1 && <Button type="button" size="sm" variant="outline"
                      disabled={busy || testing !== null || !provider.fields.every(item => status?.[provider.id]?.[item.id] === true)}
                      onClick={() => { void test(provider.id); }}>
                      {testing === provider.id ? t("Testing…") : t("Test Connection")}
                    </Button>}
                    <p className="text-xs text-muted-foreground sm:col-span-5">
                      {status === null ? t("Status unavailable") : configured ? t("Configured") : t("Not configured")}
                    </p>
                  </div>
                );
              })}
              <div className="flex flex-wrap items-center gap-3">
                {testResults[provider.id] && <p role="status" aria-live="polite" className="text-xs text-muted-foreground">
                  {t(testResults[provider.id]!.status)} · {t(testResults[provider.id]!.message)}
                  {testResults[provider.id]!.http_status != null ? ` · HTTP ${testResults[provider.id]!.http_status}` : ""}
                  {` · ${testResults[provider.id]!.elapsed_ms} ms`}
                </p>}
              </div>
            </div>
          ))}
          <Button size="sm" type="button" variant="outline" disabled={busy} onClick={() => { void reload().then(() => notify(t("Credential status refreshed."), "success")).catch(() => notify(t("Credential status could not be refreshed. See details below."), "error")); }}>{t("Refresh")}</Button>
        </>}
        {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      </CardContent>
    </Card>
  );
}
