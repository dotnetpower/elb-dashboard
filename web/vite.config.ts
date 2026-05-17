import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const hmrClientPort = Number(process.env.VITE_HMR_CLIENT_PORT || "");

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
    // vite v5 blocks requests whose Host header is not in this list. The
    // compose api sidecar reverse-proxies with `Host: frontend`; allow it
    // alongside the usual local hostnames. This is dev-only — the
    // production frontend is nginx, not vite.
    allowedHosts: ["localhost", "127.0.0.1", "frontend"],
    hmr: Number.isFinite(hmrClientPort)
      ? {
          host: process.env.VITE_HMR_HOST || "127.0.0.1",
          clientPort: hmrClientPort,
        }
      : undefined,
    proxy: {
      "/api/": {
        target: process.env.VITE_API_BASE_URL ?? "http://localhost:8085",
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
