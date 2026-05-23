import { expect, test } from "@playwright/test";

import { assertNoErrorBoundary, installClientIssueCollector } from "./helpers/assertions";
import { seedWorkspaceConfig } from "./helpers/workspace";

const coreRoutes = [
  { path: "/", marker: "Control Plane" },
  { path: "/blast/submit", marker: "ElasticBLAST New Search" },
  { path: "/blast/jobs", marker: "BLAST" },
  { path: "/docs", marker: "API" },
];

test.beforeEach(async ({ page }) => {
  await seedWorkspaceConfig(page);
});

test("core dashboard routes render without client errors or API 5xx", async ({ page }) => {
  const collector = installClientIssueCollector(page);

  for (const route of coreRoutes) {
    await page.goto(route.path);
    await expect(page.locator("body")).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Main navigation" })).toBeVisible();
    await expect(page.getByText(route.marker).first()).toBeVisible();
    await assertNoErrorBoundary(page);
  }

  await collector.assertClean();
});