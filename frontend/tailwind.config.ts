import type { Config } from "tailwindcss";

// Токены графитовой темы (задание на редизайн, §2). Имена —
// как в задании: gp-panel, gp-text-2, gp-violation … Цвета типов объектов на
// карте (посадки, слои) тут не живут — они часть предметной области, а не
// темы интерфейса: lib/mapStyle.ts и app/globals.css.
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Onest", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["'JetBrains Mono'", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      colors: {
        greenery: {
          50: "#f0f9f0",
          100: "#dcf0dc",
          300: "#8fcf8f",
          500: "#3f9e46",
          600: "#2f7d36",
          700: "#26622b",
        },
        gp: {
          // интерфейс
          panel: "#222624",
          map: "#2A302C",
          field: "#1E2220",
          float: "#1C201E",
          hover: "#2A2F2C",
          "hover-strong": "#2E3430",
          line: "#303632",
          border: "#3A403C",
          text: "#ECEEEA",
          "text-2": "#A9B1AB",
          "text-3": "#868F89",
          "text-4": "#5C645F",
          accent: "#B5E07A",
          "accent-hover": "#C4E893",
          "on-accent": "#1A1D1B",
          "accent-soft": "#2F3A28",
          "accent-soft-border": "#5B7A3A",
          "accent-text": "#CBEB9E",
          // семантика
          violation: "#FF9351",
          "violation-text": "#FFB585",
          "violation-muted": "#C7A48D",
          "violation-soft": "#4A2E1E",
          "violation-row": "#2C2622",
          "violation-danger": "#FFA474",
          selection: "#8AB4FF",
          "selection-text": "#B8D0FF",
          "selection-soft": "#26324A",
          "selection-border": "#3F5A8A",
          warn: "#E8C48A",
          "warn-bar": "#D89B1C",
          ok: "#9FDB8E",
          "ok-strong": "#7BD389",
        },
      },
    },
  },
  plugins: [],
};

export default config;
