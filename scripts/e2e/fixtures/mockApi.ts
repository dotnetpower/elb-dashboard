import type { Page, Route } from "@playwright/test";

import { workspaceConfig } from "../scenarios/helpers/workspace";

export interface UiMockState {
  aksActions: Array<{ action: string; payload: Record<string, unknown> }>;
  buildRequests: Array<Record<string, unknown>>;
  customDbBuilds: Array<Record<string, unknown>>;
  dbCancels: string[];
  dbDownloads: Array<Record<string, unknown>>;
  jobDeletes: string[];
  scheduleRuns: string[];
  scheduleDeletes: string[];
  upgradeRollbacks: number;
  upgradeStarts: Array<Record<string, unknown>>;
}

const now = new Date("2026-05-24T10:00:00.000Z").toISOString();
// The fixture job's timestamps must stay inside the user's local "today" so
// the Recent searches view always renders a "Today" group button. Using a
// fixed `now` placed the job in "Yesterday" once enough hours had passed.
const recentNow = new Date(Date.now() - 5 * 60 * 1000).toISOString();

export const e2eCluster = {
  name: "aks-e2e",
  resource_group: workspaceConfig.workloadResourceGroup,
  region: workspaceConfig.region,
  k8s_version: "1.34.0",
  provisioning_state: "Succeeded",
  power_state: "Running",
  node_count: 3,
  node_sku: "Standard_E32s_v5",
  kubelet_object_id: "00000000-0000-0000-0000-000000000001",
  network_plugin: "azure",
  fqdn: "aks-e2e.example.local",
  agent_pools: [
    {
      name: "workload",
      vm_size: "Standard_E32s_v5",
      count: 3,
      min_count: 3,
      max_count: 3,
      os_type: "Linux",
      mode: "User",
      power_state: "Running",
      enable_auto_scaling: false,
    },
  ],
};

export const e2eDatabases = [
  {
    name: "core_nt",
    container: "blast-db",
    prefix: "core_nt",
    source: "ncbi",
    file_count: 12,
    total_bytes: 268_435_456,
    total_letters: 10_000_000,
    web_blast_searchsp: 10_000_000,
    web_blast_searchsp_scope: "e2e fixture",
    source_version: "2026-05-20",
    shard_sets: [1, 3],
    sharded: true,
  },
  {
    name: "16S_ribosomal_RNA",
    container: "blast-db",
    prefix: "16S_ribosomal_RNA",
    source: "ncbi",
    file_count: 4,
    total_bytes: 18_874_368,
    total_letters: 1_500_000,
    web_blast_searchsp: 1_500_000,
    web_blast_searchsp_scope: "e2e fixture",
  },
  {
    name: "swissprot",
    container: "blast-db",
    prefix: "swissprot",
    source: "ncbi",
    file_count: 6,
    total_bytes: 314_572_800,
    total_letters: 2_000_000,
    web_blast_searchsp: 2_000_000,
    web_blast_searchsp_scope: "e2e fixture",
  },
];

async function jsonResponse(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

export async function installCoreUiMocks(page: Page): Promise<UiMockState> {
  const state: UiMockState = {
    aksActions: [],
    buildRequests: [],
    customDbBuilds: [],
    dbCancels: [],
    dbDownloads: [],
    jobDeletes: [],
    scheduleRuns: [],
    scheduleDeletes: [],
    upgradeRollbacks: 0,
    upgradeStarts: [],
  };

  const completedJob = {
    job_id: "job-e2e",
    job_title: "E2E fixture job",
    program: "blastn",
    db: "blast-db/core_nt/core_nt",
    status: "completed",
    phase: "completed",
    created_at: recentNow,
    updated_at: recentNow,
    query_label: "e2e_16s",
    owner_upn: "researcher@example.test",
    payload: {
      subscription_id: workspaceConfig.subscriptionId,
      resource_group: workspaceConfig.workloadResourceGroup,
      storage_account: workspaceConfig.storageAccountName,
      aks_cluster_name: e2eCluster.name,
      region: workspaceConfig.region,
      program: "blastn",
      db: "blast-db/core_nt/core_nt",
      query_data: ">e2e_16s\nAGAGTTTGATCCTGGCTCAG\n",
      job_title: "E2E fixture job",
      evalue: 0.05,
      max_target_seqs: 10,
      outfmt: 5,
    },
    infrastructure: {
      subscription_id: workspaceConfig.subscriptionId,
      resource_group: workspaceConfig.workloadResourceGroup,
      storage_account: workspaceConfig.storageAccountName,
      cluster_name: e2eCluster.name,
      region: workspaceConfig.region,
      acr_name: workspaceConfig.acrName,
    },
    database_metadata: {
      name: "core_nt",
      database: "core_nt",
      title: "core_nt",
      molecule_type: "mixed DNA",
      number_of_sequences: 1000,
      number_of_letters: 1000000,
      source_version: "2026-05-20",
    },
  };

  const fixtureHit = {
    qseqid: "e2e_16s",
    sseqid: "NR_123456.1",
    pident: 98.7,
    length: 120,
    mismatch: 1,
    gapopen: 0,
    qstart: 1,
    qend: 120,
    sstart: 10,
    send: 129,
    evalue: 1e-40,
    bitscore: 220.5,
    qcovs: 96,
    stitle: "Escherichia coli 16S ribosomal RNA",
    sscinames: "Escherichia coli",
    staxids: "562",
    shard: "0",
  };

  const upgradeStatus = {
    running_version: "0.2.0",
    running_sha: "1111111",
    running_revision: "rev-current",
    current_images: { api: "api:0.2.0", frontend: "frontend:0.2.0", terminal: "terminal:0.2.0" },
    latest_version: "0.3.0",
    latest_sha: "2222222",
    latest_checked_at: now,
    git_remote: "origin",
    track_commits: true,
    latest_commit_sha: "",
    state: "idle",
    target_version: "",
    target_sha: "",
    target_kind: "release",
    job_id: "",
    started_by_oid: "",
    started_at: "",
    phase_detail: "idle",
    phase_progress: 0,
    build_log_blob: "",
    rollback_target: { api: "api:0.2.0", frontend: "frontend:0.2.0", terminal: "terminal:0.2.0" },
    rollback_available_until: "2026-05-25T10:00:00.000Z",
    updated_at: now,
  };

  await page.route("**/api/me", (route) =>
    jsonResponse(route, {
      oid: "e2e-user",
      name: "E2E User",
      roles: ["dashboard-user"],
    }),
  );

  await page.route("**/api/upgrade/status", (route) => jsonResponse(route, upgradeStatus));
  await page.route("**/api/upgrade/candidates", (route) =>
    jsonResponse(route, {
      configured: true,
      remote: "origin",
      running_version: "0.2.0",
      candidates: [{ name: "0.3.0", raw_ref: "refs/tags/v0.3.0", commit_sha: "2222222abcdef" }],
    }),
  );
  await page.route("**/api/upgrade/history?**", (route) =>
    jsonResponse(route, { events: [{ ts: now, job_id: "upgrade-e2e", event: "checked", version: "0.3.0" }] }),
  );
  await page.route("**/api/upgrade/check", (route) => jsonResponse(route, { ...upgradeStatus, latest_checked_at: now }));
  await page.route("**/api/upgrade/start", async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    state.upgradeStarts.push(payload);
    await jsonResponse(route, { ...upgradeStatus, state: "queued", target_version: payload.target_version, job_id: "upgrade-e2e", phase_detail: "queued", phase_progress: 5 });
  });
  await page.route("**/api/upgrade/rollback", async (route) => {
    state.upgradeRollbacks += 1;
    await jsonResponse(route, { ...upgradeStatus, state: "rolling_back", job_id: "upgrade-e2e", phase_detail: "rollback queued", phase_progress: 10 });
  });
  await page.route("**/api/upgrade/rollback-preflight", (route) =>
    jsonResponse(route, {
      available: true,
      reason: "ok",
      images: Object.values(upgradeStatus.rollback_target).map((image_ref) => ({ image_ref, exists: true, created_on: now, error: "" })),
    }),
  );
  await page.route("**/api/upgrade/escape-hatch", (route) =>
    jsonResponse(route, {
      container_app: "ca-elb-dashboard",
      subscription_id: workspaceConfig.subscriptionId,
      resource_group: workspaceConfig.workloadResourceGroup,
      target_images: upgradeStatus.rollback_target,
      commands: ["az containerapp revision list --name ca-elb-dashboard", "az containerapp update --image api:0.2.0"],
    }),
  );

  await page.route("**/api/arm/subscriptions", (route) =>
    jsonResponse(route, [
      {
        subscriptionId: workspaceConfig.subscriptionId,
        displayName: "E2E Subscription",
        state: "Enabled",
        tenantId: "00000000-0000-0000-0000-000000000010",
      },
    ]),
  );
  await page.route(/\/api\/arm\/subscriptions\/[^/]+\/resource-groups$/, (route) =>
    jsonResponse(route, [
      {
        name: workspaceConfig.workloadResourceGroup,
        location: workspaceConfig.region,
        tags: {
          "elb-workload-rg": workspaceConfig.workloadResourceGroup,
          "elb-acr-rg": workspaceConfig.acrResourceGroup,
          "elb-acr": workspaceConfig.acrName,
          "elb-storage": workspaceConfig.storageAccountName,
          "elb-region": workspaceConfig.region,
        },
      },
      { name: "MC_aks-e2e_nodes", location: workspaceConfig.region, tags: {} },
      { name: "rg-unrelated", location: "eastus", tags: {} },
    ]),
  );
  await page.route("**/api/arm/resource-group/tags?**", (route) =>
    jsonResponse(route, {
      resource_group: workspaceConfig.workloadResourceGroup,
      tags: {
        "elb-acr-rg": workspaceConfig.acrResourceGroup,
        "elb-acr": workspaceConfig.acrName,
        "elb-storage": workspaceConfig.storageAccountName,
        "elb-region": workspaceConfig.region,
      },
    }),
  );
  await page.route(/\/api\/arm\/subscriptions\/[^/]+\/locations$/, (route) =>
    jsonResponse(route, [
      {
        name: workspaceConfig.region,
        displayName: "Korea Central",
        regionalDisplayName: "(Asia Pacific) Korea Central",
      },
    ]),
  );
  await page.route(/\/api\/arm\/subscriptions\/[^/]+\/resource-groups\/[^/]+\/storage-accounts$/, (route) =>
    jsonResponse(route, [{ name: workspaceConfig.storageAccountName, location: workspaceConfig.region }]),
  );
  await page.route(/\/api\/arm\/subscriptions\/[^/]+\/resource-groups\/[^/]+\/acrs$/, (route) =>
    jsonResponse(route, [
      {
        name: workspaceConfig.acrName,
        location: workspaceConfig.region,
        loginServer: `${workspaceConfig.acrName}.azurecr.io`,
      },
    ]),
  );

  await page.route("**/api/monitor/aks?**", (route) =>
    jsonResponse(route, { clusters: [e2eCluster] }),
  );
  await page.route("**/api/monitor/aks/warmup-status?**", (route) =>
    jsonResponse(route, {
      warm: true,
      workspace_ready: 1,
      workspace_desired: 1,
      databases: [
        {
          name: "core_nt",
          mol_type: "nucl",
          status: "Ready",
          sources: ["warmup"],
          nodes_ready: 3,
          nodes_failed: 0,
          nodes_active: 0,
          total_jobs: 3,
        },
      ],
      vmtouch_ready: 3,
      namespaces: ["default"],
    }),
  );
  await page.route("**/api/monitor/aks/service-ip?**", (route) =>
    jsonResponse(route, { service_name: "elb-openapi", external_ip: "127.0.0.1" }),
  );
  // ClusterCard hydrates the "Provisioning failed." banner from this endpoint;
  // without a stub the real local api answers from a long-lived jobstate row
  // and every safe-scenario trips assertNoErrorBoundary on getByRole('alert').
  await page.route("**/api/aks/recent-failed-provisions**", (route) =>
    jsonResponse(route, { jobs: [], degraded: false }),
  );
  await page.route("**/api/monitor/storage?**", (route) =>
    jsonResponse(route, {
      name: workspaceConfig.storageAccountName,
      region: workspaceConfig.region,
      sku: "Standard_LRS",
      kind: "StorageV2",
      public_network_access: "Disabled",
      is_hns_enabled: false,
      containers: [
        {
          name: "blast-db",
          public_access: null,
          last_modified_time: now,
          blob_count: 22,
          size_bytes: 287_309_824,
        },
        {
          name: "queries",
          public_access: null,
          last_modified_time: now,
          blob_count: 2,
          size_bytes: 2048,
        },
      ],
    }),
  );
  await page.route("**/api/monitor/acr?**", (route) =>
    jsonResponse(route, {
      name: workspaceConfig.acrName,
      login_server: `${workspaceConfig.acrName}.azurecr.io`,
      sku: "Basic",
      expected_image_tags: {
        "elastic-blast": "1.0.0",
        "elb-openapi": "1.0.0",
      },
      actual_tags: { "elastic-blast": ["1.0.0"], "elb-openapi": ["1.0.0"] },
      build_details: [],
    }),
  );
  await page.route("**/api/monitor/terminal?**", (route) =>
    jsonResponse(route, {
      name: "terminal-sidecar",
      region: workspaceConfig.region,
      vm_size: null,
      provisioning_state: "Succeeded",
      power_state: "Running",
      os_disk_gb: null,
      public_ip: null,
      fqdn: null,
      has_managed_identity: true,
      identity_type: "UserAssigned",
    }),
  );
  await page.route("**/api/blast/databases?**", (route) =>
    jsonResponse(route, { databases: e2eDatabases, public_access_disabled: false }),
  );
  await page.route("**/api/blast/databases/build", async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    state.customDbBuilds.push(payload);
    await jsonResponse(route, {
      db_name: payload.db_name,
      db_type: payload.db_type,
      title: payload.title ?? payload.db_name,
      status: "created",
      file_count: 3,
      container: "blast-db",
      path: `custom_db/${payload.db_name}/${payload.db_name}`,
    });
  });
  await page.route(/\/api\/blast\/databases\/[^/]+\/preview$/, (route) => {
    const dbName = decodeURIComponent(route.request().url().split("/api/blast/databases/")[1].split("/preview")[0]);
    return jsonResponse(route, {
      db_name: dbName,
      snapshot: "2026-05-20",
      available: true,
      file_count: 4,
      volume_count: 4,
      total_bytes_estimate: 18_874_368,
      last_modified: now,
      files_sample: [`${dbName}.00.tar.gz`],
    });
  });

  await page.route("**/api/monitor/metrics?**", (route) =>
    jsonResponse(route, {
      window_seconds: 900,
      now_ts: Date.now() / 1000,
      path_prefix: null,
      total: 8,
      errors: 0,
      error_rate: 0,
      p50_ms: 18,
      p95_ms: 42,
      p99_ms: 50,
      rpm: [],
      by_path: [{ path: "/api/health", count: 4, errors: 0, p95_ms: 12 }],
    }),
  );
  await page.route("**/api/monitor/sidecar-requests?**", (route) =>
    jsonResponse(route, { items: [], count: 0, capacity: 200 }),
  );
  const sidecarsSnapshot = {
    ts: Date.now() / 1000,
    revision: "e2e-revision",
    sidecars: Object.fromEntries(
      ["frontend", "api", "worker", "beat", "redis", "terminal"].map((name) => [
        name,
        { name, health: "ok", ts: Date.now() / 1000, cpu_pct: 1, mem_bytes: 64_000_000 },
      ]),
    ),
    events: { row1: 1, row2: 0, row3: 0, row4: 0 },
  };
  await page.route("**/api/monitor/sidecars", (route) => jsonResponse(route, sidecarsSnapshot));
  await page.route("**/api/monitor/sidecars/ticket", (route) =>
    jsonResponse(route, { ticket: "e2e-sidecars", expires_at: now }),
  );
  await page.route("**/api/monitor/sidecars/events?**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: `event: snapshot\ndata: ${JSON.stringify(sidecarsSnapshot)}\n\n`,
    }),
  );
  await page.route("**/api/monitor/logs/ticket", (route) =>
    jsonResponse(route, { ticket: "e2e-logs", expires_at: now }),
  );
  await page.route(/\/api\/monitor\/logs\/[^/]+\/events\?/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: "event: line\ndata: {\"text\":\"e2e log line\"}\n\n",
    }),
  );
  await page.route(/\/api\/monitor\/logs\/[^/]+\/recent\?/, (route) =>
    jsonResponse(route, { lines: [{ ts: now, text: "e2e log line" }] }),
  );

  await page.route("**/api/terminal/health", (route) =>
    jsonResponse(route, { status: "ok", user: "azureuser", cwd: "/home/azureuser" }),
  );
  await page.route("**/api/terminal/azure-cli?**", (route) =>
    jsonResponse(route, { status: "signed_in", user: "researcher@example.test", subscription: workspaceConfig.subscriptionId }),
  );
  await page.route("**/api/terminal/ticket", (route) =>
    jsonResponse(route, { detail: "terminal websocket disabled in e2e fixture" }, 401),
  );

  await page.route(/\/api\/blast\/jobs\?.*/, (route) =>
    jsonResponse(route, {
      jobs: [
        {
            ...completedJob,
        },
      ],
    }),
  );
  await page.route("**/api/blast/jobs/job-e2e/execution-steps", (route) =>
    jsonResponse(route, { schema_version: 1, job_id: "job-e2e", status: "completed", phase: "completed", created_at: now, updated_at: now, artifact_state: "ready" }),
  );
  await page.route("**/api/blast/jobs/job-e2e/results/aggregate?**", (route) =>
    jsonResponse(route, {
      job_id: "job-e2e",
      status: "ready",
      stats: { total_hits: 1, query_count: 1, subject_count: 1, min_evalue: 1e-40, max_bitscore: 220.5 },
      files_parsed: 1,
      total_files: 1,
    }),
  );
  await page.route("**/api/blast/jobs/job-e2e/results/alignments?**", (route) =>
    jsonResponse(route, {
      job_id: "job-e2e",
      blob_name: "job-e2e/results.xml",
      blob_names: ["job-e2e/results.xml"],
      alignments: [fixtureHit],
      total_hits: 1,
      filtered_hits: 1,
      returned: 1,
      query_ids: ["e2e_16s"],
      subject_aggregates: [{ sseqid: "NR_123456.1", max_bitscore: 220.5, total_bitscore: 441, hsp_count: 2, sscinames: "Escherichia coli", staxids: "562" }],
      page: 1,
      page_size: 25,
      pages: 1,
      files_parsed: 1,
      total_files: 1,
    }),
  );
  await page.route("**/api/blast/jobs/job-e2e/results/taxonomy?**", (route) =>
    jsonResponse(route, {
      job_id: "job-e2e",
      organisms: [{ key: "562", organism: "Escherichia coli", taxid: "562", count: 1, best_evalue: 1e-40, top_bitscore: 220.5, blast_name: "bacteria", lineage_ex: [{ rank: "species", taxid: 562, scientific_name: "Escherichia coli" }] }],
      total_hits: 1,
      filtered_hits: 1,
      files_parsed: 1,
      total_files: 1,
      read_failures: 0,
      lineage: { requested: true, looked_up: 1, failed: 0 },
    }),
  );
  await page.route("**/api/blast/jobs/job-e2e/results/export?**", (route) =>
    route.fulfill({ status: 200, contentType: "text/csv", body: "qseqid,sseqid\ne2e_16s,NR_123456.1\n" }),
  );
  await page.route("**/api/blast/jobs/job-e2e/results?**", (route) =>
    jsonResponse(route, {
      job_id: "job-e2e",
      files: [{ file_id: "result-main", name: "job-e2e/results.xml", size: 2048, last_modified: now, format: "xml", source: "result" }],
      manifest: { schema_version: 1, job_id: "job-e2e", status: "available", source: "e2e", file_count: 1, parseable_count: 1, files: [{ file_id: "result-main", name: "job-e2e/results.xml", size: 2048, last_modified: now, format: "xml", parseable: true, source: "result" }] },
    }),
  );
  await page.route(/\/api\/blast\/jobs\/job-e2e(\?.*)?$/, (route) => {
    if (route.request().method() === "DELETE") {
      state.jobDeletes.push(route.request().url());
      return jsonResponse(route, { job_id: "job-e2e", status: "deleted" });
    }
    return jsonResponse(route, completedJob);
  });

  await page.route("**/api/aks/stop", async (route) => {
    state.aksActions.push({ action: "stop", payload: route.request().postDataJSON() as Record<string, unknown> });
    await jsonResponse(route, { cluster_name: e2eCluster.name, task_id: "task-stop-e2e", status: "queued" });
  });
  await page.route("**/api/aks/start", async (route) => {
    state.aksActions.push({ action: "start", payload: route.request().postDataJSON() as Record<string, unknown> });
    await jsonResponse(route, { cluster_name: e2eCluster.name, task_id: "task-start-e2e", status: "queued" });
  });
  await page.route("**/api/aks/delete", async (route) => {
    state.aksActions.push({ action: "delete", payload: route.request().postDataJSON() as Record<string, unknown> });
    await jsonResponse(route, { cluster_name: e2eCluster.name, task_id: "task-delete-e2e", status: "queued" });
  });

  // After a queued AKS/start/stop/delete action the SPA polls the Celery task
  // status endpoint until the task is ready. Without this stub the poll falls
  // through to the real backend (cross-origin in dev), so mock a terminal
  // SUCCESS so the destructive-action flows resolve deterministically.
  await page.route(/\/api\/tasks\/[^/?]+(\?.*)?$/, (route) => {
    const taskId = decodeURIComponent(
      new URL(route.request().url()).pathname.split("/").pop() ?? "",
    );
    return jsonResponse(route, {
      task_id: taskId,
      status: "SUCCESS",
      ready: true,
      result: { ok: true },
    });
  });

  await page.route("**/api/storage/prepare-db", async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    state.dbDownloads.push(payload);
    await jsonResponse(route, { ok: true, db_name: payload.db_name, async: true, output: "queued" });
  });
  await page.route(/\/api\/storage\/prepare-db\/[^/]+\/cancel$/, async (route) => {
    state.dbCancels.push(route.request().url());
    await jsonResponse(route, { ok: true, db_name: "core_nt", aborted: 1, skipped: 0, errors: 0 });
  });

  await page.route("**/api/aks/openapi/spec?**", (route) =>
    jsonResponse(route, {
      openapi: "3.0.3",
      info: { title: "E2E OpenAPI", version: "1.0.0", description: "E2E fixture OpenAPI." },
      tags: [{ name: "Jobs", description: "BLAST jobs" }],
      paths: {
        "/v1/jobs": {
          get: {
            tags: ["Jobs"],
            summary: "List jobs",
            parameters: [{ name: "limit", in: "query", schema: { type: "integer", default: 10 } }],
            responses: { "200": { description: "OK", content: { "application/json": { example: { jobs: [] } } } } },
          },
        },
      },
    }),
  );
  await page.route("**/api/aks/openapi/deployment?**", (route) =>
    jsonResponse(route, { configured: true, deployment_name: "elb-openapi", container_name: "openapi", namespace: "default", image: "elb-openapi:1.0.0", image_repository: "elb-openapi", image_tag: "1.0.0" }),
  );
  await page.route("**/api/aks/openapi/token?**", (route) =>
    jsonResponse(route, { configured: true, token: "e2e-token", masked_token: "e2e-****", header_name: "X-ELB-API-Token", env_name: "ELB_OPENAPI_TOKEN", source: "keyvault", updated_at: now }),
  );
  await page.route("**/api/aks/openapi/token", (route) =>
    jsonResponse(route, { configured: true, token: "e2e-token-regenerated", masked_token: "e2e-regenerated-****", header_name: "X-ELB-API-Token", env_name: "ELB_OPENAPI_TOKEN", source: "keyvault", updated_at: now, rotated: true }),
  );
  await page.route("**/api/aks/openapi/proxy?**", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ jobs: ["job-e2e"] }) }),
  );

  await page.route("**/api/acr/build-images", async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    state.buildRequests.push(payload);
    await jsonResponse(route, {
      results: [{ image: "elb-openapi:1.0.0", status: "scheduled", run_id: "run-e2e" }],
    });
  });

  await page.route("**/api/blast/cost-estimate", (route) =>
    jsonResponse(route, {
      estimate: { compute_usd: 12.34, disk_usd: 1.23, storage_usd: 0.45, total_usd: 14.02 },
      params: {},
      note: "e2e fixture",
    }),
  );
  await page.route("**/api/blast/preprocess", (route) =>
    jsonResponse(route, {
      fasta_output: ">read1\nACGTACGT\n",
      detected_format: "fasta",
      stats: {
        input_sequences: 1,
        output_sequences: 1,
        total_bases: 8,
        filtered_short: 0,
        filtered_quality: 0,
        avg_length: 8,
        min_len: 8,
        max_len: 8,
        gc_content: 50,
      },
    }),
  );
  await page.route("**/api/blast/taxonomy", (route) =>
    jsonResponse(route, {
      annotations: {
        NR_123456: {
          accession: "NR_123456",
          title: "16S ribosomal RNA",
          organism: "Escherichia coli",
          taxid: "562",
          seq_length: "1542",
        },
      },
      found: 1,
      requested: 1,
    }),
  );
  await page.route("**/api/blast/taxonomy/detail/562", (route) =>
    jsonResponse(route, {
      taxid: 562,
      scientific_name: "Escherichia coli",
      rank: "species",
      division: "Bacteria",
      lineage: "Bacteria; Proteobacteria; Gammaproteobacteria",
      lineage_ex: [
        { taxid: 2, scientific_name: "Bacteria", rank: "superkingdom" },
        { taxid: 562, scientific_name: "Escherichia coli", rank: "species" },
      ],
      synonyms: ["E. coli"],
      equivalent_names: [],
    }),
  );
  await page.route("**/api/blast/taxonomy/image?**", (route) =>
    jsonResponse(route, { image_url: null, page_url: null, source: "e2e" }),
  );
  await page.route("**/api/blast/taxonomy/tree/562?**", (route) =>
    jsonResponse(route, {
      taxid: 562,
      lineage: [{ taxid: 2, scientific_name: "Bacteria", rank: "superkingdom" }],
      children: [],
      siblings: { species: [{ taxid: 562, scientific_name: "Escherichia coli", rank: "species" }] },
    }),
  );
  await page.route("**/api/blast/schedules", (route) =>
    jsonResponse(route, {
      schedules: [
        {
          schedule_id: "schedule-e2e",
          name: "Daily 16S smoke",
          trigger_type: "manual",
          blast_params: {},
          enabled: true,
          created_at: now,
          last_run: null,
          run_count: 0,
        },
      ],
    }),
  );
  await page.route(/\/api\/blast\/schedules\/([^/]+)\/run$/, async (route) => {
    state.scheduleRuns.push(route.request().url());
    await jsonResponse(route, { job_id: "job-scheduled-e2e", instance_id: "task-e2e", schedule_id: "schedule-e2e" });
  });
  await page.route(/\/api\/blast\/schedules\/([^/]+)$/, async (route) => {
    if (route.request().method() === "DELETE") {
      state.scheduleDeletes.push(route.request().url());
      await jsonResponse(route, { status: "deleted", schedule_id: "schedule-e2e" });
      return;
    }
    await route.fallback();
  });
  await page.route("**/api/blast/databases/versions?**", (route) =>
    jsonResponse(route, {
      versions: [
        { db_name: "core_nt", db_type: "nucl", source: "ncbi", source_version: "2026-05-20", created_at: now },
      ],
      total: 1,
    }),
  );
  await page.route("**/api/audit/log?**", (route) =>
    jsonResponse(route, {
      events: [{ timestamp: now, action: "blast_submit", user: "e2e@example.test", job_id: "job-e2e", details: {} }],
      total: 1,
    }),
  );
  await page.route("**/api/blast/primer-design", (route) =>
    jsonResponse(route, {
      primers: [
        {
          pair_index: 0,
          left_sequence: "ACGTACGTACGT",
          right_sequence: "TGCATGCATGCA",
          left_tm: 60.1,
          right_tm: 60.2,
          left_gc: 50,
          right_gc: 50,
          product_size: 180,
          pair_penalty: 0.2,
        },
      ],
      target: { start: 100, length: 200 },
      product_size_range: "100-1000",
      sequence_length: 600,
    }),
  );

  return state;
}