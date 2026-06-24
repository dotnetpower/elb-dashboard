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
