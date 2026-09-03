import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..", "..");

// The FastAPI chat server serves index.html at "/" and mounts the build output
// dir at "/assets". We emit flat hashed files (assetsDir: "") into static/chat
// so every asset URL resolves under /assets/<hash> — same pattern as dashboard.
export default defineConfig({
  root: here,
  base: "/assets/",
  plugins: [react()],
  build: {
    outDir: resolve(repoRoot, "static", "chat"),
    emptyOutDir: true,
    assetsDir: "",
    sourcemap: false,
    rollupOptions: {
      output: {
        entryFileNames: "[name]-[hash].js",
        chunkFileNames: "[name]-[hash].js",
        assetFileNames: "[name]-[hash][extname]",
      },
    },
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:6322",
      "/ws": { target: "ws://127.0.0.1:6322", ws: true },
    },
  },
});
