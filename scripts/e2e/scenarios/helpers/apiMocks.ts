import type { Page, Route } from "@playwright/test";

import { workspaceConfig } from "./workspace";

export const mockCluster = {
  name: "aks-e2e",
  resource_group: workspaceConfig.workloadResourceGroup,
  region: workspaceConfig.region,
  k8s_version: "1.34.0",
  provisioning_state: "Succeeded",
  power_state: "Running",
  node_count: 3,
  node_sku: "Standard_E32s_v5",
  kubelet_object_id: "00000000-0000-0000-0000-000000000001",
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

function warmupPlan(totalBytes: number) {
  return {
    feasible: true,
    status: "ok",
    message: "E2E fixture warmup plan is feasible.",
    num_nodes: 3,
    machine_type: "Standard_E32s_v5",
    node_ram_gib: 256,
    safe_node_budget_gib: 192,
    db_total_bytes: totalBytes,
    db_gib: totalBytes / 1024 / 1024 / 1024,
    chosen_shards: 3,
    target_shards: 3,
    per_shard_gib: totalBytes / 3 / 1024 / 1024 / 1024,
    per_node_gib: totalBytes / 3 / 1024 / 1024 / 1024,
    shards_per_node: 1,
    recommendations: [],
  };
}

const mockDatabases = [
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
    shard_sets: [1, 3],
    sharded: true,
    warmup_plan: warmupPlan(268_435_456),
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
    warmup_plan: warmupPlan(18_874_368),
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
    warmup_plan: warmupPlan(314_572_800),
  },
];

export interface MockSubmitCapture {
  payloads: Array<Record<string, unknown>>;
}

async function jsonResponse(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

export async function installNewSearchApiMocks(page: Page): Promise<MockSubmitCapture> {
  const capture: MockSubmitCapture = { payloads: [] };

  await page.route("**/api/me", (route) =>
    jsonResponse(route, {
      oid: "e2e-user",
      name: "E2E User",
      roles: ["dashboard-user"],
    }),
  );
  await page.route("**/api/monitor/aks?**", (route) =>
    jsonResponse(route, { clusters: [mockCluster] }),
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
  await page.route("**/api/aks/recent-failed-provisions**", (route) =>
    jsonResponse(route, { jobs: [], degraded: false }),
  );
  await page.route("**/api/blast/databases?**", (route) =>
    jsonResponse(route, { databases: mockDatabases, public_access_disabled: true }),
  );
  await page.route("**/api/blast/pre-flight", async (route) => {
    await jsonResponse(route, {
      status: "ok",
      ready: true,
      decision: "would_accept",
      checks: [
        { id: "aks_cluster", status: "pass", title: "AKS Cluster", detail: "aks-e2e is running" },
        { id: "storage", status: "pass", title: "Storage Account", detail: "stelbe2e configured" },
        { id: "database", status: "pass", title: "BLAST Database", detail: "database selected" },
      ],
      critical_blockers: 0,
      summary: "All e2e fixture checks passed",
      admission: { decision: "would_accept", reason: "e2e_fixture", basis: "mock", snapshot_at: new Date().toISOString() },
    });
  });
  await page.route("**/api/blast/jobs", async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    capture.payloads.push(payload);
    await jsonResponse(route, {
      id: "job-e2e",
      job_id: "job-e2e",
      dashboard_job_id: "job-e2e",
      instance_id: "task-e2e",
      task_id: "task-e2e",
      status: "queued",
      admission: { decision: "accepted", reason: "e2e_fixture", basis: "mock", snapshot_at: new Date().toISOString() },
      operation: { operation_id: "task-e2e", operation_type: "blast.submit", state: "queued" },
      target: { resource_type: "blast_job", job_id: "job-e2e", job_id_kind: "dashboard" },
    });
  });
  await page.route("**/api/blast/jobs?**", (route) =>
    jsonResponse(route, {
      jobs: [
        {
          job_id: "job-e2e",
          job_title: "E2E fixture job",
          program: "blastn",
          db: "blast-db/core_nt/core_nt",
          status: "queued",
          phase: "queued",
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        },
      ],
    }),
  );
  await page.route("**/api/blast/jobs/job-e2e**", (route) =>
    jsonResponse(route, {
      job_id: "job-e2e",
      job_title: "E2E fixture job",
      program: "blastn",
      db: "blast-db/core_nt/core_nt",
      status: "queued",
      phase: "queued",
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    }),
  );
  await page.route("**/api/blast/logs/job-e2e/ticket", (route) =>
    jsonResponse(route, {
      ticket: "e2e-log-ticket",
      expires_at: new Date(Date.now() + 60_000).toISOString(),
    }),
  );
  await page.route("**/api/upgrade/status", (route) =>
    jsonResponse(route, { status: "idle", active: false }),
  );

  return capture;
}