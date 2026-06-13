import { test, expect } from "../fixtures/uiTest";

/**
 * MessageFlow constellation — ui-mock coverage.
 *
 * The Service Bus MessageFlow card hides itself unless the integration is
 * effective-enabled, so the shared fixture defaults `/api/monitor/message-flow`
 * to `{ enabled: false }`. This scenario re-registers that route with an
 * enabled snapshot (Playwright matches the most-recently-registered route
 * first) and exercises the full path: card summary -> expand -> d3
 * constellation -> click a job node -> JSON detail modal. It also asserts the
 * security redaction (charter §12): the rendered job JSON must NOT echo the raw
 * owner/tenant GUIDs the detail endpoint returns.
 */

const OWNER_OID = "11111111-2222-3333-4444-555555555555";
const TENANT_ID = "99999999-8888-7777-6666-555555555555";
const JOB_ID = "abc123def456";

const ENABLED_SNAPSHOT = {
  enabled: true,
  scope: "shared",
  namespace_fqdn: "sb-elb-e2e.servicebus.windows.net",
  request_queue: "blast-requests",
  completion_topic: "blast-completions",
  sb_counts: {
    available: true,
    queue: {
      active_message_count: 2,
      scheduled_message_count: 0,
      dead_letter_message_count: 0,
    },
  },
  active_total: 3,
  active_shown: 3,
  broker_truncated: false,
  read_truncated: false,
  producers: [
    { alias: "dashboard", job_count: 2, sources: ["dashboard"] },
    { alias: "api-client", job_count: 1, sources: ["external_api"] },
  ],
  broker: [
    {
      job_id: JOB_ID,
      program: "blastn",
      db: "core_nt",
      status: "running",
      phase: "running",
      query_label: "NR_024570.1",
      query_size: 540,
      alias: "dashboard",
      submission_source: "dashboard",
      cluster_name: "elb-cluster-01",
      created_at: new Date().toISOString(),
    },
    {
      job_id: "queued0000001",
      program: "blastn",
      db: "core_nt",
      status: "queued",
      phase: "queued",
      query_label: "queued query",
      query_size: 120,
      alias: "api-client",
      submission_source: "external_api",
      cluster_name: "elb-cluster-01",
      created_at: new Date().toISOString(),
    },
    {
      job_id: "running0000002",
      program: "blastn",
      db: "16S_ribosomal_RNA",
      status: "running",
      phase: "running",
      query_label: null,
      query_size: null,
      alias: "dashboard",
      submission_source: "dashboard",
      cluster_name: "elb-cluster-01",
      created_at: null,
    },
  ],
  consumers: {
    clusters: [
      {
        cluster_name: "elb-cluster-01",
        resource_group: "rg-elb-cluster",
        subscription_id: "00000000-0000-0000-0000-000000000000",
        running: 2,
        queued: 1,
        total: 3,
      },
    ],
  },
};

// Job detail returned by /api/monitor/jobs/{id}. Includes nested owner_oid /
// tenant_id so the scenario can prove the modal redacts them (charter §12).
const JOB_DETAIL = {
  state: {
    job_id: JOB_ID,
    status: "running",
    program: "blastn",
    db: "core_nt",
    owner_oid: OWNER_OID,
    tenant_id: TENANT_ID,
    payload: {
      metadata: {
        owner_oid: OWNER_OID,
        cluster_name: "elb-cluster-01",
      },
    },
  },
  history: [],
};

test("MessageFlow card expands to the constellation and opens a redacted job detail", async ({
  uiPage,
}) => {
  // Override the disabled default with an enabled snapshot + a job detail.
  await uiPage.route("**/api/monitor/message-flow", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(ENABLED_SNAPSHOT),
    }),
  );
  await uiPage.route("**/api/monitor/jobs/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(JOB_DETAIL),
    }),
  );

  await uiPage.goto("/");
  await expect(uiPage.getByText("ElasticBLAST Dashboard")).toBeVisible();

  // Card summary renders the active-job count and submitters.
  const card = uiPage.locator(".glass-card", { hasText: "Message Flow" }).first();
  await expect(card).toBeVisible();
  await expect(card.getByText(/active jobs/)).toBeVisible();
  await expect(card.getByText(/submitter/)).toBeVisible();

  // Expand into the full modal.
  await card.getByRole("button", { name: "Expand message flow" }).click();
  const modal = uiPage.getByRole("dialog", { name: "Service Bus message flow" });
  await expect(modal).toBeVisible();

  // The d3 constellation SVG renders with focusable job nodes. The SVG sizes
  // itself from the container via ResizeObserver, so assert on the job node
  // (the real interactive target) rather than the SVG box, which may report a
  // zero size before the first observer tick.
  const svg = modal.locator("svg.mf-constellation, .mf-constellation svg").first();
  await expect(svg).toBeAttached();
  const jobNode = modal
    .getByRole("button", { name: new RegExp(`View JSON for blastn job ${JOB_ID}`) })
    .first();
  await expect(jobNode).toBeVisible({ timeout: 15_000 });

  // Click a job node -> JSON detail modal opens.
  await jobNode.click();
  const detail = uiPage.getByRole("dialog", {
    name: new RegExp(`Job detail for blastn ${JOB_ID}`),
  });
  await expect(detail).toBeVisible();

  // Security: the rendered JSON must NOT echo the raw owner/tenant GUIDs
  // (charter §12 — sanitise UI output; redactState strips them recursively).
  const pre = detail.locator("pre");
  await expect(pre).toBeVisible();
  await expect(pre).toContainText(JOB_ID);
  await expect(pre).not.toContainText(OWNER_OID);
  await expect(pre).not.toContainText(TENANT_ID);

  // Escape closes the detail, then the modal.
  await uiPage.keyboard.press("Escape");
  await expect(detail).toHaveCount(0);
  await uiPage.keyboard.press("Escape");
  await expect(modal).toHaveCount(0);
});

test("MessageFlow card stays hidden when the integration is disabled", async ({ uiPage }) => {
  // Default fixture route already returns { enabled: false }; assert the card
  // renders nothing so the integration-off dashboard is unchanged.
  await uiPage.goto("/");
  await expect(uiPage.getByText("ElasticBLAST Dashboard")).toBeVisible();
  await expect(uiPage.locator(".glass-card", { hasText: "Message Flow" })).toHaveCount(0);
});
