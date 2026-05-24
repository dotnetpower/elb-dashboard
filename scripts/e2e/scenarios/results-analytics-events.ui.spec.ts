import { test, expect } from "../fixtures/uiTest";

test("BLAST results tabs, filters, selection, taxonomy, and files events work", async ({ uiPage }) => {
  await uiPage.goto("/blast/jobs/job-e2e?tab=descriptions");
  await expect(uiPage.getByRole("heading", { name: "E2E fixture job" })).toBeVisible();

  await uiPage.getByPlaceholder("Accession").fill("NR_123456");
  await uiPage.getByRole("button", { name: "Apply" }).click();
  await expect(uiPage.getByText(/Active:/)).toBeVisible();

  await uiPage.getByRole("checkbox", { name: /Select hit/i }).check();
  await expect(uiPage.getByText(/1 hit selected/i)).toBeVisible();

  await uiPage.getByRole("button", { name: "Escherichia coli" }).click();
  await expect(uiPage.getByRole("dialog").filter({ hasText: "Escherichia coli" })).toBeVisible();
  await uiPage.keyboard.press("Escape");

  await uiPage.getByRole("link", { name: /Taxonomy/i }).click();
  await expect(uiPage.getByRole("tab", { name: "Organism" })).toBeVisible();
  await uiPage.getByRole("tab", { name: "Lineage" }).click();
  await expect(uiPage.getByText(/lineage/i).first()).toBeVisible();

  await uiPage.getByRole("link", { name: /Files/i }).click();
  await expect(uiPage.getByRole("heading", { name: "Results" })).toBeVisible();
  await uiPage.getByRole("button", { name: /Refresh/i }).click();
});