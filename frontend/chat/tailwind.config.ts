import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import type { Config } from "tailwindcss";

// 与 dashboard 相同的 token 体系（content 只扫 chat 自身，不扫 plugins）。
const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..", "..");

export default {
  content: [
    resolve(here, "index.html"),
    resolve(here, "src/**/*.{ts,tsx}"),
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      colors: {
        bg: "rgb(var(--color-bg-rgb) / <alpha-value>)",
        surface: "rgb(var(--color-surface-rgb) / <alpha-value>)",
        "surface-2": "rgb(var(--color-surface-2-rgb) / <alpha-value>)",
        "surface-3": "rgb(var(--color-surface-3-rgb) / <alpha-value>)",
        border: "var(--color-border)",
        "border-strong": "var(--color-border-strong)",
        fg: "rgb(var(--color-fg-rgb) / <alpha-value>)",
        muted: "rgb(var(--color-muted-rgb) / <alpha-value>)",
        subtle: "rgb(var(--color-subtle-rgb) / <alpha-value>)",
        accent: "rgb(var(--color-accent-rgb) / <alpha-value>)",
        "accent-ink": "rgb(var(--color-accent-ink-rgb) / <alpha-value>)",
        danger: "rgb(var(--color-danger-rgb) / <alpha-value>)",
        warning: "rgb(var(--color-warning-rgb) / <alpha-value>)",
        success: "rgb(var(--color-success-rgb) / <alpha-value>)",
      },
      borderRadius: {
        xs: "1px",
        sm: "2px",
        DEFAULT: "4px",
        md: "6px",
        lg: "10px",
        xl: "12px",
        "2xl": "14px",
      },
    },
  },
  plugins: [],
} satisfies Config;
