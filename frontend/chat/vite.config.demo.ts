// PulseCore demo 独立构建配置：不进聊天主包，产物出 static/chat-demo
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..", "..");

export default defineConfig({
    root: here,
    base: "/assets/",
    plugins: [react()],
    build: {
        outDir: resolve(repoRoot, "static", "chat-demo"),
        emptyOutDir: true,
        assetsDir: "",
        sourcemap: false,
        rollupOptions: {
            input: resolve(here, "demo", "index.html"),
            output: {
                entryFileNames: "[name]-[hash].js",
                chunkFileNames: "[name]-[hash].js",
                assetFileNames: "[name]-[hash][ext]",
            },
        },
    },
    server: {
        port: 5188,
        strictPort: true,
    },
});
