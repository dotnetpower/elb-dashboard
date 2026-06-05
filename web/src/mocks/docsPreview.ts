const DOCS_MOCK_PREVIEW = import.meta.env.VITE_DOCS_MOCK_PREVIEW === "true";

const workspaceConfig = {
  subscriptionId: "00000000-0000-0000-0000-000000000000",
  workloadResourceGroup: "rg-elb-dashboard-demo",
  acrResourceGroup: "rg-elb-acr-demo",
  acrName: "elbdemoacr",
  storageAccountName: "stelbdemodata",
  terminalResourceGroup: "rg-elb-terminal-demo",
  terminalVmName: "retired-terminal-vm",
  region: "koreacentral",
};

const startedAt = Date.now();
const dashboardJobId = "bb61858a-8cb6-4590-a2e3-c144662851f7";
const runningJobId = "423ae3e4-bbb8-4fa9-9104-e92741800d5d";
const failedJobId = "2d103011-8b19-4db9-9b8a-fcab676d15ba";
const openApiJobId = "17dfd2825089";

function iso(offsetMinutes: number): string {
  return new Date(startedAt + offsetMinutes * 60_000).toISOString();
}

function scenario(): string {
  if (typeof window === "undefined") return "ready";
  const search = new URLSearchParams(window.location.search);
  if (search.get("scenario")) return search.get("scenario") || "ready";
  const hashQuery = window.location.hash.split("?")[1] || "";
  return new URLSearchParams(hashQuery).get("scenario") || "ready";
}

function seedLocalState(): void {
  if (typeof window === "undefined") return;
  if (scenario() === "first-run") {
    window.localStorage.removeItem("elb-resource-config");
    window.localStorage.removeItem("elb-getting-started-dismissed");
    return;
  }
  window.localStorage.setItem("elb-resource-config", JSON.stringify(workspaceConfig));
  window.localStorage.setItem("elb-getting-started-dismissed", "1");
}

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  const headers = new Headers(init.headers);
  if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    statusText: init.statusText,
    headers,
  });
}

function textResponse(body: string, init: ResponseInit = {}): Response {
  const headers = new Headers(init.headers);
  if (!headers.has("Content-Type"))
    headers.set("Content-Type", "text/plain;charset=utf-8");
  return new Response(body, { status: init.status ?? 200, headers });
}

function clusterPayload() {
  const stopped = scenario() === "cluster-stopped";
  return {
    clusters: [
      {
        name: "elb-cluster",
        resource_group: workspaceConfig.workloadResourceGroup,
        region: workspaceConfig.region,
        k8s_version: "1.34.0",
        provisioning_state: "Succeeded",
        power_state: stopped ? "Stopped" : "Running",
        node_count: stopped ? 0 : 4,
        node_sku: "Standard_E16s_v5",
        kubelet_object_id: "00000000-0000-0000-0000-000000000001",
        network_plugin: "azure",
        fqdn: "elb-cluster-demo.hcp.koreacentral.azmk8s.io",
        agent_pools: [
          {
            name: "systempool",
            vm_size: "Standard_D2s_v3",
            count: 1,
            min_count: 1,
            max_count: 1,
            os_type: "Linux",
            mode: "System",
            power_state: stopped ? "Stopped" : "Running",
            enable_auto_scaling: false,
          },
          {
            name: "blastpool",
            vm_size: "Standard_E16s_v5",
            count: stopped ? 0 : 3,
            min_count: 0,
            max_count: 6,
            os_type: "Linux",
            mode: "User",
            power_state: stopped ? "Stopped" : "Running",
            enable_auto_scaling: true,
          },
        ],
      },
    ],
  };
}

function acrPayload() {
  return {
    name: workspaceConfig.acrName,
    login_server: `${workspaceConfig.acrName}.azurecr.io`,
    sku: "Premium",
    expected_image_tags: {
      "elb-api": "2026.05.21",
      "elb-worker": "2026.05.21",
      "elb-openapi": "4.14",
      "elb-terminal": "2026.05.21",
    },
    actual_tags: {
      "elb-api": ["2026.05.21"],
      "elb-worker": ["2026.05.21"],
      "elb-openapi": ["4.14"],
      "elb-terminal": ["2026.05.21"],
    },
    building_images: [],
    build_details: [],
  };
}

function storagePayload() {
  return {
    name: workspaceConfig.storageAccountName,
    region: workspaceConfig.region,
    sku: "Standard_LRS",
    kind: "StorageV2",
    public_network_access: "Disabled",
    is_hns_enabled: true,
    containers: [
      {
        name: "blast-db",
        public_access: null,
        last_modified_time: iso(-1440),
        blob_count: 8,
        size_bytes: 13958643712,
        usage_truncated: false,
        usage_error: null,
      },
      {
        name: "queries",
        public_access: null,
        last_modified_time: iso(-42),
        blob_count: 3,
        size_bytes: 92712,
        usage_truncated: false,
        usage_error: null,
      },
      {
        name: "results",
        public_access: null,
        last_modified_time: iso(-5),
        blob_count: 11,
        size_bytes: 5483312,
        usage_truncated: false,
        usage_error: null,
      },
      {
        name: "audit",
        public_access: null,
        last_modified_time: iso(-30),
        blob_count: 2,
        size_bytes: 8192,
        usage_truncated: false,
        usage_error: null,
      },
      {
        name: "job-artifacts",
        public_access: null,
        last_modified_time: iso(-30),
        blob_count: 4,
        size_bytes: 32144,
        usage_truncated: false,
        usage_error: null,
      },
    ],
  };
}

function warmupPayload() {
  const preparing = scenario() === "db-preparing";
  return {
    warm: !preparing,
    workspace_ready: preparing ? 2 : 4,
    workspace_desired: 4,
    vmtouch_ready: preparing ? 2 : 4,
    namespaces: ["default", "elastic-blast"],
    databases: [
      {
        name: "core_nt",
        mol_type: "nucl",
        status: preparing ? "Loading" : "Ready",
        nodes_ready: preparing ? 2 : 4,
        nodes_failed: 0,
        nodes_active: preparing ? 2 : 0,
        total_jobs: 4,
        progress_pct: preparing ? 58 : 100,
        active_phase: preparing ? "copying_files" : "completed",
        active_phase_label: preparing ? "Copying DB shards" : "Warm",
        active_message: preparing
          ? "Copying core_nt shards to node-local SSD"
          : "core_nt is resident on all warmup nodes",
        source_version: "2026-05-20-00-00-00",
        source_versions: ["2026-05-20-00-00-00"],
      },
    ],
  };
}

function databasesPayload() {
  return {
    databases: [
      {
        name: "core_nt",
        container: "blast-db",
        prefix: "core_nt/core_nt",
        source: "ncbi",
        file_count: 148,
        total_bytes: 48_120_000_000,
        total_letters: 32_156_241_807_668,
        total_sequences: 92_418_220,
        web_blast_searchsp: 32_156_241_807_668,
        web_blast_searchsp_scope: "database",
        last_modified: iso(-1440),
        source_version: "2026-05-20-00-00-00",
        downloaded_at: iso(-1430),
        sharded: true,
        shard_sets: [1, 2, 4, 8, 16],
        shard_source_version: "2026-05-20-00-00-00",
        shards_stale: false,
        warmup_plan: {
          feasible: true,
          status: "ok",
          message: "core_nt can be warmed across the selected blastpool nodes.",
          num_nodes: 4,
          machine_type: "Standard_E16s_v5",
          node_ram_gib: 128,
          safe_node_budget_gib: 96,
          db_total_bytes: 48_120_000_000,
          db_gib: 44.8,
          chosen_shards: 4,
          target_shards: 4,
          per_shard_gib: 11.2,
          per_node_gib: 11.2,
          shards_per_node: 1,
          recommendations: [],
          required_nodes: 4,
          available_nodes: 4,
          estimated_seconds: 520,
        },
      },
      {
        name: "16S_ribosomal_RNA",
        container: "blast-db",
        prefix: "16S_ribosomal_RNA/16S_ribosomal_RNA",
        source: "ncbi",
        file_count: 12,
        total_bytes: 840_000_000,
        total_letters: 42_000_000,
        total_sequences: 38_440,
        last_modified: iso(-2880),
        source_version: "2026-05-18-00-00-00",
        sharded: true,
        shard_sets: [1, 2, 4],
        warmup_plan: {
          feasible: true,
          status: "ok",
          message: "16S_ribosomal_RNA can be warmed across the selected blastpool nodes.",
          num_nodes: 4,
          machine_type: "Standard_E16s_v5",
          node_ram_gib: 128,
          safe_node_budget_gib: 96,
          db_total_bytes: 840_000_000,
          db_gib: 0.8,
          chosen_shards: 1,
          target_shards: 1,
          per_shard_gib: 0.8,
          per_node_gib: 0.8,
          shards_per_node: 1,
          recommendations: [],
        },
      },
    ],
    public_access_disabled: true,
  };
}

function baseJob(jobId: string, status: string) {
  const completed = status === "completed";
  return {
    job_id: jobId,
    job_id_kind: "dashboard",
    dashboard_job_id: jobId,
    openapi_job_id: openApiJobId,
    instance_id: `task-${jobId.slice(0, 8)}`,
    job_title: completed
      ? "core_nt monkeypox completed example"
      : "core_nt monkeypox smoke test",
    program: "blastn",
    db: "core_nt",
    status,
    phase: completed ? "completed" : status === "failed" ? "failed" : "running",
    created_at: completed ? iso(-160) : iso(-37),
    updated_at: completed ? iso(-142) : iso(-2),
    runtime_status: completed ? "Completed" : status === "failed" ? "Failed" : "Running",
    query_label: "NC_003310.1:c48509-48048",
    error:
      status === "failed" ? "Demo failure: AKS pod image pull timed out." : undefined,
    infrastructure: {
      subscription_id: workspaceConfig.subscriptionId,
      resource_group: workspaceConfig.workloadResourceGroup,
      region: workspaceConfig.region,
      storage_account: workspaceConfig.storageAccountName,
      acr_name: workspaceConfig.acrName,
      cluster_name: "elb-cluster",
    },
    database_metadata: {
      name: "core_nt",
      database: "core_nt",
      molecule_type: "nucl",
      source_version: "2026-05-20-00-00-00",
      number_of_sequences: 92_418_220,
      number_of_letters: 32_156_241_807_668,
    },
    custom_status: {
      phase: completed ? "completed" : status === "failed" ? "failed" : "running",
      duration_ms: completed ? 520_000 : 185_000,
      steps: {
        preparing: { phase: "completed", duration_ms: 14_200 },
        submitting: { phase: "completed", duration_ms: 31_400 },
        running: {
          phase: completed ? "completed" : status === "failed" ? "failed" : "running",
          duration_ms: completed ? 474_400 : 139_400,
        },
      },
    },
    payload: {
      subscription_id: workspaceConfig.subscriptionId,
      resource_group: workspaceConfig.workloadResourceGroup,
      storage_account: workspaceConfig.storageAccountName,
      aks_cluster_name: "elb-cluster",
      program: "blastn",
      db: "core_nt",
      outfmt: 5,
      resource_profile: "core_nt_safe",
    },
  };
}

function jobsPayload() {
  return {
    jobs: [
      baseJob(runningJobId, "running"),
      baseJob(dashboardJobId, "completed"),
      baseJob(failedJobId, "failed"),
    ],
  };
}

function jobById(id: string) {
  return (
    jobsPayload().jobs.find((job) => job.job_id === id) ||
    baseJob(dashboardJobId, "completed")
  );
}

function resultFilesPayload() {
  return {
    job_id: dashboardJobId,
    files: [
      {
        file_id: "result-001",
        name: "batch_001.xml.gz",
        size: 18_420,
        last_modified: iso(-141),
        format: "blast_xml",
        source: "storage",
      },
      {
        file_id: "merged-xml",
        name: "merged_results.out.gz",
        size: 22_104,
        last_modified: iso(-140),
        format: "blast_xml",
        source: "storage",
      },
    ],
    public_access_disabled: true,
    manifest: {
      schema_version: 1,
      job_id: dashboardJobId,
      status: "available",
      source: "storage",
      file_count: 2,
      parseable_count: 2,
      files: [
        {
          file_id: "result-001",
          name: "batch_001.xml.gz",
          size: 18_420,
          last_modified: iso(-141),
          format: "blast_xml",
          parseable: true,
        },
      ],
    },
  };
}

function openApiSpecPayload() {
  const ok = { responses: { "200": { description: "OK" } } };
  return {
    openapi: "3.1.0",
    info: { title: "ElasticBLAST on Azure", version: "3.3.0" },
    tags: [
      { name: "System", description: "Health checks and configuration" },
      { name: "Cluster", description: "AKS cluster status" },
      { name: "Jobs", description: "BLAST job submission, status, and results" },
      {
        name: "External ElasticBLAST",
        description: "Stable external integration facade",
      },
    ],
    paths: {
      "/v1/health": { get: { tags: ["System"], summary: "Detailed health", ...ok } },
      "/v1/config": {
        get: { tags: ["System"], summary: "Redacted active config", ...ok },
      },
      "/v1/cluster": {
        get: { tags: ["Cluster"], summary: "Get AKS runtime overview", ...ok },
      },
      "/v1/jobs": {
        get: { tags: ["Jobs"], summary: "List all jobs", ...ok },
        post: {
          tags: ["Jobs"],
          summary: "Submit a BLAST search",
          requestBody: {
            content: { "application/json": { schema: { type: "object" } } },
          },
          responses: { "202": { description: "Accepted" } },
        },
      },
      "/v1/jobs/{job_id}/status": {
        get: {
          tags: ["Jobs"],
          summary: "Get job status",
          parameters: [
            { name: "job_id", in: "path", required: true, schema: { type: "string" } },
          ],
          ...ok,
        },
      },
      "/v1/jobs/{job_id}/results": {
        get: {
          tags: ["Jobs"],
          summary: "Download results",
          parameters: [
            { name: "job_id", in: "path", required: true, schema: { type: "string" } },
            {
              name: "content",
              in: "query",
              required: false,
              schema: { type: "string", enum: ["full", "merged", "xml"] },
            },
          ],
          ...ok,
        },
      },
      "/api/v1/elastic-blast/submit": {
        post: {
          tags: ["External ElasticBLAST"],
          summary: "Submit an external ElasticBLAST job",
          responses: { "202": { description: "Accepted" } },
        },
      },
      "/api/v1/elastic-blast/jobs/{job_id}": {
        get: {
          tags: ["External ElasticBLAST"],
          summary: "Get external ElasticBLAST job status",
          parameters: [
            { name: "job_id", in: "path", required: true, schema: { type: "string" } },
          ],
          ...ok,
        },
      },
      "/api/v1/elastic-blast/jobs/{job_id}/files/{file_id}": {
        get: {
          tags: ["External ElasticBLAST"],
          summary: "Download an external ElasticBLAST result file",
          parameters: [
            { name: "job_id", in: "path", required: true, schema: { type: "string" } },
            { name: "file_id", in: "path", required: true, schema: { type: "string" } },
          ],
          ...ok,
        },
      },
    },
  };
}

function aggregatePayload() {
  return {
    job_id: dashboardJobId,
    status: "available",
    stats: {
      total_hits: 42,
      unique_queries: 1,
      unique_subjects: 6,
      evalue_distribution: { "0": 38, "1e-100": 4, "1e-20": 0, "1e-5": 0, ">1e-5": 0 },
      identity_distribution: { "100": 38, "99-95": 4, "94-90": 0, "89-80": 0, "<80": 0 },
      top_subjects: [{ id: "OZ254294.1", count: 12 }],
      total_queries: 1,
      avg_identity: 99.8,
      avg_bitscore: 812.6,
      avg_length: 461,
      max_bitscore: 828.419,
      min_evalue: 0,
      top_organisms: [{ organism: "Monkeypox virus", count: 42 }],
    },
    files_parsed: 2,
    total_files: 2,
    read_failures: 0,
  };
}

function sidecarsPayload() {
  const ts = Math.floor(startedAt / 1000);
  return {
    ts,
    revision: "docs-mock-preview",
    sidecars: {
      frontend: {
        name: "frontend",
        health: "ok",
        ts,
        cpu_pct: 4,
        mem_bytes: 58_000_000,
        mem_max_bytes: 268_435_456,
        mem_pct: 22,
      },
      api: {
        name: "api",
        health: "ok",
        ts,
        cpu_pct: 12,
        mem_bytes: 174_000_000,
        mem_max_bytes: 536_870_912,
        mem_pct: 32,
      },
      worker: {
        name: "worker",
        health: "ok",
        ts,
        cpu_pct: 18,
        mem_bytes: 238_000_000,
        mem_max_bytes: 536_870_912,
        mem_pct: 44,
      },
      beat: {
        name: "beat",
        health: "ok",
        ts,
        cpu_pct: 3,
        mem_bytes: 82_000_000,
        mem_max_bytes: 268_435_456,
        mem_pct: 30,
      },
      redis: {
        name: "redis",
        health: "ok",
        ts,
        cpu_pct: 2,
        mem_bytes: 42_000_000,
        mem_max_bytes: 268_435_456,
        mem_pct: 16,
        redis_version: "7.4",
      },
      terminal: {
        name: "terminal",
        health: "ok",
        ts,
        cpu_pct: 7,
        mem_bytes: 126_000_000,
        mem_max_bytes: 536_870_912,
        mem_pct: 23,
      },
    },
    events: { row1: 4, row2: 1, row3: 1, row4: 0 },
  };
}

function sidecarRequestsPayload() {
  const baseTs = Math.floor(startedAt / 1000);
  const jsonHeaders = [
    { name: "content-type", value: "application/json" },
    { name: "x-request-id", value: "docs-mock" },
  ];
  const authHeaders = [
    { name: "authorization", value: "Bearer ***REDACTED***" },
    { name: "x-ms-client-principal-name", value: "researcher.kim@example.org" },
  ];
  const items = [
    {
      ts: baseTs - 6,
      request_id: "req-docs-0007",
      method: "POST",
      path: "/api/blast/pre-flight",
      status: 200,
      duration_ms: 118,
      caller: "researcher.kim@example.org",
      client_ip: "10.42.0.14",
      request_headers: authHeaders,
      request_body: JSON.stringify(
        { db: "core_nt", program: "blastn", outfmt: 5, enable_warmup: true },
        null,
        2,
      ),
      request_body_truncated: false,
      response_headers: jsonHeaders,
      response_body: JSON.stringify(
        {
          ready: true,
          critical_blockers: 0,
          summary: "Mock preview accepts this search.",
        },
        null,
        2,
      ),
      response_body_truncated: false,
      response_size_bytes: 142,
    },
    {
      ts: baseTs - 18,
      request_id: "req-docs-0006",
      method: "GET",
      path: "/api/blast/databases?subscription_id=00000000-0000-0000-0000-000000000000&resource_group=rg-elb-dashboard-demo",
      status: 200,
      duration_ms: 1240,
      caller: "researcher.kim@example.org",
      client_ip: "10.42.0.14",
      request_headers: authHeaders,
      request_body: null,
      request_body_truncated: false,
      response_headers: jsonHeaders,
      response_body: JSON.stringify(
        { databases: ["core_nt", "16S_ribosomal_RNA"], public_access_disabled: true },
        null,
        2,
      ),
      response_body_truncated: false,
      response_size_bytes: 312,
    },
    {
      ts: baseTs - 32,
      request_id: "req-docs-0005",
      method: "GET",
      path: "/api/monitor/aks/warmup-status",
      status: 200,
      duration_ms: 84,
      caller: "dashboard",
      client_ip: "10.42.0.14",
      request_headers: authHeaders,
      request_body: null,
      request_body_truncated: false,
      response_headers: jsonHeaders,
      response_body: JSON.stringify(
        {
          warm: true,
          databases: [
            { name: "core_nt", status: "Ready", nodes_ready: 4, sources: ["warmup"] },
          ],
        },
        null,
        2,
      ),
      response_body_truncated: false,
      response_size_bytes: 284,
    },
    {
      ts: baseTs - 51,
      request_id: "req-docs-0004",
      method: "GET",
      path: "/api/monitor/storage",
      status: 200,
      duration_ms: 238,
      caller: "dashboard",
      client_ip: "10.42.0.14",
      request_headers: authHeaders,
      request_body: null,
      request_body_truncated: false,
      response_headers: jsonHeaders,
      response_body: JSON.stringify(
        {
          public_network_access: "Disabled",
          containers: ["blast-db", "queries", "results"],
        },
        null,
        2,
      ),
      response_body_truncated: false,
      response_size_bytes: 421,
    },
    {
      ts: baseTs - 76,
      request_id: "req-docs-0003",
      method: "GET",
      path: "/api/monitor/aks/nodes",
      status: 503,
      duration_ms: 2120,
      caller: "dashboard",
      client_ip: "10.42.0.14",
      request_headers: authHeaders,
      request_body: null,
      request_body_truncated: false,
      response_headers: jsonHeaders,
      response_body: JSON.stringify(
        {
          degraded: true,
          degraded_reason:
            "Kubernetes metrics API timed out; cached node status is still available.",
        },
        null,
        2,
      ),
      response_body_truncated: false,
      response_size_bytes: 198,
    },
    {
      ts: baseTs - 104,
      request_id: "req-docs-0002",
      method: "POST",
      path: "/api/blast/jobs",
      status: 202,
      duration_ms: 540,
      caller: "researcher.kim@example.org",
      client_ip: "10.42.0.14",
      request_headers: authHeaders,
      request_body: JSON.stringify(
        { program: "blastn", db: "core_nt", resource_profile: "core_nt_safe" },
        null,
        2,
      ),
      request_body_truncated: false,
      response_headers: jsonHeaders,
      response_body: JSON.stringify(
        { job_id: dashboardJobId, openapi_job_id: openApiJobId, status: "queued" },
        null,
        2,
      ),
      response_body_truncated: false,
      response_size_bytes: 238,
    },
    {
      ts: baseTs - 138,
      request_id: "req-docs-0001",
      method: "GET",
      path: "/api/terminal/health",
      status: 200,
      duration_ms: 36,
      caller: "dashboard",
      client_ip: "10.42.0.14",
      request_headers: authHeaders,
      request_body: null,
      request_body_truncated: false,
      response_headers: jsonHeaders,
      response_body: JSON.stringify({ status: "ok", upstream_status: 200 }, null, 2),
      response_body_truncated: false,
      response_size_bytes: 96,
    },
  ];
  return { items, count: items.length, capacity: 500 };
}

function alignmentsPayload() {
  return {
    job_id: dashboardJobId,
    blob_name: "merged_results.out.gz",
    blob_names: ["batch_001.xml.gz", "merged_results.out.gz"],
    alignments: [
      {
        qseqid: "NC_003310.1:c48509-48048",
        sseqid: "OZ254294.1",
        stitle:
          "Monkeypox virus isolate 24MPX2634V genome assembly, complete genome: monopartite",
        pident: 100,
        length: 462,
        mismatch: 0,
        gapopen: 0,
        qstart: 1,
        qend: 462,
        sstart: 48487,
        send: 48026,
        evalue: 0,
        bitscore: 828.419,
        qcovs: 100,
        sscinames: "Monkeypox virus",
        staxids: "10244",
      },
    ],
    total_hits: 42,
    returned: 1,
    query_ids: ["NC_003310.1:c48509-48048"],
    page: 1,
    page_size: 25,
    pages: 2,
    files_parsed: 2,
    total_files: 2,
    read_failures: 0,
  };
}

function requestMetricsPayload() {
  return {
    window_seconds: 900,
    now_ts: Math.floor(startedAt / 1000),
    path_prefix: null,
    total: 184,
    errors: 2,
    error_rate: 0.011,
    p50_ms: 82,
    p95_ms: 240,
    p99_ms: 390,
    rpm: Array.from({ length: 12 }, (_, index) => ({
      t_end: Math.floor((startedAt - (11 - index) * 60_000) / 1000),
      count: 7 + (index % 4),
    })),
    by_path: [
      { path: "/api/monitor/aks", count: 44, errors: 0, p95_ms: 130 },
      { path: "/api/blast/jobs", count: 31, errors: 1, p95_ms: 210 },
      { path: "/api/blast/databases", count: 22, errors: 0, p95_ms: 170 },
    ],
  };
}

function matchApi(path: string, method: string): Response | null {
  if (path === "/api/arm/subscriptions") {
    return jsonResponse([
      {
        subscriptionId: workspaceConfig.subscriptionId,
        displayName: "Docs mock subscription",
        state: "Enabled",
        tenantId: "mock-tenant",
      },
    ]);
  }
  if (/^\/api\/arm\/subscriptions\/[^/]+\/resource-groups$/.test(path)) {
    return jsonResponse(
      scenario() === "first-run"
        ? []
        : [
            {
              name: workspaceConfig.workloadResourceGroup,
              location: workspaceConfig.region,
              tags: {
                app: "elb-dashboard",
                "elb:acr-name": workspaceConfig.acrName,
                "elb:acr-rg": workspaceConfig.acrResourceGroup,
                "elb:storage-account": workspaceConfig.storageAccountName,
              },
            },
          ],
    );
  }
  if (path === "/api/arm/resource-group/tags") {
    return jsonResponse({
      resource_group: workspaceConfig.workloadResourceGroup,
      tags: { app: "elb-dashboard" },
    });
  }
  if (path === "/api/monitor/aks") return jsonResponse(clusterPayload());
  if (path === "/api/monitor/storage") return jsonResponse(storagePayload());
  if (path === "/api/monitor/acr") return jsonResponse(acrPayload());
  if (path === "/api/monitor/terminal") {
    return jsonResponse({
      name: "terminal-sidecar",
      region: workspaceConfig.region,
      vm_size: null,
      provisioning_state: "Retired",
      power_state: "Sidecar",
      os_disk_gb: null,
      public_ip: null,
      fqdn: null,
      has_managed_identity: true,
      identity_type: "UserAssigned",
    });
  }
  if (path === "/api/terminal/health")
    return jsonResponse({ status: "ok", upstream_status: 200 });
  if (path === "/api/monitor/aks/service-ip")
    return jsonResponse({ service_name: "elb-openapi", external_ip: "10.42.0.52" });
  if (path === "/api/aks/openapi/spec") return jsonResponse(openApiSpecPayload());
  if (path === "/api/aks/openapi/deployment") {
    return jsonResponse({
      configured: true,
      deployment_name: "elb-openapi",
      container_name: "openapi",
      namespace: "default",
      image: `${workspaceConfig.acrName}.azurecr.io/elb-openapi:4.14`,
      image_repository: "elb-openapi",
      image_tag: "4.14",
    });
  }
  if (path === "/api/aks/openapi/token") {
    return jsonResponse({
      configured: true,
      token: "",
      masked_token: "elb_************",
      header_name: "X-ELB-API-Token",
      env_name: "ELB_OPENAPI_API_TOKEN",
      source: "kubernetes_secret",
      updated_at: iso(-30),
    });
  }
  if (path === "/api/monitor/aks/warmup-status") return jsonResponse(warmupPayload());
  if (path === "/api/monitor/metrics") return jsonResponse(requestMetricsPayload());
  if (path === "/api/monitor/sidecars") return jsonResponse(sidecarsPayload());
  if (path === "/api/monitor/sidecars/ticket")
    return jsonResponse({ ticket: "docs-mock-ticket", expires_in: 60 });
  if (path === "/api/monitor/sidecar-requests")
    return jsonResponse(sidecarRequestsPayload());
  if (path === "/api/monitor/aks/events") {
    return jsonResponse({
      events: [
        {
          namespace: "elastic-blast",
          name: "blast-job-started",
          type: "Normal",
          reason: "Started",
          message: "Started BLAST shard 1/4",
          count: 1,
          last_timestamp: iso(-4),
          involved_kind: "Pod",
          involved_name: "blast-job-17dfd2825089-0",
          source_component: "kubelet",
          source_host: "aks-blastpool",
        },
      ],
    });
  }
  if (path === "/api/monitor/aks/nodes") return jsonResponse({ nodes: [] });
  if (path === "/api/monitor/aks/top-nodes") {
    return jsonResponse({
      nodes: [
        {
          name: "aks-blastpool-41800479-vmss00002p",
          cpu: "1820m",
          cpu_pct: 23,
          memory: "41Gi",
          memory_pct: 64,
          memory_total: "64Gi",
          mem_ki: 42991616,
          mem_capacity_ki: 67108864,
          cache_ki: 18874368,
          cache_pct: 28,
          pool: "blastpool",
          ready: true,
          conditions: { Ready: "True", MemoryPressure: "False" },
        },
      ],
    });
  }
  if (path === "/api/monitor/aks/pods") return jsonResponse({ pods: [] });
  if (path === "/api/storage/local-debug") {
    return jsonResponse({
      is_local: false,
      public_access: "Disabled",
      default_action: "Deny",
      ip_rules: [],
      caller_ip: null,
      caller_ip_in_rules: false,
    });
  }
  if (path === "/api/blast/databases") return jsonResponse(databasesPayload());
  if (path === "/api/blast/databases/check-updates")
    return jsonResponse({ latest_version: "2026-05-20-00-00-00" });
  if (path === "/api/blast/jobs" && method === "GET") return jsonResponse(jobsPayload());
  if (path === "/api/blast/jobs" && method === "POST") {
    return jsonResponse(
      {
        job_id: dashboardJobId,
        dashboard_job_id: dashboardJobId,
        openapi_job_id: openApiJobId,
        instance_id: "task-new-demo",
        status: "queued",
      },
      { status: 202 },
    );
  }
  const jobMatch = path.match(/^\/api\/blast\/jobs\/([^/]+)$/);
  if (jobMatch && method === "GET")
    return jsonResponse(jobById(decodeURIComponent(jobMatch[1])));
  if (path.match(/^\/api\/blast\/jobs\/[^/]+\/execution-steps$/)) {
    return jsonResponse({
      schema_version: 1,
      job_id: dashboardJobId,
      status: "completed",
      phase: "completed",
      artifact_state: "ready",
      custom_status: jobById(dashboardJobId).custom_status,
      output: { status: "completed" },
    });
  }
  if (path.match(/^\/api\/blast\/jobs\/[^/]+\/results$/))
    return jsonResponse(resultFilesPayload());
  if (path.match(/^\/api\/blast\/jobs\/[^/]+\/results\/aggregate$/))
    return jsonResponse(aggregatePayload());
  if (path.match(/^\/api\/blast\/jobs\/[^/]+\/results\/alignments$/))
    return jsonResponse(alignmentsPayload());
  if (path.match(/^\/api\/blast\/jobs\/[^/]+\/results\/taxonomy$/)) {
    return jsonResponse({
      job_id: dashboardJobId,
      organisms: [
        {
          key: "10244",
          organism: "Monkeypox virus",
          taxid: "10244",
          count: 42,
          best_evalue: 0,
          top_bitscore: 828.419,
        },
      ],
      total_hits: 42,
      files_parsed: 2,
      total_files: 2,
      read_failures: 0,
    });
  }
  if (path.match(/^\/api\/blast\/jobs\/[^/]+\/events$/))
    return jsonResponse({ job_id: dashboardJobId, events: [] });
  if (path.match(/^\/api\/blast\/jobs\/[^/]+\/file$/)) {
    return jsonResponse({
      name: "batch_001.xml",
      content: "<BlastOutput><BlastOutput_db>core_nt</BlastOutput_db></BlastOutput>",
      truncated: false,
    });
  }
  if (path === "/api/blast/pre-flight")
    return jsonResponse({
      status: "ok",
      ready: true,
      checks: [],
      critical_blockers: 0,
      summary: "Mock preview accepts this search.",
    });
  if (path === "/api/blast/taxonomy/search") {
    return jsonResponse({
      query: "monkeypox",
      count: 1,
      source: "ncbi_eutils",
      cached: true,
      results: [
        {
          taxid: 10244,
          scientific_name: "Monkeypox virus",
          common_name: null,
          rank: "species",
          lineage: "Viruses",
          matched_name: "Monkeypox virus",
          synonyms: [],
        },
      ],
    });
  }
  if (path.startsWith("/api/"))
    return jsonResponse({ mocked: true, path, method, items: [] });
  return null;
}

export function initDocsMockPreview(): void {
  if (!DOCS_MOCK_PREVIEW || typeof window === "undefined") return;
  seedLocalState();
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> => {
    const method = (
      init?.method || (input instanceof Request ? input.method : "GET")
    ).toUpperCase();
    const rawUrl =
      typeof input === "string" || input instanceof URL ? String(input) : input.url;
    const url = new URL(rawUrl, window.location.origin);
    const mocked = matchApi(url.pathname, method);
    if (mocked) return mocked;
    if (url.hostname === "api.example.internal" || url.hostname.startsWith("10.")) {
      return jsonResponse({ status: "ok", mocked: true, path: url.pathname });
    }
    try {
      return await originalFetch(input, init);
    } catch {
      return textResponse(`Mock preview has no fixture for ${method} ${url.pathname}`, {
        status: 404,
      });
    }
  };

  class MockEventSource extends EventTarget {
    static readonly CONNECTING = 0;
    static readonly OPEN = 1;
    static readonly CLOSED = 2;

    readonly url: string;
    readonly withCredentials = false;
    readyState = MockEventSource.CONNECTING;
    onopen: ((event: Event) => void) | null = null;
    onmessage: ((event: MessageEvent) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;

    constructor(url: string | URL) {
      super();
      this.url = String(url);
      window.setTimeout(() => {
        if (this.readyState === MockEventSource.CLOSED) return;
        this.readyState = MockEventSource.OPEN;
        const openEvent = new Event("open");
        this.onopen?.(openEvent);
        this.dispatchEvent(openEvent);
        this.emitSnapshot();
      }, 0);
    }

    close(): void {
      this.readyState = MockEventSource.CLOSED;
    }

    private emitSnapshot(): void {
      if (!this.url.includes("/api/monitor/sidecars/events")) return;
      const snapshot = new MessageEvent("snapshot", {
        data: JSON.stringify(sidecarsPayload()),
      });
      this.onmessage?.(snapshot);
      this.dispatchEvent(snapshot);
    }
  }

  window.EventSource = MockEventSource as unknown as typeof EventSource;
}
