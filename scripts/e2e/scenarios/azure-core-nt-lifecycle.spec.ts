import { expect, test, type APIRequestContext, type APIResponse } from "@playwright/test";

const apiUrl = process.env.E2E_API_URL ?? "http://127.0.0.1:8085";
const bearerToken = process.env.E2E_BEARER_TOKEN ?? "";
const allowLifecycle = process.env.E2E_ALLOW_AZURE_LIFECYCLE === "1";
const confirmedCosts = process.env.E2E_CONFIRM_AZURE_COSTS === "create-core-nt-shard-warmup-blast";
const stopAksDuringStoragePhase = process.env.E2E_STOP_AKS_DURING_STORAGE_PHASE !== "0";

const pollMs = numberEnv("E2E_LIFECYCLE_POLL_MS", 30_000);
const apiRequestTimeoutMs = numberEnv("E2E_API_REQUEST_TIMEOUT_MS", 45_000);
const provisionTimeoutMs = numberEnv("E2E_AKS_PROVISION_TIMEOUT_MS", 90 * 60_000);
const prepareTimeoutMs = numberEnv("E2E_CORE_NT_PREPARE_TIMEOUT_MS", 24 * 60 * 60_000);
const shardTimeoutMs = numberEnv("E2E_CORE_NT_SHARD_TIMEOUT_MS", 4 * 60 * 60_000);
const warmupTimeoutMs = numberEnv("E2E_CORE_NT_WARMUP_TIMEOUT_MS", 6 * 60 * 60_000);
const blastTimeoutMs = numberEnv("E2E_CORE_NT_BLAST_TIMEOUT_MS", 2 * 60 * 60_000);
const lifecycleTimeoutMs =
  provisionTimeoutMs + prepareTimeoutMs + shardTimeoutMs + warmupTimeoutMs + blastTimeoutMs + 10 * 60_000;

interface LifecycleConfig {
  subscriptionId: string;
  resourceGroup: string;
  region: string;
  clusterName: string;
  storageResourceGroup: string;
  storageAccount: string;
  acrResourceGroup: string;
  acrName: string;
  nodeSku: string;
  nodeCount: number;
  systemVmSize: string;
  systemNodeCount: number;
  shardingMode: "approximate" | "precise";
}

interface TaskStatusResponse {
  task_id: string;
  status: string;
  ready: boolean;
  result?: unknown;
  error?: string;
  progress?: Record<string, unknown>;
}

interface ClusterSummary {
  name: string;
  provisioning_state?: string | null;
  power_state?: string | null;
  region?: string | null;
  node_count?: number | null;
  node_sku?: string | null;
  agent_pools?: Array<{ mode?: string | null; count?: number | null; vm_size?: string | null }>;
}

interface BlastDatabaseRow {
  name: string;
  total_bytes?: number;
  total_letters?: number;
  web_blast_searchsp?: number;
  update_in_progress?: boolean;
  update_error?: string | null;
  source_version?: string | null;
  downloaded_at?: string | null;
  file_count?: number;
  copy_status?: { phase?: string; failed?: number; success?: number; total_files?: number };
  sharding_in_progress?: boolean;
  sharding_error?: string | null;
  sharded?: boolean;
  shard_sets?: number[];
}

interface WarmupDatabaseRow {
  name: string;
  status: string;
  nodes_ready: number;
  nodes_active: number;
  nodes_failed: number;
  total_jobs: number;
}

interface WarmupStatusResponse {
  databases?: WarmupDatabaseRow[];
}

interface BlastJobResponse {
  job_id?: string;
  id?: string;
  status?: string;
  phase?: string;
  error?: string;
}

test("provisions AKS, prepares core_nt, shards, warms, and runs BLAST", async ({ request }) => {
  test.setTimeout(lifecycleTimeoutMs);
  test.skip(
    !allowLifecycle || !confirmedCosts,
    "Set E2E_ALLOW_AZURE_LIFECYCLE=1 and E2E_CONFIRM_AZURE_COSTS=create-core-nt-shard-warmup-blast to run this costly Azure lifecycle scenario.",
  );

  const cfg = readConfig();

  await test.step("preflight local fullstack", async () => {
    await expectHealthy(request, "/api/health");
    await expectCeleryWorker(request);
    await expectTerminalExec(request);
  });

  const cluster = await test.step("ensure AKS cluster exists", async () =>
    ensureClusterExistsForStoragePhase(request, cfg),
  );

  if (stopAksDuringStoragePhase && !(await isCoreNtStorageReady(request, cfg, cluster))) {
    await test.step("stop AKS while Storage-only work runs", async () =>
      stopClusterForStoragePhase(request, cfg),
    );
  }

  const preparedDb = await test.step("download core_nt into workload Storage", async () =>
    ensurePreparedCoreNt(request, cfg, cluster),
  );

  const shardedDb = await test.step("build core_nt shard layouts", async () =>
    ensureShardedCoreNt(request, cfg, cluster, preparedDb),
  );

  const warmupCluster = await test.step("ensure AKS is running for warmup", async () =>
    ensureClusterReady(request, cfg),
  );

  await test.step("wait for AKS warmup API readiness", async () =>
    waitForWarmupStatusApiReady(request, cfg),
  );

  await test.step("warm core_nt on AKS", async () =>
    ensureWarmCoreNt(request, cfg, warmupCluster),
  );

  await test.step("submit and complete a sharded core_nt BLAST smoke", async () =>
    submitAndWaitForBlast(request, cfg, warmupCluster, shardedDb),
  );
});

function readConfig(): LifecycleConfig {
  const subscriptionId = requiredEnv("E2E_AZURE_SUBSCRIPTION_ID");
  const resourceGroup = requiredEnv("E2E_AZURE_RESOURCE_GROUP");
  const clusterName = requiredEnv("E2E_AKS_CLUSTER");
  const storageAccount = requiredEnv("E2E_STORAGE_ACCOUNT");
  const acrName = requiredEnv("E2E_ACR_NAME");
  const cfg = {
    subscriptionId,
    resourceGroup,
    region: process.env.E2E_AZURE_REGION ?? "koreacentral",
    clusterName,
    storageResourceGroup: process.env.E2E_STORAGE_RESOURCE_GROUP ?? resourceGroup,
    storageAccount,
    acrResourceGroup: process.env.E2E_ACR_RESOURCE_GROUP ?? resourceGroup,
    acrName,
    nodeSku: process.env.E2E_NODE_SKU ?? "Standard_E32as_v7",
    nodeCount: numberEnv("E2E_NODE_COUNT", 3),
    systemVmSize: process.env.E2E_SYSTEM_VM_SIZE ?? "Standard_D2as_v7",
    systemNodeCount: numberEnv("E2E_SYSTEM_NODE_COUNT", 1),
    shardingMode: (process.env.E2E_SHARDING_MODE ?? "approximate") as "approximate" | "precise",
  };
  expect(cfg.subscriptionId).toMatch(/^[0-9a-fA-F-]{36}$/);
  expect(cfg.storageAccount).toMatch(/^[a-z0-9]{3,24}$/);
  expect(["approximate", "precise"]).toContain(cfg.shardingMode);
  return cfg;
}

async function ensureClusterReady(request: APIRequestContext, cfg: LifecycleConfig) {
  const existing = await findCluster(request, cfg);
  if (existing && isClusterReady(existing)) return existing;

  if (existing) {
    const start = await postJson(request, "/api/aks/start", {
      subscription_id: cfg.subscriptionId,
      resource_group: cfg.resourceGroup,
      cluster_name: cfg.clusterName,
      acr_name: cfg.acrName,
      acr_resource_group: cfg.acrResourceGroup,
      storage_account: cfg.storageAccount,
      storage_resource_group: cfg.storageResourceGroup,
    });
    await expectOk(start, "AKS start");
    const body = await start.json();
    if (body.task_id) await waitForTask(request, body.task_id, provisionTimeoutMs, "AKS start");
  } else {
    const provision = await postJson(request, "/api/aks/provision", {
      subscription_id: cfg.subscriptionId,
      resource_group: cfg.resourceGroup,
      region: cfg.region,
      cluster_name: cfg.clusterName,
      node_sku: cfg.nodeSku,
      node_count: cfg.nodeCount,
      system_vm_size: cfg.systemVmSize,
      system_node_count: cfg.systemNodeCount,
      acr_resource_group: cfg.acrResourceGroup,
      acr_name: cfg.acrName,
      storage_resource_group: cfg.storageResourceGroup,
      storage_account: cfg.storageAccount,
    });
    await expectOk(provision, "AKS provision");
    const body = await provision.json();
    if (body.task_id || body.instance_id) {
      await waitForTask(request, body.task_id ?? body.instance_id, provisionTimeoutMs, "AKS provision");
    }
  }

  return poll("AKS cluster ready", provisionTimeoutMs, pollMs, async () => {
    const cluster = await findCluster(request, cfg);
    if (cluster && isClusterReady(cluster)) return cluster;
    return null;
  });
}

async function ensureClusterExistsForStoragePhase(
  request: APIRequestContext,
  cfg: LifecycleConfig,
) {
  const existing = await findCluster(request, cfg);
  if (existing) return existing;
  return ensureClusterReady(request, cfg);
}

async function stopClusterForStoragePhase(request: APIRequestContext, cfg: LifecycleConfig) {
  const cluster = await findCluster(request, cfg);
  if (!cluster || cluster.power_state !== "Running") return null;
  const response = await postJson(request, "/api/aks/stop", {
    subscription_id: cfg.subscriptionId,
    resource_group: cfg.resourceGroup,
    cluster_name: cfg.clusterName,
  });
  await expectOk(response, "AKS stop for Storage phase");
  const body = await response.json();
  if (body.task_id) await waitForTask(request, body.task_id, provisionTimeoutMs, "AKS stop");
  return poll("AKS cluster stopped", provisionTimeoutMs, pollMs, async () => {
    const current = await findCluster(request, cfg);
    if (current?.power_state === "Stopped") return current;
    return null;
  });
}

async function isCoreNtStorageReady(
  request: APIRequestContext,
  cfg: LifecycleConfig,
  cluster: ClusterSummary,
) {
  const row = await findDatabase(request, cfg, cluster, "core_nt");
  return Boolean(row && isDatabasePrepared(row) && isDatabaseSharded(row));
}

async function ensurePreparedCoreNt(
  request: APIRequestContext,
  cfg: LifecycleConfig,
  cluster: ClusterSummary,
) {
  const existing = await findDatabase(request, cfg, cluster, "core_nt");
  if (existing && isDatabasePrepared(existing)) return existing;

  const response = await postJson(request, "/api/storage/prepare-db", {
    subscription_id: cfg.subscriptionId,
    storage_resource_group: cfg.storageResourceGroup,
    account_name: cfg.storageAccount,
    db_name: "core_nt",
  });
  if (response.status() !== 409) await expectOk(response, "core_nt prepare-db");

  return poll("core_nt prepared", prepareTimeoutMs, Math.max(pollMs, 60_000), async () => {
    const row = await findDatabase(request, cfg, cluster, "core_nt");
    if (!row) return null;
    if (row.update_error) throw new Error(`prepare-db failed: ${row.update_error}`);
    if (row.copy_status?.phase === "partial" || row.copy_status?.phase === "init_failed") {
      throw new Error(`prepare-db ended in ${row.copy_status.phase}`);
    }
    return isDatabasePrepared(row) ? row : null;
  });
}

async function ensureShardedCoreNt(
  request: APIRequestContext,
  cfg: LifecycleConfig,
  cluster: ClusterSummary,
  preparedDb: BlastDatabaseRow,
) {
  if (isDatabaseSharded(preparedDb)) return preparedDb;

  const response = await postJson(request, "/api/blast/databases/core_nt/shard", {
    subscription_id: cfg.subscriptionId,
    resource_group: cfg.storageResourceGroup,
    account_name: cfg.storageAccount,
  });
  if (response.status() !== 409) await expectOk(response, "core_nt shard");

  return poll("core_nt sharded", shardTimeoutMs, Math.max(pollMs, 60_000), async () => {
    const row = await findDatabase(request, cfg, cluster, "core_nt");
    if (!row) return null;
    if (row.sharding_error) throw new Error(`sharding failed: ${row.sharding_error}`);
    return isDatabaseSharded(row) ? row : null;
  });
}

async function ensureWarmCoreNt(
  request: APIRequestContext,
  cfg: LifecycleConfig,
  cluster: ClusterSummary,
) {
  const alreadyWarm = await findWarmDb(request, cfg, "core_nt");
  if (alreadyWarm && alreadyWarm.nodes_ready >= Math.max(1, cfg.nodeCount)) return alreadyWarm;
  if (alreadyWarm && ["Failed", "Blocked", "Stale"].includes(alreadyWarm.status) && alreadyWarm.nodes_active === 0) {
    await releaseWarmCoreNt(request, cfg);
  }
  const warmupAlreadyRunning =
    alreadyWarm &&
    ["Loading", "Partial", "Pressure"].includes(alreadyWarm.status) &&
    alreadyWarm.nodes_active > 0;

  if (!warmupAlreadyRunning) {
    const response = await postJson(request, "/api/warmup/start", {
      subscription_id: cfg.subscriptionId,
      resource_group: cfg.resourceGroup,
      storage_account: cfg.storageAccount,
      storage_resource_group: cfg.storageResourceGroup,
      region: cfg.region,
      db: "core_nt",
      db_display_name: "core_nt",
      program: "blastn",
      aks_cluster_name: cfg.clusterName,
      machine_type: workloadSku(cluster, cfg),
      num_nodes: workloadNodeCount(cluster, cfg),
      acr_resource_group: cfg.acrResourceGroup,
      acr_name: cfg.acrName,
    });
    await expectOk(response, "core_nt warmup start");
    const body = await response.json();
    if (body.task_id || body.instance_id) {
      await waitForWarmupTask(request, body.task_id ?? body.instance_id, warmupTimeoutMs);
    }
  }

  return poll("core_nt warm on cluster", warmupTimeoutMs, pollMs, async () => {
    const row = await findWarmDb(request, cfg, "core_nt");
    if (!row) return null;
    if (row.status === "Failed" || row.status === "Blocked") {
      throw new Error(`warmup failed with status ${row.status}`);
    }
    if (row.nodes_failed > 0) throw new Error(`warmup failed on ${row.nodes_failed} node(s)`);
    return row.status === "Ready" && row.nodes_ready >= Math.max(1, cfg.nodeCount) ? row : null;
  });
}

async function releaseWarmCoreNt(request: APIRequestContext, cfg: LifecycleConfig) {
  const response = await postJson(request, "/api/warmup/release", {
    subscription_id: cfg.subscriptionId,
    resource_group: cfg.resourceGroup,
    aks_cluster_name: cfg.clusterName,
    database_name: "core_nt",
  });
  await expectOk(response, "stale core_nt warmup release");
}

async function submitAndWaitForBlast(
  request: APIRequestContext,
  cfg: LifecycleConfig,
  cluster: ClusterSummary,
  db: BlastDatabaseRow,
) {
  const query = ">e2e_core_nt_16s\nAGAGTTTGATCCTGGCTCAGATTGAACGCTGGCGGCAGGCCTAACACATGCAAGTCGAAC\n";
  const shardSets = db.shard_sets?.filter((value) => value > 1) ?? [];
  const payload = {
    subscription_id: cfg.subscriptionId,
    resource_group: cfg.resourceGroup,
    region: cfg.region,
    program: "blastn",
    db: "blast-db/core_nt/core_nt",
    query_data: query,
    job_title: `e2e-core-nt-lifecycle-${Date.now()}`,
    evalue: 0.05,
    max_target_seqs: 10,
    outfmt: 5,
    low_complexity_filter: true,
    enable_warmup: true,
    use_local_ssd: true,
    machine_type: workloadSku(cluster, cfg),
    num_nodes: workloadNodeCount(cluster, cfg),
    storage_account: cfg.storageAccount,
    aks_cluster_name: cfg.clusterName,
    acr_resource_group: cfg.acrResourceGroup,
    acr_name: cfg.acrName,
    db_auto_partition: true,
    sharding_mode: cfg.shardingMode,
    allow_approximate_sharding: cfg.shardingMode === "approximate" || undefined,
    disable_sharding: false,
    shard_sets: shardSets,
    db_total_bytes: db.total_bytes,
    db_total_letters: db.total_letters,
    db_effective_search_space: db.web_blast_searchsp,
  };

  const preflight = await postJson(request, "/api/blast/pre-flight", payload);
  await expectOk(preflight, "BLAST pre-flight");
  const preflightBody = await preflight.json();
  expect(preflightBody.admission?.decision ?? preflightBody.decision).not.toBe("would_reject");

  const submit = await postJson(request, "/api/blast/jobs", payload);
  await expectOk(submit, "BLAST submit");
  const body = await submit.json();
  const jobId = String(body.job_id || body.id || "");
  expect(jobId).toBeTruthy();

  const completed = await poll("BLAST job completed", blastTimeoutMs, pollMs, async () => {
    const job = await getJson<BlastJobResponse>(request, `/api/blast/jobs/${encodeURIComponent(jobId)}`);
    const status = String(job.status ?? "").toLowerCase();
    const phase = String(job.phase ?? "").toLowerCase();
    if ([status, phase].some((value) => ["failed", "error", "cancelled", "canceled"].includes(value))) {
      throw new Error(`BLAST job failed: ${job.error ?? (status || phase)}`);
    }
    if ([status, phase].some((value) => ["completed", "succeeded", "success"].includes(value))) {
      return job;
    }
    return null;
  });
  expect(completed.job_id || completed.id).toBeTruthy();
}

async function expectHealthy(request: APIRequestContext, path: string) {
  const response = await request.get(`${apiUrl}${path}`, { headers: authHeaders() });
  await expectOk(response, path);
}

async function expectCeleryWorker(request: APIRequestContext) {
  const body = await getJson<Record<string, unknown>>(request, "/api/health/celery");
  const workers = body.workers as { ping?: Record<string, unknown> | null } | undefined;
  expect(Object.keys(workers?.ping ?? {}).length, "no Celery workers responded to ping").toBeGreaterThan(0);
}

async function expectTerminalExec(request: APIRequestContext) {
  if (process.env.E2E_SKIP_TERMINAL_EXEC_CHECK === "1") return;
  const response = await request.get("http://127.0.0.1:7682/healthz");
  expect(response.status(), await response.text()).toBe(200);
}

async function findCluster(request: APIRequestContext, cfg: LifecycleConfig) {
  const body = await getJson<{ clusters?: ClusterSummary[] }>(
    request,
    `/api/monitor/aks?subscription_id=${encodeURIComponent(cfg.subscriptionId)}&resource_group=${encodeURIComponent(cfg.resourceGroup)}`,
  );
  return (body.clusters ?? []).find((cluster) => cluster.name === cfg.clusterName) ?? null;
}

async function findDatabase(
  request: APIRequestContext,
  cfg: LifecycleConfig,
  cluster: ClusterSummary,
  name: string,
) {
  const qs = new URLSearchParams({
    subscription_id: cfg.subscriptionId,
    storage_account: cfg.storageAccount,
    resource_group: cfg.storageResourceGroup,
    num_nodes: String(workloadNodeCount(cluster, cfg)),
    machine_type: workloadSku(cluster, cfg),
  });
  const body = await getJson<{ databases?: BlastDatabaseRow[] }>(request, `/api/blast/databases?${qs}`);
  return (body.databases ?? []).find((row) => row.name === name) ?? null;
}

async function findWarmDb(request: APIRequestContext, cfg: LifecycleConfig, name: string) {
  const qs = new URLSearchParams({
    subscription_id: cfg.subscriptionId,
    resource_group: cfg.resourceGroup,
    cluster_name: cfg.clusterName,
  });
  const body = await getJson<WarmupStatusResponse>(request, `/api/monitor/aks/warmup-status?${qs}`);
  return (body.databases ?? []).find((row) => row.name === name) ?? null;
}

async function waitForWarmupStatusApiReady(request: APIRequestContext, cfg: LifecycleConfig) {
  const qs = new URLSearchParams({
    subscription_id: cfg.subscriptionId,
    resource_group: cfg.resourceGroup,
    cluster_name: cfg.clusterName,
  });
  return poll("AKS warmup status API ready", 10 * 60_000, pollMs, async () => {
    try {
      const response = await request.get(`${apiUrl}/api/monitor/aks/warmup-status?${qs}`, {
        headers: authHeaders(),
        timeout: apiRequestTimeoutMs,
      });
      return response.status() < 500 ? true : null;
    } catch {
      return null;
    }
  });
}

async function waitForTask(
  request: APIRequestContext,
  taskId: string,
  timeoutMs: number,
  label: string,
) {
  return poll(label, timeoutMs, pollMs, async () => {
    const task = await getJson<TaskStatusResponse>(request, `/api/tasks/${encodeURIComponent(taskId)}`);
    if (task.status === "FAILURE" || task.status === "REVOKED") {
      throw new Error(`${label} failed: ${task.error ?? JSON.stringify(task.result ?? task.progress ?? {})}`);
    }
    return task.ready && task.status === "SUCCESS" ? task : null;
  });
}

async function waitForWarmupTask(request: APIRequestContext, taskId: string, timeoutMs: number) {
  return poll("warmup task", timeoutMs, pollMs, async () => {
    const body = await getJson<{ runtime_status?: string; output?: { status?: string; error?: string } }>(
      request,
      `/api/warmup/${encodeURIComponent(taskId)}/status`,
    );
    if (body.runtime_status === "Failed" || body.output?.status === "failed") {
      throw new Error(`warmup failed: ${body.output?.error ?? "unknown"}`);
    }
    return body.runtime_status === "Completed" ? body : null;
  });
}

async function getJson<T>(request: APIRequestContext, path: string): Promise<T> {
  const response = await request.get(`${apiUrl}${path}`, {
    headers: authHeaders(),
    timeout: apiRequestTimeoutMs,
  });
  await expectOk(response, path);
  return (await response.json()) as T;
}

async function postJson(request: APIRequestContext, path: string, data: unknown) {
  return request.post(`${apiUrl}${path}`, {
    data,
    headers: authHeaders(),
    timeout: apiRequestTimeoutMs,
  });
}

async function expectOk(response: APIResponse, label: string) {
  expect(response.status(), `${label}: ${await response.text()}`).toBeLessThan(300);
}

async function poll<T>(
  label: string,
  timeoutMs: number,
  intervalMs: number,
  probe: () => Promise<T | null>,
): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  let attempts = 0;
  let lastError: unknown = null;
  while (Date.now() < deadline) {
    attempts += 1;
    try {
      const value = await probe();
      if (value) return value;
    } catch (error) {
      lastError = error;
      throw error;
    }
    if (attempts % 10 === 0) console.log(`${label}: still waiting (${attempts} probes)`);
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error(`${label} timed out after ${Math.round(timeoutMs / 1000)}s${lastError ? `; last error: ${String(lastError)}` : ""}`);
}

function isClusterReady(cluster: ClusterSummary) {
  return cluster.power_state === "Running" && cluster.provisioning_state === "Succeeded";
}

function isDatabasePrepared(row: BlastDatabaseRow) {
  if (row.update_in_progress) return false;
  if (row.copy_status?.phase === "completed") return true;
  return Boolean(row.source_version && (row.file_count ?? 0) > 0 && (row.total_bytes ?? 0) > 0);
}

function isDatabaseSharded(row: BlastDatabaseRow) {
  return Boolean(!row.sharding_in_progress && row.sharded && row.shard_sets?.some((value) => value > 1));
}

function workloadNodeCount(cluster: ClusterSummary, cfg: LifecycleConfig) {
  const userPool = cluster.agent_pools?.find((pool) => pool.mode === "User") ?? cluster.agent_pools?.[0];
  return userPool?.count ?? cluster.node_count ?? cfg.nodeCount;
}

function workloadSku(cluster: ClusterSummary, cfg: LifecycleConfig) {
  const userPool = cluster.agent_pools?.find((pool) => pool.mode === "User") ?? cluster.agent_pools?.[0];
  return userPool?.vm_size ?? cluster.node_sku ?? cfg.nodeSku;
}

function authHeaders() {
  return bearerToken ? { Authorization: `Bearer ${bearerToken}` } : undefined;
}

function requiredEnv(name: string) {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required for azure-core-nt-lifecycle`);
  return value;
}

function numberEnv(name: string, fallback: number) {
  const raw = process.env[name];
  if (!raw) return fallback;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed <= 0) throw new Error(`${name} must be a positive integer`);
  return parsed;
}