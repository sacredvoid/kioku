import { defineConfig } from "vite";

// Tauri expects a fixed dev port; pages live in src/, build lands in dist/.
export default defineConfig({
  clearScreen: false,
  root: "src",
  build: { outDir: "../dist", emptyOutDir: true },
  server: { port: 1420, strictPort: true, watch: { ignored: ["**/src-tauri/**"] } },
});
