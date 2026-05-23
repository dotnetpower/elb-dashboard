import { expect, test, type Page } from "@playwright/test";

import { installClientIssueCollector } from "./helpers/assertions";
import { installNewSearchApiMocks, type MockSubmitCapture } from "./helpers/apiMocks";
import { seedWorkspaceConfig } from "./helpers/workspace";

const nucleotideQuery =
  ">e2e_16s\nAGAGTTTGATCCTGGCTCAGATTGAACGCTGGCGGCAGGCCTAACACATGCAAGTCGAAC\n";
const shortNucleotideQuery = ">short\nAGAGTTTGATCCTGGCTCAG\n";
const proteinQuery = ">protein\nMEEPQSDPSVEPPLSQETFSDLWKLLPEN\n";

test.beforeEach(async ({ page }) => {
  await seedWorkspaceConfig(page);
});

test("New Search option matrix produces accepted submit payloads", async ({ page }) => {
  const collector = installClientIssueCollector(page);
  const capture = await installNewSearchApiMocks(page);

  await runCase(page, capture, "default blastn", async () => {
    await fillBaseForm(page, nucleotideQuery);
  }, (payload) => {
    expect(payload.program).toBe("blastn");
    expect(payload.db).toBe("blast-db/core_nt/core_nt");
    expect(payload.disable_sharding).toBe(false);
  });

  await runCase(page, capture, "blastn short query", async () => {
    await fillBaseForm(page, shortNucleotideQuery);
    await openAlgorithmParameters(page);
  }, (payload) => {
    expect(payload.additional_options).toContain("-task blastn-short");
  });

  await runCase(page, capture, "taxonomy include", async () => {
    await fillBaseForm(page, nucleotideQuery);
    await page.getByRole("button", { name: /Homo sapiens/i }).first().click();
  }, (payload) => {
    expect(payload.taxid).toBe(9606);
    expect(payload.is_inclusive).toBe(true);
  });

  await runCase(page, capture, "algorithm parameters", async () => {
    await fillBaseForm(page, nucleotideQuery);
    await openAlgorithmParameters(page);
    await page.getByLabel("Max target sequences").fill("50");
    await page.getByLabel("Expect threshold").fill("0.00001");
    await page.getByRole("spinbutton", { name: /Word size/i }).fill("11");
    await page.getByLabel("Output format").selectOption("6");
  }, (payload) => {
    expect(payload.max_target_seqs).toBe(50);
    expect(payload.evalue).toBe(0.00001);
    expect(payload.word_size).toBe(11);
    expect(payload.outfmt).toBe(6);
  });

  await runCase(page, capture, "masking options", async () => {
    await fillBaseForm(page, nucleotideQuery.toLowerCase());
    await openAlgorithmParameters(page);
    await page.getByLabel("Mask lower case letters").check();
  }, (payload) => {
    expect(payload.additional_options).toContain("-lcase_masking");
  });

  await runCase(page, capture, "protein blastp", async () => {
    await fillBaseForm(page, proteinQuery);
    await page.getByRole("button", { name: /^blastp/i }).click();
    await page.getByRole("radio", { name: /swissprot/i }).click();
  }, (payload) => {
    expect(payload.program).toBe("blastp");
    expect(payload.db).toBe("blast-db/swissprot/swissprot");
  });

  await collector.assertClean();
});

async function runCase(
  page: Page,
  capture: MockSubmitCapture,
  name: string,
  arrange: () => Promise<void>,
  assertPayload: (payload: Record<string, unknown>) => void,
) {
  await test.step(name, async () => {
    const before = capture.payloads.length;
    await page.goto("/blast/submit");
    await expect(page.getByText("ElasticBLAST New Search")).toBeVisible();
    await waitForRuntimeFixtures(page);
    await arrange();
    await submitFromRail(page);
    await expect.poll(() => capture.payloads.length).toBe(before + 1);
    assertPayload(capture.payloads[capture.payloads.length - 1]);
  });
}

async function fillBaseForm(page: Page, query: string) {
  const queryInput = page.getByPlaceholder(/Paste FASTA/i);
  await expect(queryInput).toBeEditable();
  await queryInput.fill(query);
  await expect(page.locator(".bsl-rail .blast-submit-btn")).toBeEnabled();
}

async function waitForRuntimeFixtures(page: Page) {
  await expect(page.locator('select option[value="aks-e2e"]')).toHaveCount(1);
  await expect(page.locator("select").first()).toHaveValue("aks-e2e");
  await expect(page.getByText("core_nt").first()).toBeVisible();
  await expect(page.getByLabel("Loading databases")).toHaveCount(0);
  await expect(page.getByLabel("Loading execution profile")).toHaveCount(0);
}

async function openAlgorithmParameters(page: Page) {
  const toggle = page.getByRole("button", { name: /Algorithm Parameters/i });
  await toggle.click();
  await expect(page.getByLabel("Max target sequences")).toBeVisible();
}

async function submitFromRail(page: Page) {
  const button = page.locator(".bsl-rail .blast-submit-btn");
  await expect(button).toBeEnabled();
  await button.click();
}