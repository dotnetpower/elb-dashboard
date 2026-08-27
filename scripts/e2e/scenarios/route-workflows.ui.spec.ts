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
  let dryRun = false;
  await uiPage.route("**/api/settings/service-bus/send", async (route) => {
    const body = route.request().postDataJSON() as { dry_run?: boolean };
    dryRun = body.dry_run === true;
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
  await uiPage.getByRole("button", { name: "Validate" }).click();
  await expect.poll(() => dryRun).toBe(true);
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