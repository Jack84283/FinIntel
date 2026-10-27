import { useCallback, useEffect, useRef, useState } from "react";
import { DatabaseBackup, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  backupsAvailable, createDatabaseBackup, listDatabaseBackups, previewDatabaseBackup,
  restoreDatabaseBackup, type BackupEntry, type BackupPreview, type RestoreMode,
} from "./database-backup-api";
import { useLocale } from "./localization";
import { useNotify } from "./notifications";

export function DatabaseBackupCard() {
  const { t, language } = useLocale();
  const notify = useNotify();
  const [items, setItems] = useState<BackupEntry[]>([]);
  const [selected, setSelected] = useState("");
  const [preview, setPreview] = useState<BackupPreview | null>(null);
  const [inspecting, setInspecting] = useState(false);
  const inspectionSequence = useRef(0);
  const [confirming, setConfirming] = useState(false);
  const [replacePhrase, setReplacePhrase] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const native = backupsAvailable();

  const refresh = useCallback(async () => {
    if (!native) return;
    setItems(await listDatabaseBackups());
  }, [native]);

  useEffect(() => {
    let active = true;
    if (native) void listDatabaseBackups()
      .then(rows => { if (active) setItems(rows); })
      .catch(reason => { if (active) setError(String(reason)); });
    return () => { active = false; };
  }, [native]);

  const inspect = async (identifier: string) => {
    const requestId = ++inspectionSequence.current;
    setInspecting(true);
    setError("");
    try {
      const result = await previewDatabaseBackup(identifier);
      if (requestId === inspectionSequence.current) setPreview(result);
    } catch (reason) {
      if (requestId === inspectionSequence.current) {
        setPreview(null);
        setError(String(reason));
        notify(t("Backup inspection failed."), "error");
      }
    } finally {
      if (requestId === inspectionSequence.current) setInspecting(false);
    }
  };

  const selectBackup = (identifier: string) => {
    ++inspectionSequence.current; // Ignore the result of any previous selection.
    setSelected(identifier);
    setPreview(null);
    setInspecting(false);
    setConfirming(false);
    setReplacePhrase("");
    setError("");
    if (identifier) void inspect(identifier);
  };

  const openRestore = () => {
    if (!selected || busy) return;
    setConfirming(true);
    // Retry a failed inspection while keeping Cancel available.
    if (!preview && !inspecting) void inspect(selected);
  };

  const save = async () => {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await createDatabaseBackup();
      setMessage(t("Database backup completed."));
      notify(t("Database backup completed."), "success");
      try {
        await refresh();
      } catch (reason) {
        setError(String(reason));
        notify(t("Backup list could not be loaded."), "error");
      }
    } catch (reason) {
      setError(String(reason));
      notify(t("Database backup failed."), "error");
    } finally {
      setBusy(false);
    }
  };

  const restore = async (mode: RestoreMode) => {
    if (!preview || preview.id !== selected || inspecting || (mode === "replace" && replacePhrase !== "REPLACE")) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await restoreDatabaseBackup(preview.id, mode);
      await refresh();
      setConfirming(false);
      setReplacePhrase("");
      setMessage(t("Restore completed. Reopen Markets to refresh local data."));
      notify(t("Database restored successfully."), "success");
    } catch (reason) {
      setError(String(reason));
      notify(t("Database restore failed. See details below."), "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle><span className="flex items-center gap-2"><DatabaseBackup aria-hidden="true" className="size-4" />{t("Database backup & restore")}</span></CardTitle>
        <CardDescription>{t("Create backups manually. FinIntel creates a recovery backup before database migration or restore.")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {!native ? (
          <p className="text-xs text-muted-foreground">{t("Backup management requires the managed native desktop.")}</p>
        ) : (
          <>
            <div className="grid min-w-0 gap-2 sm:grid-cols-[120px_minmax(0,1fr)] sm:items-center">
              <label htmlFor="available-backups" className="text-xs font-medium">{t("Available backups")}</label>
              <select
                id="available-backups"
                value={selected}
                disabled={busy}
                onChange={event => selectBackup(event.target.value)}
                className="h-9 w-full min-w-0 rounded-md border border-input bg-background px-2 text-sm text-foreground"
              >
                <option value="">{t("Choose a backup")}</option>
                {items.map(item => (
                  <option key={item.id} value={item.id}>
                    {t(item.reason)} · {new Date(item.created_at).toLocaleString(language)} · {(item.size_bytes / 1048576).toFixed(1)} MB
                  </option>
                ))}
              </select>
              <div className="flex flex-wrap justify-end gap-2 sm:col-start-2">
                <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void refresh().catch(reason => { setError(String(reason)); notify(t("Backup list could not be loaded."), "error"); })}>
                  <RefreshCw aria-hidden="true" />{t("Refresh")}
                </Button>
                <Button type="button" size="sm" variant="secondary" disabled={busy} onClick={() => void save()}>
                  {t(busy ? "Working…" : "Backup now")}
                </Button>
                <Button type="button" size="sm" variant="secondary" disabled={busy || !selected || confirming} onClick={openRestore}>
                  {t("Restore…")}
                </Button>
              </div>
            </div>
            {items.length === 0 && <p className="text-xs text-muted-foreground">{t("No backups yet. Use Backup now to create one.")}</p>}
            {(preview || confirming) && (
              <div className="space-y-2 rounded-md border border-border p-3 text-xs">
                {preview && <>
                  <p className="font-medium">{t("Backup preview")} · SQLite v{preview.schema_version}</p>
                  <p className="break-words text-muted-foreground">{preview.id}</p>
                  <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                    {Object.entries(preview.counts).map(([key, value]) => (
                      <span key={key} className="rounded border border-border px-2 py-1">
                        {t(key.replaceAll("_", " "))}: <strong>{value.toLocaleString(language)}</strong>
                      </span>
                    ))}
                  </div>
                  <p className="text-muted-foreground">{t("Full database integrity checks run before restoration.")}</p>
                </>}
                {confirming && (
                  <div role="dialog" aria-label={t("Confirm database restore")} className="space-y-3 rounded-md border border-warning p-3">
                    <p className="font-semibold">{t("Confirm database restore")}</p>
                    <p>{t("Merge adds missing data and accepts newer price observations. Replace overwrites the current database. Both create a recovery backup first.")}</p>
                    {!preview && <p role="status" className="text-muted-foreground">{t(inspecting ? "Inspecting selected backup…" : "Backup inspection unavailable. Select another backup or retry Restore.")}</p>}
                    <label className="block space-y-1">
                      <span>{t("Type REPLACE to enable destructive replacement.")}</span>
                      <input type="text" value={replacePhrase} onChange={event => setReplacePhrase(event.target.value)}
                        className="h-9 w-full rounded-md border border-input bg-background px-2 text-sm" placeholder="REPLACE" autoComplete="off" />
                    </label>
                    <div className="flex flex-wrap justify-end gap-2">
                      <Button size="sm" variant="outline" type="button" disabled={busy} onClick={() => { setConfirming(false); setReplacePhrase(""); }}>{t("Cancel")}</Button>
                      <Button size="sm" variant="secondary" type="button" disabled={busy || inspecting || !preview} onClick={() => { void restore("merge"); }}>{t("Merge")}</Button>
                      <Button size="sm" variant="destructive" type="button" disabled={busy || inspecting || !preview || replacePhrase !== "REPLACE"} onClick={() => { void restore("replace"); }}>{t("Replace")}</Button>
                    </div>
                  </div>
                )}
              </div>
            )}
          </>
        )}
        {busy && <p role="status" className="text-xs text-muted-foreground">{t("Database operation running. Keep FinIntel open.")}</p>}
        {message && <p role="status" className="text-xs text-positive">{message}</p>}
        {error && <p role="alert" className="break-words text-xs text-destructive">{error}</p>}
      </CardContent>
    </Card>
  );
}
