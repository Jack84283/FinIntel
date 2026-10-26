/** Lightweight, device-local UI preferences. No network or account storage. */
export type ColorTheme = "dark" | "light";
export type AppLanguage = "en" | "zh-CN";

export interface WorkspacePreferences {
  theme: ColorTheme;
  reduceMotion: boolean;
  language: AppLanguage;
}

const STORAGE_KEY = "finintel.workspace-preferences.v1";

export const DEFAULT_PREFERENCES: WorkspacePreferences = {
  theme: "dark",
  reduceMotion: false,
  language: "en",
};

export function readWorkspacePreferences(): WorkspacePreferences {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULT_PREFERENCES };
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return { ...DEFAULT_PREFERENCES };
    const settings = parsed as Partial<WorkspacePreferences>;
    return {
      theme: settings.theme === "light" ? "light" : "dark",
      reduceMotion: settings.reduceMotion === true,
      language: settings.language === "zh-CN" ? "zh-CN" : "en",
    };
  } catch {
    return { ...DEFAULT_PREFERENCES };
  }
}

export function applyWorkspacePreferences(settings: WorkspacePreferences): void {
  document.documentElement.dataset.theme = settings.theme;
  document.documentElement.lang = settings.language;
  document.documentElement.dataset.reduceMotion = String(settings.reduceMotion);
}

export function saveWorkspacePreferences(settings: WorkspacePreferences): void {
  applyWorkspacePreferences(settings);
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  } catch {
    // The current session stays usable even when local storage is unavailable.
  }
}
