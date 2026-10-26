import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  credentialRemove, credentialSave, credentialStatuses, credentialsNativeAvailable,
  type CredentialField, type CredentialStatus, type ProviderId,
} from "./credentials-api";
import { useLocale } from "./localization";

const providers: { id: ProviderId; title: string; fields: { id: CredentialField; label: string }[] }[] = [
  { id: "alpha_vantage", title: "Alpha Vantage", fields: [{ id: "api_key", label: "API key" }] },
  { id: "alpaca_sip", title: "Alpaca SIP", fields: [{ id: "key_id", label: "Key ID" }, { id: "secret_key", label: "Secret key" }] },
  { id: "tiingo", title: "Tiingo", fields: [{ id: "api_key", label: "API key" }] },
  { id: "equibles", title: "Equibles", fields: [{ id: "api_key", label: "API key" }] },
];

export function ProviderCredentialsCard() {
  const { t } = useLocale();
  const [status, setStatus] = useState<CredentialStatus | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const native = credentialsNativeAvailable();

  const reload = useCallback(async () => {
    if (!native) return;
    try {
      setStatus(await credentialStatuses());
      setError("");
    } catch (reason) {
      setError(String(reason));
    }
  }, [native]);

  useEffect(() => { void reload(); }, [reload]);

  const modify = async (provider: ProviderId, field: CredentialField, remove: boolean) => {
    const key = provider + ":" + field;
    const secret = draft[key] || "";
    if (!remove && !secret.trim()) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      if (remove) await credentialRemove(provider, field);
      else await credentialSave(provider, field, secret);
      setMessage(t(remove ? "Credential deleted." : "Credential saved securely."));
      await reload();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setDraft(previous => ({ ...previous, [key]: "" }));
      setBusy(false);
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
              {provider.fields.map(field => {
                const key = provider.id + ":" + field.id;
                const configured = !!status?.[provider.id]?.[field.id];
                return (
                  <div key={key} className="grid gap-2 sm:grid-cols-[100px_minmax(120px,1fr)_auto_auto] sm:items-center">
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
                    <p className="text-xs text-muted-foreground sm:col-span-4">
                      {configured ? t("Configured") : t("Not configured")}
                    </p>
                  </div>
                );
              })}
            </div>
          ))}
          <Button size="sm" type="button" variant="outline" disabled={busy} onClick={() => { void reload(); }}>{t("Refresh")}</Button>
        </>}
        {message && <p role="status" className="text-xs text-positive">{message}</p>}
        {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      </CardContent>
    </Card>
  );
}
