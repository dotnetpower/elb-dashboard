import { defineConfig, devices } from "@playwright/test";

const browserMode = process.env.E2E_BROWSER_MODE ?? "headless";
const headless = browserMode !== "headed";
const baseURL = process.env.E2E_BASE_URL ?? "http://localhost:8090";

export default defineConfig({
  testDir: "../scripts/e2e/scenarios",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI
    ? [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]]
    : "list",
  use: {
    ...devices["Desktop Chrome"],
    baseURL,
    headless,
    actionTimeout: 10_000,
    navigationTimeout: 30_000,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "retain-on-failure",
  },
});