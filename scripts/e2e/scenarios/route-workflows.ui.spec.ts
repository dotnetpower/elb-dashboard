import { test, expect } from "../fixtures/uiTest";

test("Diagnostics renders a read-only reliability report", async ({ uiPage }) => {
  await uiPage.route("**/api/diagnostics/reliability?**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        category: "reliability",
        generated_at: "2026-08-27T00:00:00Z",
        findings: [],
        rollup: {},
        has_indeterminate: false,
      }),
    }),
  );

  await uiPage.goto("/diagnostics/reliability");
  await expect(
    uiPage.getByRole("heading", { name: "Diagnose & solve problems" }),
  ).toBeVisible();
  await expect(uiPage.getByText("No findings for the configured resources.")).toBeVisible();
});

test("Service Bus Playground validates without enqueueing", async ({ uiPage }) => {
  await uiPage.route("**/api/settings/service-bus", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        config: {
          revision: "e2e",
          enabled: false,
          auth_mode: "rbac",
          namespace_fqdn: "",
          request_queue: "elastic-blast-requests",
          completion_topic: "elastic-blast-completions",
          completion_kind: "topic",
          sas_secret_name: "",
          subscription_id: "",
          resource_group: "",
          cluster_name: "",
          storage_account: "",
          dlq_cleanup_enabled: false,
          dlq_max_age_days: 7,
          dlq_max_count: 1000,
          dlq_cleanup_batch: 100,
          updated_at: "",
          owner_oid: "",
          tenant_id: "",
        },
        env_enabled: false,
        effective_enabled: false,
        env_gate_enabled: false,
        kill_switch_enabled: false,
        counts: { available: false, reason: "disabled" },
      }),
    }),
  );
  await uiPage.route("**/api/settings/service-bus/observed-completions?**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        events: [],
        consumer_enabled: false,
        subscription: "playground-observer",
        subscriptions: ["playground-observer"],
        topic: "elastic-blast-completions",
      }),
    }),
  );
  const dryRunBodies: Array<{
    dry_run?: boolean;
    blast_options?: {
      max_target_seqs?: number;
      outfmt?: string;
      result_selection_policy?: string;
      candidate_pool_size?: number;
    };
  }> = [];
  await uiPage.route("**/api/settings/service-bus/send", async (route) => {
    const body = route.request().postDataJSON() as (typeof dryRunBodies)[number];
    dryRunBodies.push(body);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "valid",
        external_correlation_id: "corr-e2e",
        message_id: "",
      }),
    });
  });

  await uiPage.goto("/blast/playground");
  await expect(uiPage.getByRole("heading", { name: "Service Bus Playground" })).toBeVisible();
  const requestEditor = uiPage.locator("#pg-body-editor");
  await requestEditor.fill(
    JSON.stringify({
      query_fasta: ">stale\nACGT",
      db: "stale-db",
      program: "blastn",
    }),
  );
  await expect(uiPage.getByRole("button", { name: "Reset to form" })).toBeVisible();
  await uiPage.locator("#pg-preset").selectOption("core-nt-sequence-diversity");
  await expect(uiPage.getByRole("button", { name: "Reset to form" })).toHaveCount(0);
  await expect(requestEditor).not.toHaveValue(/stale-db/);
  const candidatePool = uiPage.locator("#pg-candidate-pool-size");
  const maxTarget = uiPage.locator("#pg-mts-t");
  const validate = uiPage.getByRole("button", { name: "Validate" });

  await expect(candidatePool).toHaveValue("2000");
  await candidatePool.fill("");
  await validate.click();
  await expect.poll(() => dryRunBodies.length).toBe(1);
  expect(dryRunBodies[0].dry_run).toBe(true);
  expect(dryRunBodies[0].blast_options).toMatchObject({
    max_target_seqs: 100,
    outfmt: "7 std sseq",
    result_selection_policy: "sequence_diversity",
  });
  expect(dryRunBodies[0].blast_options?.candidate_pool_size).toBeUndefined();

  await maxTarget.fill("10000");
  await candidatePool.fill("5000");
  await expect(
    uiPage.getByText(
      "candidate_pool_size must be greater than or equal to max_target_seqs.",
    ),
  ).toBeVisible();
  await expect(candidatePool).toHaveAttribute(
    "aria-describedby",
    "pg-candidate-pool-error",
  );
  await expect(validate).toBeDisabled();

  await candidatePool.fill("20000");
  await expect(validate).toBeEnabled();
  await validate.click();
  await expect.poll(() => dryRunBodies.length).toBe(2);
  expect(dryRunBodies[1].blast_options).toMatchObject({
    max_target_seqs: 10000,
    outfmt: "7 std sseq",
    result_selection_policy: "sequence_diversity",
    candidate_pool_size: 20000,
  });
  await expect(uiPage.getByText(/Validated \(no message sent\)/)).toBeVisible();
});

test("Sequence Detail hands the accession to New Search", async ({ uiPage }) => {
  const accession = "NR_123456.1";
  await uiPage.route(`**/api/ncbi/nuccore/${accession}`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        accession: "NR_123456",
        accession_version: accession,
        title: "E2E reference sequence",
        organism: "Escherichia coli",
        taxid: 562,
        length: 12,
        moltype: "DNA",
        biomol: "genomic",
        completeness: "complete",
        source_db: "refseq",
        strand: "double",
        topology: "linear",
        create_date: "2026-01-01",
        update_date: "2026-08-27",
        status: "live",
        replaced_by: null,
        cached: false,
        source: "esummary",
      }),
    }),
  );
  await uiPage.route(`**/api/ncbi/nuccore/${accession}/genbank`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        accession: "NR_123456",
        accession_version: accession,
        primary_accession: "NR_123456",
        gi: null,
        other_seqids: [],
        secondary_accessions: [],
        locus: "NR_123456",
        definition: "E2E reference sequence",
        length: 12,
        moltype: "DNA",
        topology: "linear",
        strandedness: "double",
        division: "BCT",
        create_date: "2026-01-01",
        update_date: "2026-08-27",
        organism: "Escherichia coli",
        taxonomy_lineage: "Bacteria; Proteobacteria",
        keywords: [],
        source: "Escherichia coli",
        comment: null,
        truncated_fields: [],
        features: [],
        references: [],
        xrefs: [],
        cached: false,
        data_source: "ncbi_eutils",
      }),
    }),
  );
  await uiPage.route(`**/api/ncbi/nuccore/${accession}/fasta`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/plain",
      body: `>${accession}\nACGTACGTACGT\n`,
    }),
  );
  await uiPage.route(`**/api/blast/jobs/by-accession/${accession}?**`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        accession,
        match: "base",
        scanned: 0,
        jobs: [],
        degraded: false,
      }),
    }),
  );

  await uiPage.goto(`/sequence/${accession}`);
  await expect(uiPage.getByRole("heading", { name: accession })).toBeVisible();
  await uiPage.getByRole("button", { name: "Use in BLAST" }).click();

  await expect(uiPage).toHaveURL(/\/blast\/submit\?accession=NR_123456\.1$/);
  await expect(uiPage.getByLabel("Or fetch by NCBI accession")).toHaveValue(accession);
});