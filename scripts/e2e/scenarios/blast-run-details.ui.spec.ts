import type { Page } from "@playwright/test";

import { test, expect } from "../fixtures/uiTest";

// Run details tab coverage for the queue/API job-detail enrichment work:
// config_snapshot projection, sibling execution stats (BLAST/DB version, run
// time), query identity (length + molecule), the BLAST command preview, and the
// raw-parameters panel. The detail API is fully stubbed so the scenario stays in
// the ui-mock project — no backend, no Azure, no auth. Navigation is bypassed by
// going straight to the job detail URL, so the responsive "More" nav grouping
// does not affect this lane.

// NOTE: a non-terminal phase is used on purpose. BlastResults auto-switches a
// COMPLETED job away from the Run details tab to the Descriptions analytics tab
// (rising-edge effect), which would unmount this grid mid-assertion. The grid's
// metadata rows are value-driven (independent of phase), so a `running` job with
// the captured fields renders exactly the same rows while staying on the tab.
const RICH_JOB_ID = "ext-meta-rich";
const RICH_DETAIL = {
  job_id: RICH_JOB_ID,
  status: "running",
  phase: "running",
  submission_source: "servicebus",
  queue_origin: "external",
  program: "blastn",
  db: "core_nt",
  created_at: "2026-06-20T00:00:00Z",
  updated_at: "2026-06-20T00:05:00Z",
  config_snapshot: {
    outfmt: "7 std staxids sscinames stitle qcovs",
    evalue: 0.01,
    max_target_seqs: 50,
    word_size: 28,
    extra: "-searchsp 32156241807668",
  },
  query_length: 1465,
  molecule: "nucleotide",
  db_version: "2026-06-06-01-05-02",
  blast_version: "2.17.0+",
  run_seconds: 95,
  infrastructure: {
    cluster_name: "elb-cluster-01",
    region: "koreacentral",
    resource_group: "rg-elb-cluster",
    storage_account: "elbstg01",
  },
  meta: {},
};

const LEGACY_JOB_ID = "ext-meta-legacy";
const LEGACY_DETAIL = {
  job_id: LEGACY_JOB_ID,
  status: "running",
  phase: "running",
  submission_source: "servicebus",
  program: "blastn",
  db: "core_nt",
  created_at: "2026-05-01T00:00:00Z",
  updated_at: "2026-05-01T00:04:00Z",
  meta: {},
};

// Stub the detail GET for a specific job id only; let every nested route
// (events / queue / results / database-metadata) fall through to the core
// ui-mock so the page renders without an error boundary.
async function stubDetail(
  page: Page,
  jobId: string,
  detail: Record<string, unknown>,
): Promise<void> {
  await page.route(`**/api/blast/jobs/${jobId}**`, (route) => {
    const url = new URL(route.request().url());
    if (
      url.pathname === `/api/blast/jobs/${jobId}` &&
      route.request().method() === "GET"
    ) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(detail),
      });
    }
    return route.continue();
  });
  // The Run details tab renders ExecutionStepsCard, which polls
  // `/execution-steps`. Registered AFTER the broad detail route so it takes
  // precedence for that exact path (Playwright evaluates the most-recently
  // registered matching route first). A 5xx here would trip the client-issue
  // collector even though the metadata rows render fine.
  await page.route(`**/api/blast/jobs/${jobId}/execution-steps**`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: 1,
        job_id: jobId,
        status: "running",
        phase: "running",
        artifact_state: "pending",
        output: { status: "running" },
      }),
    }),
  );
}

test("Run details renders captured queue-job metadata, BLAST command, and raw params", async ({
  uiPage,
}) => {
  await stubDetail(uiPage, RICH_JOB_ID, RICH_DETAIL);
  await uiPage.goto(`/blast/jobs/${RICH_JOB_ID}?tab=run`);

  // Scope all assertions to the Run details grid so the Database-metadata card
  // (which renders its own "nucleotide" / "Query length" terms elsewhere on the
  // page) cannot trip Playwright strict mode.
  const grid = uiPage.getByTestId("blast-run-details-grid");

  // Anchor: confirm our stubbed detail actually drove the grid before asserting
  // the rest, so a mock-wiring regression fails here with a clear signal.
  await expect(grid.getByText(RICH_JOB_ID, { exact: true })).toBeVisible();

  // Captured BLAST options surface as labelled rows (config_snapshot projection).
  await expect(grid.getByText("Output format", { exact: true })).toBeVisible();
  await expect(
    grid.getByText("7 std staxids sscinames stitle qcovs", { exact: true }),
  ).toBeVisible();
  await expect(grid.getByText("E-value", { exact: true })).toBeVisible();
  await expect(grid.getByText("Max targets", { exact: true })).toBeVisible();
  await expect(grid.getByText("Word size", { exact: true })).toBeVisible();

  // Sibling execution stats merged onto the detail.
  await expect(grid.getByText("BLAST version", { exact: true })).toBeVisible();
  await expect(grid.getByText("2.17.0+", { exact: true })).toBeVisible();
  await expect(grid.getByText("DB version", { exact: true })).toBeVisible();
  await expect(grid.getByText("Run time", { exact: true })).toBeVisible();

  // Query identity (length + molecule) without a Storage blob read.
  await expect(grid.getByText("Query length", { exact: true })).toBeVisible();
  await expect(grid.getByText(/1,465\s+nt/)).toBeVisible();
  await expect(grid.getByText("Molecule", { exact: true })).toBeVisible();
  await expect(grid.getByText("nucleotide", { exact: true })).toBeVisible();

  // BLAST command preview + raw-parameters panel.
  await expect(grid.getByText("BLAST command", { exact: true })).toBeVisible();
  await expect(grid.getByText(/blastn .*-db core_nt .*-outfmt/)).toBeVisible();
  await expect(grid.getByText("Raw parameters", { exact: true })).toBeVisible();
});

test("Run details shows 'not recorded' for a legacy queue job without captured params", async ({
  uiPage,
}) => {
  await stubDetail(uiPage, LEGACY_JOB_ID, LEGACY_DETAIL);
  await uiPage.goto(`/blast/jobs/${LEGACY_JOB_ID}?tab=run`);

  const grid = uiPage.getByTestId("blast-run-details-grid");
  await expect(grid.getByText("Parameters", { exact: true })).toBeVisible();
  await expect(grid.getByText("not recorded for this job")).toBeVisible();
  // No captured options -> the raw-parameters panel is absent. (The BLAST
  // command preview still renders the bare `blastn -db core_nt` derived from
  // program + db, so it is intentionally NOT asserted absent here.)
  await expect(grid.getByText("Raw parameters", { exact: true })).toHaveCount(0);
  await expect(grid.getByText(/blastn .*-db core_nt/)).toBeVisible();
  await expect(grid.getByText(/-outfmt|-evalue/)).toHaveCount(0);
});

test("Run details raw-parameters panel expands to the captured config JSON", async ({
  uiPage,
}) => {
  await stubDetail(uiPage, RICH_JOB_ID, RICH_DETAIL);
  await uiPage.goto(`/blast/jobs/${RICH_JOB_ID}?tab=run`);

  const grid = uiPage.getByTestId("blast-run-details-grid");
  const summary = grid.getByText("Raw parameters", { exact: true });
  await expect(summary).toBeVisible();

  // The <pre> JSON is collapsed inside <details> until the summary is toggled.
  const json = grid.locator("details > pre");
  await expect(json).toBeHidden();
  await summary.click();
  await expect(json).toBeVisible();
  await expect(json).toContainText('"outfmt"');
  await expect(json).toContainText("32156241807668");
});

test("Run details Job ID copy button confirms the copy", async ({ uiPage }) => {
  await stubDetail(uiPage, RICH_JOB_ID, RICH_DETAIL);
  await uiPage.goto(`/blast/jobs/${RICH_JOB_ID}?tab=run`);

  const grid = uiPage.getByTestId("blast-run-details-grid");
  const copyBtn = grid.getByRole("button", { name: "Copy Job ID" });
  await expect(copyBtn).toBeVisible();
  await copyBtn.click();
  // Copy success toggles the copy-btn--copied modifier (icon swaps to a check).
  // navigator.clipboard.writeText is stubbed by the ui-mock fixture.
  await expect(grid.locator("button.copy-btn--copied")).toBeVisible();
});

const FAILED_JOB_ID = "ext-meta-failed";
const FAILED_DETAIL = {
  job_id: FAILED_JOB_ID,
  status: "failed",
  phase: "failed",
  submission_source: "servicebus",
  program: "blastn",
  db: "core_nt",
  created_at: "2026-06-20T00:00:00Z",
  updated_at: "2026-06-20T00:03:00Z",
  error_code:
    "BLAST database core_nt memory requirements exceed memory available on the selected machine type",
  // BlastJobFailureBanner surfaces job.error (via getFailureText); an ERROR:
  // prefix makes firstErrorLine pick it as the summary line.
  error:
    "ERROR: BLAST database core_nt memory requirements exceed memory available on the selected machine type",
  config_snapshot: {
    outfmt: "7 std staxids sscinames stitle qcovs",
    evalue: 0.01,
    max_target_seqs: 50,
  },
  meta: {},
};

test("Run details renders a failed queue job with its captured parameters", async ({
  uiPage,
}) => {
  await stubDetail(uiPage, FAILED_JOB_ID, FAILED_DETAIL);
  await uiPage.goto(`/blast/jobs/${FAILED_JOB_ID}?tab=run`);

  const grid = uiPage.getByTestId("blast-run-details-grid");
  await expect(grid.getByText(FAILED_JOB_ID, { exact: true })).toBeVisible();
  // A failed job still shows its captured parameters in the grid.
  await expect(grid.getByText("Output format", { exact: true })).toBeVisible();
  await expect(grid.getByText("E-value", { exact: true })).toBeVisible();

  // The failure banner (a sibling of the grid) renders the header + the
  // orchestrator error summary derived from job.error. The same error also
  // appears in the execution-steps card, so scope to the first match (banner).
  await expect(uiPage.getByText(/Job Failed at/)).toBeVisible();
  await expect(
    uiPage.getByText(/BLAST database core_nt memory requirements/).first(),
  ).toBeVisible();
});

const FULL_JOB_ID = "ext-meta-full";
const FULL_DETAIL = {
  job_id: FULL_JOB_ID,
  status: "running",
  phase: "running",
  submission_source: "servicebus",
  program: "blastp",
  db: "nr",
  created_at: "2026-06-20T00:00:00Z",
  updated_at: "2026-06-20T00:05:00Z",
  // A protein query so the Query length unit renders "aa" (not "nt").
  query_length: 350,
  molecule: "protein",
  config_snapshot: {
    outfmt: "7 std staxids sscinames stitle qcovs",
    evalue: 0.001,
    max_target_seqs: 100,
    word_size: 6,
    dust: "no",
    machine_type: "Standard_E16s_v5",
    num_nodes: 4,
    // taxid + is_inclusive:false renders the "exclude taxid …" filter label.
    taxid: 9606,
    is_inclusive: false,
  },
  meta: {},
};

test("Run details renders the conditional option rows + protein molecule unit", async ({
  uiPage,
}) => {
  await stubDetail(uiPage, FULL_JOB_ID, FULL_DETAIL);
  await uiPage.goto(`/blast/jobs/${FULL_JOB_ID}?tab=run`);

  const grid = uiPage.getByTestId("blast-run-details-grid");
  await expect(grid.getByText(FULL_JOB_ID, { exact: true })).toBeVisible();

  // Conditional option rows only render when their key is present.
  await expect(grid.getByText("Dust", { exact: true })).toBeVisible();
  await expect(grid.getByText("Machine", { exact: true })).toBeVisible();
  await expect(grid.getByText("Standard_E16s_v5", { exact: true })).toBeVisible();
  await expect(grid.getByText("Nodes", { exact: true })).toBeVisible();
  await expect(grid.getByText("Taxonomy filter", { exact: true })).toBeVisible();
  await expect(grid.getByText(/exclude taxid 9606/)).toBeVisible();

  // Protein query -> "aa" unit + Molecule row.
  await expect(grid.getByText("Molecule", { exact: true })).toBeVisible();
  await expect(grid.getByText("protein", { exact: true })).toBeVisible();
  await expect(grid.getByText(/350\s+aa/)).toBeVisible();
});
