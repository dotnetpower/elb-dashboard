import { expect, test } from "../fixtures/uiTest";

import { assertNoErrorBoundary, installClientIssueCollector } from "./helpers/assertions";

const coreRoutes = [
  { path: "/", marker: "Control Plane" },
  { path: "/blast/submit", marker: "ElasticBLAST New Search" },
  { path: "/blast/jobs", marker: "BLAST" },
  { path: "/docs", marker: "API" },
];

test("core dashboard routes render without client errors or API 5xx", async ({ uiPage: page }) => {
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