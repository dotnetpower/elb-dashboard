import { test, expect } from "../fixtures/uiTest";

test("Lab Tools tabs exercise representative form and action events", async ({ uiPage, uiMocks }) => {
  await uiPage.goto("/tools");
  await expect(uiPage.getByText("ElasticBLAST Lab Tools")).toBeVisible();

  await uiPage.locator(".form-row", { hasText: "Nodes" }).getByRole("spinbutton").fill("4");
  await uiPage.locator(".form-row", { hasText: "Estimated hours" }).getByRole("spinbutton").fill("3");
  await uiPage.getByRole("button", { name: /Calculate estimate/i }).click();
  await expect(uiPage.getByText("$14.02")).toBeVisible();

  await uiPage.getByRole("button", { name: /Preprocessor/i }).click();
  await uiPage.locator(".form-row", { hasText: /Input sequences/i }).locator("textarea").fill(">read1\nACGTACGTACGT\n");
  await uiPage.locator(".form-row", { hasText: "Min length" }).getByRole("spinbutton").fill("4");
  await uiPage.getByRole("button", { name: /^Process$/i }).click();
  await expect(uiPage.getByText("Output FASTA")).toBeVisible();

  await uiPage.getByRole("button", { name: /Taxonomy/i }).click();
  await uiPage.locator(".form-row", { hasText: /Accessions/i }).locator("textarea").fill("NR_123456");
  await uiPage.getByRole("button", { name: /Look up/i }).click();
  await expect(uiPage.getByText("Escherichia coli")).toBeVisible();

  await uiPage.getByRole("button", { name: /Schedules/i }).click();
  await expect(uiPage.getByText("Daily 16S smoke")).toBeVisible();
  await uiPage.getByTitle("Run now").click();
  await expect.poll(() => uiMocks.scheduleRuns.length).toBe(1);
  await uiPage.getByTitle("Delete").click();
  await expect.poll(() => uiMocks.scheduleDeletes.length).toBe(1);
});