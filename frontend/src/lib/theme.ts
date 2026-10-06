export type ThemePreference = "light" | "dark" | "system";

const STORAGE_KEY = "theme-preference";

export function getStoredThemePreference(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === "light" || stored === "dark" || stored === "system") {
      return stored;
    }
  } catch {
    // localStorage unavailable (private mode, etc.) - fall back to system.
  }
  return "system";
}

export function applyThemePreference(preference: ThemePreference): void {
  const root = document.documentElement;
  if (preference === "system") {
    root.removeAttribute("data-theme");
  } else {
    root.setAttribute("data-theme", preference);
  }
  try {
    window.localStorage.setItem(STORAGE_KEY, preference);
  } catch {
    // ignore write failures
  }
}

export function initTheme(): void {
  applyThemePreference(getStoredThemePreference());
}
