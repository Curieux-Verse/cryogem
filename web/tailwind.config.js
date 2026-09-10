/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Spec 16.2. Deliberately not the two-tone dark-with-acid-green look
        // every trading dashboard defaults to. `fail` is clay, not red: a
        // disqualification is information, not an error.
        ground: "#12141A",
        surface: "#1A1D26",
        line: "#2A2F3C",
        ink: "#E4E7EE",
        muted: "#7B8394",
        pass: "#5FB49C",
        fail: "#C2664D",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        mono: ["IBM Plex Mono", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      fontVariantNumeric: { tabular: "tabular-nums" },
    },
  },
  plugins: [],
};
