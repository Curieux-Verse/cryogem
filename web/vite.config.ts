import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `base` MUST match the repository name for a project Pages site, or every
// asset 404s with no useful error (spec 16.4.3). It is overridable so a fork
// under a different repo name does not have to edit this file:
//     VITE_BASE=/my-fork/ npm run build
const base = process.env.VITE_BASE ?? "/cryogem/";

export default defineConfig({
  base,
  plugins: [react()],
  build: {
    // The bundle budget is 500 KB gzipped (spec 16.4). Warn well before it.
    chunkSizeWarningLimit: 400,
    sourcemap: false,
  },
});
