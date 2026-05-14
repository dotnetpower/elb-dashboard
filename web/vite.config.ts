import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  server: {
    port: 8090,
    strictPort: true,
    proxy: {
      "/api/": {
        target: process.env.VITE_API_BASE_URL ?? "http://localhost:8080",
        changeOrigin: true,
      },
    },
    watch: {
      ignored: ["**/node_modules/**", "**/.venv/**", "**/.git/**"],
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
