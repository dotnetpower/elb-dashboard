import { test, expect } from "../fixtures/uiTest";

test("Terminal side panels expose cockpit/manual events without a live websocket", async ({ uiPage }) => {
  await uiPage.goto("/terminal");
  await expect(uiPage.getByText("ElasticBLAST Terminal")).toBeVisible();
  await expect(uiPage.getByLabel("Terminal cockpit")).toBeVisible();

  await uiPage.getByLabel("Command to preview").fill("blastn -query q.fa -db core_nt -out results.xml");
  await uiPage.getByLabel("Re-check az login status").click();
  await uiPage.getByRole("button", { name: /Manual/i }).click();
  await expect(uiPage.getByLabel("Terminal user manual")).toBeVisible();
  await uiPage.getByRole("button", { name: /Cockpit/i }).click();
  await expect(uiPage.getByLabel("Terminal cockpit")).toBeVisible();
});

test("API Reference sidebar, try-it, and token controls are event-safe", async ({ uiPage }) => {
  await uiPage.goto("/docs");
  await expect(uiPage.getByRole("heading", { name: "ElasticBLAST API Reference" })).toBeVisible();

  await uiPage.getByPlaceholder(/Search path or summary/i).fill("jobs");
  await uiPage.getByRole("button", { name: "GET" }).click();
  await uiPage.getByRole("link", { name: /\/v1\/jobs/ }).click();
  await uiPage.getByRole("button", { name: /Send Request/i }).click();
  await expect(uiPage.getByText(/job-e2e/)).toBeVisible();

  await uiPage.getByLabel("Refresh token status").click();
  await uiPage.getByLabel("Reveal token").click();
  await expect(uiPage.getByText("e2e-token")).toBeVisible();
  await uiPage.getByLabel("Regenerate API token").click();
  await expect(uiPage.getByText("e2e-token-regenerated")).toBeVisible();
});

test("Custom DB builder covers config, FASTA input, build, and copy path events", async ({ uiPage, uiMocks }) => {
  await uiPage.goto("/blast/databases/build");
  await expect(uiPage.getByText("ElasticBLAST Custom DB")).toBeVisible();

  await uiPage.getByLabel("Database name *").fill("e2e_custom_db");
  await uiPage.locator(".blast-section", { hasText: "Database Configuration" }).getByRole("button", { name: /Protein/i }).click();
  await uiPage.getByLabel("Title (optional)").fill("E2E custom protein DB");
  await uiPage.getByPlaceholder(/sequence_id Description/i).fill(">p1\nMEEPQSDPSVEPPLSQETFSDLWKLLPEN\n");
  await expect(uiPage.getByText("1 sequence", { exact: true })).toBeVisible();

  await uiPage.getByRole("button", { name: /^Build database$/i }).click();
  await expect.poll(() => uiMocks.customDbBuilds.length).toBe(1);
  await expect(uiPage.getByText("Database created")).toBeVisible();
  await uiPage.getByLabel("Copy database path").click();
  await expect(uiPage.getByText("Copied")).toBeVisible();
});