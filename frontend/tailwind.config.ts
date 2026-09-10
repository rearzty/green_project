import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        greenery: {
          50: "#f0f9f0",
          100: "#dcf0dc",
          300: "#8fcf8f",
          500: "#3f9e46",
          600: "#2f7d36",
          700: "#26622b",
        },
      },
    },
  },
  plugins: [],
};

export default config;
