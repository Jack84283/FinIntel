import { invoke, isTauri } from "@tauri-apps/api/core";

export type BackupEntry = {
  id: string;
  reason: "auto" | "manual" | "pre-restore";
  created_at: string;
  size_bytes: number;
};

export type BackupPreview = BackupEntry & {
  schema_version: number;
  counts: Record<string, number>;
};

export type RestoreMode = "merge" | "replace";

export const backupsAvailable = () => isTauri();
export async function listDatabaseBackups(): Promise<BackupEntry[]> {
  if (!isTauri()) throw new Error("Database restoration requires the native desktop.");
  const result = await invoke<{ items: BackupEntry[] }>("database_backup_list");
  return result.items;
}
export async function createDatabaseBackup(): Promise<BackupEntry> {
  if (!isTauri()) throw new Error("Database restoration requires the native desktop.");
  return invoke<BackupEntry>("database_backup_create");
}
export async function previewDatabaseBackup(backupId: string): Promise<BackupPreview> {
  if (!isTauri()) throw new Error("Database restoration requires the native desktop.");
  return invoke<BackupPreview>("database_backup_preview", { backupId });
}
export async function restoreDatabaseBackup(backupId: string, mode: RestoreMode): Promise<void> {
  if (!isTauri()) throw new Error("Database restoration requires the native desktop.");
  await invoke("database_backup_restore", { backupId, mode });
}
