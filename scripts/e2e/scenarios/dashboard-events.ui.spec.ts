import { test, expect } from "../fixtures/uiTest";

test("Dashboard settings, ACR build, and database manager events stay client-safe", async ({
  uiPage,
  uiMocks,
}) => {
  await uiPage.goto("/");
  await expect(uiPage.getByText("ElasticBLAST Dashboard")).toBeVisible();

  await uiPage.getByLabel("Open workspace settings").click();
  await expect(uiPage.getByRole("dialog", { name: "Settings" })).toBeVisible();
  await uiPage.keyboard.press("Escape");
  await expect(uiPage.getByRole("dialog", { name: "Settings" })).toHaveCount(0);

  await uiPage.getByRole("button", { name: /^Build$/ }).first().click();
  await expect(uiPage.getByText(/Build \d+ images\?/)).toBeVisible();
  await uiPage.getByRole("button", { name: /Start Build/i }).click();
  await expect.poll(() => uiMocks.buildRequests.length).toBe(1);

  await uiPage.getByTitle("Open database manager").click();
  await expect(uiPage.getByRole("dialog", { name: "BLAST Databases" })).toBeVisible();
  await uiPage.getByTitle("Refresh database status").click();
  await uiPage.keyboard.press("Escape");
  await expect(uiPage.getByRole("dialog", { name: "BLAST Databases" })).toHaveCount(0);
});