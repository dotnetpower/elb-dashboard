import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { execSync } from "node:child_process";
import { readFileSync } from "node:fs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const hmrClientPort = Number(process.env.VITE_HMR_CLIENT_PORT || "");

// Build-time version stamp.
// Resolution order: explicit env (Docker build-arg) -> local git -> fallback.
// Docker builds run without .git in context, so deploy scripts pass
// APP_VERSION / APP_BUILD_NUMBER / GIT_COMMIT / BUILD_TIME as --build-arg.
function readPkgVersion(): string {
  try {
    const pkg = JSON.parse(readFileSync(path.resolve(__dirname, "package.json"), "utf-8"));
    return typeof pkg.version === "string" ? pkg.version : "0.0.0";
  } catch {
    return "0.0.0";
  }
}
function tryGit(args: string[]): string {
  try {
    return execSync(`git ${args.join(" ")}`, { cwd: __dirname, stdio: ["ignore", "pipe", "ignore"] })
      .toString()
      .trim();
  } catch {
    return "";
  }
}
function readBuildNumber(): string {
  const latestTag = tryGit(["tag", "--list", "'v[0-9]*.[0-9]*.[0-9]*'", "--sort=-v:refname", "--merged", "HEAD"])
    .split(/\r?\n/)
    .find(Boolean);
  const count = tryGit(latestTag ? ["rev-list", "--count", `${latestTag}..HEAD`] : ["rev-list", "--count", "HEAD"]);
  return /^\d+$/.test(count) ? count : "0";
}
const APP_VERSION = process.env.APP_VERSION?.trim() || readPkgVersion();
const APP_BUILD_NUMBER = process.env.APP_BUILD_NUMBER?.trim() || readBuildNumber();
const APP_COMMIT = process.env.GIT_COMMIT?.trim() || tryGit(["rev-parse", "--short", "HEAD"]) || "dev";
const APP_BUILD_TIME = process.env.BUILD_TIME?.trim() || new Date().toISOString();
const ENABLE_PRODUCTION_SOURCEMAPS = process.env.VITE_ENABLE_PRODUCTION_SOURCEMAPS === "true";

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  define: {
    __APP_VERSION__: JSON.stringify(APP_VERSION),
    __APP_BUILD_NUMBER__: JSON.stringify(APP_BUILD_NUMBER),
    __APP_COMMIT__: JSON.stringify(APP_COMMIT),
    __APP_BUILD_TIME__: JSON.stringify(APP_BUILD_TIME),
  },
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
  optimizeDeps: {
    include: ["@xterm/xterm", "@xterm/addon-fit"],
  },
  build: {
    outDir: "dist",
    sourcemap: ENABLE_PRODUCTION_SOURCEMAPS,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("node_modules/react") || id.includes("node_modules/react-dom") || id.includes("node_modules/react-router-dom")) {
            return "vendor-react";
          }
          if (id.includes("node_modules/@tanstack/react-query")) {
            return "vendor-query";
          }
          if (id.includes("node_modules/@xterm/")) {
            return "vendor-xterm";
          }
          if (id.includes("/src/pages/blastResults/") || id.includes("/src/pages/BlastJobs/")) {
            return "blast-results";
          }
          if (id.includes("/src/pages/blastSubmit/") || id.includes("/src/pages/BlastSubmit")) {
            return "blast-submit";
          }
          if (id.includes("/src/pages/terminal/") || id.includes("RemoteTerminal")) {
            return "terminal";
          }
          return undefined;
        },
      },
    },
  },
});
