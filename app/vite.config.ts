import { defineConfig } from "vite";
import pkg from "./package.json" with { type: "json" };

// Tauri expects a fixed dev port; pages live in src/, build lands in dist/.
export default defineConfig({
  clearScreen: false,
  define: { __APP_VERSION__: JSON.stringify(pkg.version) }, // shown on the Settings page
  root: "src",
  build: { outDir: "../dist", emptyOutDir: true },
  server: { port: 1420, strictPort: true, watch: { ignored: ["**/src-tauri/**"] } },
});
