/**
 * Design tokens: the one source for this app's visual language.
 *
 * Change a value here to change it everywhere. `tailwind.config.ts` reads this
 * file. It turns each theme color into a CSS variable (`--color-<name>`) and a
 * Tailwind class. Components use the classes (`bg-bg-primary`,
 * `text-text-muted`, `p-md`, `tablet:px-lg`). They do not use raw hex values.
 *
 * See DESIGN.md for the rules.
 */

/** Names of the colors that change between light mode and dark mode. */
export type ColorName =
  | "bg-primary" // page background
  | "bg-secondary" // cards, panels, inputs
  | "bg-tertiary" // hover and selected rows
  | "border-subtle"
  | "border-medium"
  | "text-primary" // body text
  | "text-secondary"
  | "text-muted" // hints; keep at least 4.5:1 contrast with bg-primary
  | "accent"
  | "accent-hover"
  | "accent-contrast" // text on an accent background
  | "focus" // keyboard focus outline
  | "success"
  | "warning"
  | "error"
  | "info";

export type ShadowName = "sm" | "md" | "lg" | "xl";

export interface ThemeTokens {
  colors: Record<ColorName, string>;
  shadows: Record<ShadowName, string>;
}

export const themes: { light: ThemeTokens; dark: ThemeTokens } = {
  light: {
    colors: {
      "bg-primary": "#fafafa",
      "bg-secondary": "#ffffff",
      "bg-tertiary": "#f4f4f5",
      "border-subtle": "#e4e4e7",
      "border-medium": "#d4d4d8",
      "text-primary": "#18181b",
      "text-secondary": "#52525b",
      "text-muted": "#6b6b76",
      accent: "#4f46e5",
      "accent-hover": "#4338ca",
      "accent-contrast": "#ffffff",
      focus: "#6366f1",
      success: "#15803d",
      warning: "#b45309",
      error: "#dc2626",
      info: "#2563eb",
    },
    shadows: {
      sm: "0 1px 2px rgb(0 0 0 / 0.06)",
      md: "0 4px 12px rgb(0 0 0 / 0.08)",
      lg: "0 8px 24px rgb(0 0 0 / 0.10)",
      xl: "0 16px 48px rgb(0 0 0 / 0.14)",
    },
  },
  dark: {
    colors: {
      "bg-primary": "#0a0a0f",
      "bg-secondary": "#12121a",
      "bg-tertiary": "#1a1a25",
      "border-subtle": "#1e1e2e",
      "border-medium": "#2a2a3e",
      "text-primary": "#f0f0f5",
      "text-secondary": "#b4b4c6",
      "text-muted": "#8a8aa0",
      // White text on these two keeps at least 4.5:1 contrast.
      accent: "#5b5bf0",
      "accent-hover": "#4f46e5",
      "accent-contrast": "#ffffff",
      focus: "#a5b4fc",
      success: "#22c55e",
      warning: "#f59e0b",
      error: "#f87171",
      info: "#60a5fa",
    },
    shadows: {
      sm: "0 1px 2px rgb(0 0 0 / 0.3)",
      md: "0 4px 12px rgb(0 0 0 / 0.4)",
      lg: "0 8px 24px rgb(0 0 0 / 0.5)",
      xl: "0 16px 48px rgb(0 0 0 / 0.6)",
    },
  },
};

/** Font stacks. The first name in each stack must be loaded by `fontStylesheet`. */
export const fonts = {
  heading: ["Urbanist", "system-ui", "sans-serif"],
  body: ["Figtree", "system-ui", "sans-serif"],
  mono: ["JetBrains Mono", "ui-monospace", "monospace"],
};

/**
 * Stylesheet that loads the web fonts above. `vite.config.ts` adds it to
 * index.html. When you change a font, change this URL too. Use a full URL, or a
 * local path such as "/fonts/fonts.css" for self-hosted fonts. Set it to "" to
 * use system fonts only and make no font request.
 */
export const fontStylesheet =
  "https://fonts.googleapis.com/css2?family=Urbanist:wght@400;500;600;700&family=Figtree:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap";

/** Named spacing steps. Tailwind's numeric steps (`p-4`) still work. */
export const spacing = {
  "2xs": "2px",
  xs: "4px",
  sm: "8px",
  md: "16px",
  lg: "24px",
  xl: "32px",
  "2xl": "48px",
  "3xl": "64px",
};

export const radii = {
  sm: "6px",
  md: "12px",
  lg: "16px",
  xl: "20px",
  full: "9999px",
};

export const motion = {
  duration: { fast: "150ms", med: "250ms", slow: "400ms" },
  easing: {
    smooth: "cubic-bezier(0.4, 0, 0.2, 1)",
    enter: "cubic-bezier(0, 0, 0.2, 1)",
    exit: "cubic-bezier(0.4, 0, 1, 1)",
  },
};

/**
 * Mobile-first breakpoints: minimum widths in pixels.
 *
 * Style for a phone first, with no prefix. Add `tablet:`, `desktop:`, or
 * `wide:` for larger screens. The values match Tailwind's `sm`, `lg`, and
 * `xl`, so older classes keep working.
 */
export const breakpoints = {
  phone: 0,
  tablet: 640,
  desktop: 1024,
  wide: 1280,
};

/** Widest content column, and the side gutter for each screen size. */
export const layout = {
  maxContentWidth: "1200px",
  gutter: { phone: "16px", tablet: "24px", desktop: "32px" },
};
