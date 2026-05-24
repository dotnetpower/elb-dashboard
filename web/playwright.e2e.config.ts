import { defineConfig, devices } from "@playwright/test";

const browserMode = process.env.E2E_BROWSER_MODE ?? "headless";
const headless = browserMode !== "headed";
const baseURL = process.env.E2E_BASE_URL ?? "http://localhost:8090";
const configuredWorkers = process.env.E2E_WORKERS
  ? Number.parseInt(process.env.E2E_WORKERS, 10)
  : undefined;
const safeWorkers = configuredWorkers && configuredWorkers > 0 ? configuredWorkers : undefined;

const desktopChrome = {
  ...devices["Desktop Chrome"],
  baseURL,
  viewport: { width: 1600, height: 1000 },
  headless,
  actionTimeout: 10_000,
  navigationTimeout: 30_000,
  screenshot: "only-on-failure" as const,
  trace: "retain-on-failure" as const,
  video: "retain-on-failure" as const,
};

export default defineConfig({
  testDir: "../scripts/e2e/scenarios",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  workers: safeWorkers,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI
    ? [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]]
    : "list",
  use: desktopChrome,
  projects: [
    {
      name: "ui-mock",
      testMatch: [
        /.*\.ui\.spec\.ts/,
        /dashboard-smoke\.spec\.ts/,
        /new-search-options-matrix\.spec\.ts/,
      ],
      fullyParallel: true,
      use: desktopChrome,
    },
    {
      name: "api-smoke",
      testMatch: [/.*\.api\.spec\.ts/, /api-blast-submit-smoke\.spec\.ts/],
      fullyParallel: true,
      use: desktopChrome,
    },
    {
      name: "mutation-mock",
      testMatch: [/.*\.mutation\.spec\.ts/],
      fullyParallel: true,
      use: desktopChrome,
    },
    {
      name: "azure-lifecycle",
      testMatch: [/.*\.azure\.spec\.ts/, /azure-core-nt-lifecycle\.spec\.ts/],
      fullyParallel: false,
      workers: 1,
      use: desktopChrome,
    },
  ],
});