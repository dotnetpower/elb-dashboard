import { test, expect } from "../fixtures/uiTest";

test("Live Wall filter, global pause, and tile pause events are covered", async ({ uiPage }) => {
  await uiPage.goto("/monitor/live-wall");
  await expect(uiPage.getByRole("heading", { name: "Live Wall" })).toBeVisible();

  await uiPage.getByLabel("Filter log lines across all tiles").fill("e2e|ERROR");
  await uiPage.getByLabel("Pause").first().click();
  await expect(uiPage.getByLabel("Resume").first()).toBeVisible();
  await uiPage.getByRole("button", { name: /Pause all/i }).click();
  await expect(uiPage.getByRole("button", { name: /Resume all/i })).toBeVisible();
  await uiPage.getByRole("button", { name: /Resume all/i }).click();
  await expect(uiPage.getByRole("button", { name: /Pause all/i })).toBeVisible();
});

test("Recent searches filter, search, refresh, group collapse, and navigation events are covered", async ({ uiPage }) => {
  await uiPage.goto("/blast/jobs");
  await expect(uiPage.getByText("Recent BLAST searches")).toBeVisible();

  await uiPage.getByRole("button", { name: /completed/i }).click();
  await uiPage.getByPlaceholder("Search jobs…").fill("fixture");
  const jobLink = uiPage.getByRole("link", { name: "E2E fixture job", exact: true });
  await expect(jobLink).toBeVisible();
  await uiPage.getByRole("button", { name: /^Today/ }).click();
  await expect(jobLink).toHaveCount(0);
  await uiPage.getByRole("button", { name: /^Today/ }).click();
  await uiPage.getByRole("button", { name: /Refresh/i }).click();
  await jobLink.click();
  await expect(uiPage.getByRole("heading", { name: "E2E fixture job" })).toBeVisible();
});