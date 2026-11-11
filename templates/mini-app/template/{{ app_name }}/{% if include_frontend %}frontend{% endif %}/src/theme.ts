/**
 * Light, dark, or system theme choice.
 *
 * "system" follows the operating system (`prefers-color-scheme`). "light" and
 * "dark" set `data-theme` on <html>, which overrides the system setting. The
 * choice is a display preference only, so it lives in localStorage.
 *
 * index.html applies the saved choice before the first paint. Keep its inline
 * script in step with STORAGE_KEY and the allowed values.
 */

export type ThemeChoice = "system" | "light" | "dark";

export const STORAGE_KEY = "theme";
export const THEME_CHOICES: readonly ThemeChoice[] = ["system", "light", "dark"];

function isThemeChoice(value: unknown): value is ThemeChoice {
  return typeof value === "string" && (THEME_CHOICES as readonly string[]).includes(value);
}

/** The saved choice, or "system" when none is saved or storage is blocked. */
export function getThemeChoice(): ThemeChoice {
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    return isThemeChoice(saved) ? saved : "system";
  } catch {
    return "system";
  }
}

/** Apply a choice to <html> and save it. Saving can fail; applying cannot. */
export function setThemeChoice(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === "system") {
    root.removeAttribute("data-theme");
  } else {
    root.setAttribute("data-theme", choice);
  }
  try {
    if (choice === "system") {
      window.localStorage.removeItem(STORAGE_KEY);
    } else {
      window.localStorage.setItem(STORAGE_KEY, choice);
    }
  } catch {
    // Private windows and blocked storage: the choice lasts for this page only.
  }
}

/** The next choice in the cycle system → light → dark → system. */
export function nextThemeChoice(choice: ThemeChoice): ThemeChoice {
  return THEME_CHOICES[(THEME_CHOICES.indexOf(choice) + 1) % THEME_CHOICES.length];
}
