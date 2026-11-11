import type { Config } from "tailwindcss";
import plugin from "tailwindcss/plugin";
import {
  breakpoints,
  fonts,
  layout,
  motion,
  radii,
  spacing,
  themes,
  type ColorName,
  type ThemeTokens,
} from "./src/design/tokens";

// Do not put design values in this file. Edit src/design/tokens.ts instead.

const colorNames = Object.keys(themes.light.colors) as ColorName[];
const shadowNames = Object.keys(themes.light.shadows) as (keyof ThemeTokens["shadows"])[];

/** CSS variables for one theme: --color-<name> and --shadow-<name>. */
function themeVariables(theme: ThemeTokens, scheme: "light" | "dark") {
  const vars: Record<string, string> = { "color-scheme": scheme };
  for (const name of colorNames) vars[`--color-${name}`] = theme.colors[name];
  for (const name of shadowNames) vars[`--shadow-${name}`] = theme.shadows[name];
  return vars;
}

/** Nested Tailwind color keys, e.g. `bg-primary` gives `bg-bg-primary`. */
function colorClasses() {
  const result: Record<string, string | Record<string, string>> = {};
  for (const name of colorNames) {
    const value = `var(--color-${name})`;
    const [group, ...rest] = name.split("-");
    if (rest.length === 0) {
      const existing = result[group];
      result[group] = typeof existing === "object" ? { ...existing, DEFAULT: value } : value;
    } else {
      const existing = result[group];
      const nested = typeof existing === "object" ? existing : existing ? { DEFAULT: existing } : {};
      result[group] = { ...nested, [rest.join("-")]: value };
    }
  }
  return result;
}

const px = (value: number) => `${value}px`;

export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      screens: {
        tablet: px(breakpoints.tablet),
        desktop: px(breakpoints.desktop),
        wide: px(breakpoints.wide),
      },
      colors: colorClasses(),
      fontFamily: fonts,
      spacing,
      borderRadius: radii,
      boxShadow: Object.fromEntries(shadowNames.map((name) => [name, `var(--shadow-${name})`])),
      transitionDuration: motion.duration,
      transitionTimingFunction: motion.easing,
      maxWidth: { content: layout.maxContentWidth },
    },
  },
  plugins: [
    plugin(({ addBase }) => {
      addBase({
        // Light is the base. The OS dark setting applies unless the person
        // chose light. A manual choice sets data-theme on <html> (src/theme.ts).
        ":root": themeVariables(themes.light, "light"),
        "@media (prefers-color-scheme: dark)": {
          ':root:not([data-theme="light"])': themeVariables(themes.dark, "dark"),
        },
        ':root[data-theme="dark"]': themeVariables(themes.dark, "dark"),
        body: {
          fontFamily: fonts.body.join(", "),
          backgroundColor: "var(--color-bg-primary)",
          color: "var(--color-text-primary)",
        },
        ":focus-visible": {
          outline: "2px solid var(--color-focus)",
          outlineOffset: "2px",
        },
        "@media (prefers-reduced-motion: reduce)": {
          "*, ::before, ::after": {
            animationDuration: "0.01ms !important",
            animationIterationCount: "1 !important",
            transitionDuration: "0.01ms !important",
            scrollBehavior: "auto !important",
          },
        },
      });
    }),
  ],
} satisfies Config;
