import { useState } from "react";
import { getThemeChoice, nextThemeChoice, setThemeChoice, type ThemeChoice } from "../theme";

const LABELS: Record<ThemeChoice, string> = {
  system: "Theme: system",
  light: "Theme: light",
  dark: "Theme: dark",
};

/** Cycles the theme: system → light → dark. Shows the current choice. */
export default function ThemeToggle() {
  const [choice, setChoice] = useState<ThemeChoice>(getThemeChoice);

  return (
    <button
      type="button"
      onClick={() => {
        const next = nextThemeChoice(choice);
        setThemeChoice(next);
        setChoice(next);
      }}
      aria-label={`${LABELS[choice]}. Change theme.`}
      className="text-sm text-text-muted border border-border-subtle px-3 py-1.5 rounded-md cursor-pointer transition-colors duration-fast hover:border-border-medium hover:text-text-secondary"
    >
      {LABELS[choice]}
    </button>
  );
}
