# Container Apps Migration Plan

This document defines the target architecture and migration sequence for moving
the ElasticBLAST control plane from Azure Functions to Azure Container Apps.

The goal is not a lift-and-shift of the Function App runtime. The current API
has grown into a control plane that provisions Azure resources, tracks long
BLAST jobs, proxies AKS services, manages storage access windows, and coordinates
Remote Terminal operations. Container Apps should be used to split those
responsibilities into independently deployable services with private networking
and queue-backed workers.

## Decision Summary

Use this target shape:

- **Bundle the React SPA into the same Container App as a sixth sidecar**
  named `frontend` (nginx serving the built `dist/`). The Static Web App
  resource (`Microsoft.Web/staticSites`) goes away.
- Replace the Function App backend with **one Azure Container App that bundles
  six sidecar containers** in the same revision: `frontend` (nginx), `api`
  (FastAPI), `worker` (Celery), `beat` (Celery beat), `redis` (Redis 7
  alpine), and `terminal` (interactive shell with `elastic-blast` toolchain).
  All six share the same network namespace, so the worker reaches the broker
  at `127.0.0.1:6379`, the api proxies the browser terminal to
  `127.0.0.1:7681`, and the api reverse-proxies non-`/api/*` requests to the
  frontend at `127.0.0.1:8081`.
- Use **Celery beat** for scheduled work (BLAST schedules, DB refresh checks,
  periodic monitoring). No Container Apps Jobs and no Service Bus scheduled
  messages.
- **No managed database.** All durable state (job registry, audit log, schedule
  records, command history) is persisted to **Azure Storage** (blob and table)
  using managed-identity auth.
- **No separate Redis VM.** Redis runs as an in-revision sidecar with an Azure
  Files volume mounted at `/data` for AOF persistence so the broker survives
  revision restarts.
- **No separate Remote Terminal VM.** The browser-accessible operator shell is
  a sidecar in the same Container App. The api sidecar terminates the
  WebSocket from the browser (after MSAL + role check) and proxies it to a
  loopback `ttyd` instance inside the `terminal` sidecar. User home directory
  (`/home/azureuser`) is persisted on an Azure Files share.
- Move platform resources behind VNet integration and private endpoints.
- **Hard requirement, day 1: every Storage account in scope has
  `publicNetworkAccess: Disabled`. The Container App is the only client that
  can reach platform Storage, and it does so exclusively over private
  endpoints from inside the platform VNet.** No `bypass: AzureServices`
  workaround, no temporary public-window toggle for control-plane traffic.
- Use **one user-assigned managed identity** for the Container App. The six
  sidecars share it. The only other identity is `id-elb-openapi` for the AKS
  workload.

### Cost-minimisation choice

The control plane workload is low traffic and operator-driven. Splitting it
into separate Container Apps + a Redis VM + a Remote Terminal VM + a Static
Web App is over-provisioned. Bundling all processes into one Container App
with `minReplicas: 1`, `maxReplicas: 1` makes the whole stack one billable
unit at the smallest viable size (1.0 vCPU / 2 GiB total split across the six
sidecars; the terminal image carries the elastic-blast toolchain so it needs
the largest single allocation, frontend nginx needs almost nothing).
Trade-offs:

- The whole app restarts when any one container image changes. Acceptable
  because the API surface is small and the deploy pipeline is single-tenant.
- API and worker cannot scale horizontally because beat must be a singleton and
  Redis state must stay co-located. Acceptable for current and projected
  traffic; if scale-out is ever needed, split `beat` (and Redis) into a separate
  app first.
- In-flight Celery tasks are lost on revision restart. Mitigated by:
  - AOF on the Redis sidecar persisted to an Azure Files mount, so the queue is
    restored across restarts.
  - Storage state rows + the periodic reconciler (run by `beat`) re-dispatch
    tasks that were observed as `running` but whose worker disappeared.

Do not move the control plane into AKS as the first target. AKS is the workload
plane for ElasticBLAST. Hosting the control plane outside AKS keeps recovery,
upgrades, and cluster troubleshooting independent from the cluster being
managed.

### Explicitly removed from the prior plan

| Removed | Reason | Replacement |
|---------|--------|-------------|
| Azure Service Bus | Adds a managed dependency we no longer need once the worker model is Celery-based. | Celery + in-revision Redis sidecar. |
| Cosmos DB / Azure Database for PostgreSQL | A managed database is over-scoped for the document/append workloads this control plane has. Adds cost and operational surface. | Azure Storage (blob for documents, table for indexed queries). |
| Azure Cache for Redis (managed) | Cost. Broker is internal-only and does not need geo-replication, AAD, or managed patching. | Redis 7 alpine sidecar inside the Container App. |
| Self-hosted Redis VM (`vm-elb-redis`) | Adds a VM, NIC, NSG, subnet, MI, and nightly backup job. | Redis sidecar in the same Container App revision; AOF persisted to an Azure Files share. |
| Container Apps Jobs for scheduled work | Two scheduling systems (jobs + beat) is redundant. | Celery beat sidecar. |
| Separate `ca-control-api`, `ca-control-worker`, `ca-control-beat` apps | Three Container Apps means three billable revisions and three managed identities. | Single `ca-elb-control` Container App with six sidecars. |

## Resources to Create

Authoritative list for `infra/` planning. Use this table when sizing cost
estimates or writing new Bicep modules.

| Resource | Type | Purpose | New / Existing |
|----------|------|---------|----------------|
| Container Apps Environment | `Microsoft.App/managedEnvironments` | VNet-integrated runtime; binds the Azure Files volumes for Redis and the terminal home | New |
| `ca-elb-control` | `Microsoft.App/containerApps` | Single Container App with six sidecar containers: `frontend`, `api`, `worker`, `beat`, `redis`, `terminal`. Pinned to `minReplicas: 1`, `maxReplicas: 1`. Public ingress targets the `api` sidecar on `:8080`. | New |
| Azure Files share `redis-data` | `Microsoft.Storage/storageAccounts/fileServices/shares` | AOF persistence mount for the Redis sidecar | New (on existing platform storage account) |
| Azure Files share `terminal-home` | `Microsoft.Storage/storageAccounts/fileServices/shares` | `/home/azureuser` persistence for the terminal sidecar (queries staged locally, az CLI profile, kubeconfig, ssh known_hosts) | New (on existing platform storage account) |
| Platform Storage account | `Microsoft.Storage/storageAccounts` | Job state (table), audit (append blob), schedules (blob), command history (blob), Redis AOF (file share), terminal home (file share) | Re-purposed existing |
| Workload Storage account | `Microsoft.Storage/storageAccounts` | ElasticBLAST `blast-db`, `queries`, `results` | Existing |
| Container Registry | `Microsoft.ContainerRegistry/registries` | App + ElasticBLAST images (including the new `elb-frontend` and `elb-terminal` images) | Existing |
| Key Vault | `Microsoft.KeyVault/vaults` | Secrets, app configuration references | Existing |
| AKS cluster | `Microsoft.ContainerService/managedClusters` | ElasticBLAST workload | Existing |
| Platform VNet | `Microsoft.Network/virtualNetworks` | Subnets: `snet-containerapps`, `snet-private-endpoints`, `snet-aks` | New |
| Private endpoints | `Microsoft.Network/privateEndpoints` | Key Vault, Storage (blob + table + file), ACR | New |
| Private DNS zones | `Microsoft.Network/privateDnsZones` | `privatelink.vaultcore.azure.net`, `privatelink.blob.core.windows.net`, `privatelink.table.core.windows.net`, `privatelink.file.core.windows.net`, `privatelink.azurecr.io` | New |
| User-assigned managed identities | `Microsoft.ManagedIdentity/userAssignedIdentities` | `id-elb-control` (shared by all six sidecars), `id-elb-openapi` (AKS Workload Identity) | New |
| Log Analytics + Application Insights | `Microsoft.OperationalInsights/workspaces` + `Microsoft.Insights/components` | Logs, metrics, traces | Existing |

Not created: Azure Service Bus, Azure Cosmos DB, Azure Database for PostgreSQL,
Azure Cache for Redis, dedicated Redis VM, dedicated Redis subnet/NSG/MI,
Remote Terminal VM, terminal subnet, terminal NSG, terminal admin password
secret, terminal MI, Azure Bastion, **Azure Static Web Apps**.

## Why Move Away From Functions

The current Function App remains a good proof-of-concept host, but several
project responsibilities now fit a long-running service model better:

- Many HTTP routes poll Azure, AKS, ACR, Storage, Key Vault, and VM state.
- BLAST submit/delete/status flows can run for minutes to hours.
- Durable entities currently hold job registry, audit, and schedule state that
  should be queryable and portable outside the Functions runtime.
- Consumption-plan networking limits make private Storage and Key Vault access
  harder than the target security model requires.
- SSH and AKS proxy-style features are awkward in HTTP-triggered Functions.
- Packaging native Python dependencies into Functions has already required a
  custom deployment script.

Container Apps gives this project a better operational boundary: HTTP API,
workers, schedulers, and terminal gateway can scale and fail independently while
sharing one private Container Apps Environment.

## CPU and Memory Sizing

Container Apps allocates CPU and memory **per container, per replica**, and
the sum across containers in one replica must satisfy the platform's
constraints.

### Container Apps allocation rules (Consumption / Workload-profile Consumption)

- Minimum per container: **0.25 vCPU + 0.5 GiB**.
- Increments: **0.25 vCPU + 0.5 GiB**.
- The replica-total ratio must be **1 vCPU : 2 GiB**. (e.g. 0.5 vCPU → 1.0 GiB,
  2.25 vCPU → 4.5 GiB.)
- Max per replica on Consumption profile: **4 vCPU / 8 GiB**.
- Dedicated workload profiles (D4 / D8 / D16 / E-series) allow up to the
  profile's node capacity per replica and finer increments (down to 0.1 vCPU /
  0.1 GiB).

Reference: Microsoft Docs, "Containers in Azure Container Apps", under
"Allocations" (sums across all containers in a replica must respect the
ratio).

### Initial allocation per sidecar

Sized for the steady-state operator workload (low concurrency, occasional
BLAST submit / DB warmup). Revise after the first week of production
telemetry; resize is a revision swap with no downtime.

| Sidecar | vCPU | Memory | Sizing reasoning |
|---------|------|--------|------------------|
| `frontend` (nginx:alpine) | 0.25 | 0.5 GiB | Static files; a few QPS at most. The minimum allocation is already overkill. |
| `api` (FastAPI) | 0.5 | 1.0 GiB | Handles JSON requests, the WebSocket terminal proxy, and the streaming upload/download proxy (1 MiB chunks, 4 MiB block uploads, semaphore-capped to 4 concurrent transfers). 0.5 vCPU is sized for the proxy bursts; idle steady-state will be much lower. |
| `worker` (Celery) | 0.5 | 1.0 GiB | Runs Azure SDK pollers, ARM/AKS calls, and `az acr build` orchestration. CPU spikes during ACR build dispatch and AKS provision but is mostly waiting on long-running Azure operations. |
| `beat` (Celery beat) | 0.25 | 0.5 GiB | Scheduler thread + Storage poller for schedule definitions. Trivial. |
| `redis` (redis:7-alpine) | 0.25 | 0.5 GiB | Single-node broker for control-plane traffic. AOF write rate is one fsync/second. Memory grows with queue depth; 0.5 GiB is enough for hundreds of thousands of pending tasks. |
| `terminal` (Ubuntu + elastic-blast toolchain) | 0.5 | 1.0 GiB | Bash + tmux + `python` + occasional `kubectl`/`az`/`azcopy`. Carries the heaviest image, but at runtime it is mostly idle waiting for the operator to type. |
| **Replica total** | **2.25** | **4.5 GiB** | Satisfies the 1 vCPU : 2 GiB ratio. Within Consumption-profile per-replica max (4 / 8). |

If any sidecar regularly hits its CPU limit (visible in App Insights as
`Container CPU Usage Percent` saturating), bump that sidecar in 0.25 vCPU /
0.5 GiB increments and bump another sidecar down by the same amount, or grow
the replica total (still respecting the 1:2 ratio). The bundled topology has
no horizontal scale-out (`minReplicas: 1, maxReplicas: 1`); vertical resize
is the only knob.

### What if the bundle outgrows 4 vCPU / 8 GiB?

Two paths, in preference order:

1. **Move to a Workload-Profile Dedicated node** (D4 → 4 vCPU / 16 GiB, D8 → 8 /
   32, etc.). This raises the per-replica cap and lets the bundle keep its
   single-revision semantics.
2. **Split a hot sidecar into its own Container App** (likely candidates: the
   `api` for proxy load, then the `worker` for ARM throughput). This breaks
   the single-revision invariant but unlocks `maxReplicas > 1`.

Do **not** raise replica count on the bundled app; that would duplicate the
`beat` singleton and break Redis state locality.

## Cost Estimate (Korea Central, USD, monthly)

Numbers are based on Azure Retail Prices API for `koreacentral`, May 2026.
Verify in the [Azure Pricing Calculator](https://azure.microsoft.com/pricing/calculator/)
before publishing to stakeholders.

### Per-second meters used (confirmed)

| Meter | Unit price |
|-------|------------|
| Standard vCPU Active Usage | `$0.000024` / vCPU-second |
| Standard vCPU Idle Usage | `$0.000003` / vCPU-second |
| Standard Memory (active and idle, same price) | `$0.000003` / GiB-second |
| Standard Requests | `$0.40` per 1,000,000 requests |
| Dedicated Plan Management (workload-profile environment fee) | `$0.10` / hour ≈ `$72` / month |

Free monthly grant per subscription:
**180,000 vCPU-seconds** + **360,000 GiB-seconds** + **2,000,000 requests**.

### Always-on math for the bundled app (2.25 vCPU / 4.5 GiB)

Per month at 30 days:

- vCPU-seconds = 2.25 × 86,400 × 30 = **5,832,000**
- GiB-seconds  = 4.5  × 86,400 × 30 = **11,664,000**

After applying the free grant:

- Charged vCPU-seconds = 5,832,000 − 180,000 = **5,652,000**
- Charged GiB-seconds  = 11,664,000 − 360,000 = **11,304,000**

Three duty-cycle scenarios:

| Active fraction | vCPU cost | Memory cost | Total per-second meters |
|-----------------|-----------|-------------|-------------------------|
| 0% (always idle, hypothetical floor) | `5,652,000 × $0.000003` = **$16.96** | `11,304,000 × $0.000003` = **$33.91** | **~$50.87** |
| 5% (realistic operator workload) | `0.05 × 5,652,000 × $0.000024` + `0.95 × 5,652,000 × $0.000003` = `$6.78 + $16.11` = **$22.89** | **$33.91** | **~$56.80** |
| 100% (worst case, never happens for this workload) | `5,652,000 × $0.000024` = **$135.65** | **$33.91** | **~$169.56** |

Requests are negligible (a few thousand /day at most → free tier covers it).

### Two deployment options

**Option A — Workload-profiles plan with the Consumption profile (recommended).**
This is required to host the Container Apps Environment inside the platform
VNet with private endpoints to Storage and Key Vault. The plan adds the
"Dedicated Plan Management" fee even when the workloads run on the
Consumption profile (no dedicated node).

| Line item | Monthly | Notes |
|-----------|---------|-------|
| Per-second usage (5% active scenario) | ~$57 | from the table above |
| Workload-profile environment fee | $72 | $0.10/hour × 720 hours |
| Azure Files for `redis-data` (Standard LRS, ~5 GiB used) | ~$0.30 | $0.06/GiB/month |
| Azure Files for `terminal-home` (Standard LRS, ~20 GiB used) | ~$1.20 | same rate |
| Platform Storage (table + append blobs for state) | ~$1 | low transactions |
| **Container-Apps-side total** | **~$132 / month** | excludes ACR ($20, already paid) and workload Storage |

**Option B — Consumption-only plan (no workload-profile fee).**
Cheaper, but VNet integration support is more limited and does not cover the
day-1 private-storage requirement on every Azure region. Use only if you
verify in your subscription that Consumption-only environments can sit in
the platform VNet AND mount Azure Files privately AND reach Key Vault /
Storage private endpoints — otherwise you cannot satisfy the Storage Network
Isolation invariant.

| Line item | Monthly | Notes |
|-----------|---------|-------|
| Per-second usage (5% active scenario) | ~$57 | same math |
| Environment fee | $0 | Consumption-only has none |
| Azure Files | ~$1.50 | same |
| Platform Storage | ~$1 | same |
| **Container-Apps-side total** | **~$60 / month** | only if VNet + private endpoints actually work in this mode |

The plan defaults to **Option A** because the Storage Network Isolation rule
is non-negotiable.

### Comparison to today and to earlier revisions of this plan

| Topology | Monthly Azure cost (control-plane only, KR Central, 5% active) | Notes |
|----------|----------------------------------------------------------------|-------|
| Today: Function App Consumption + SWA Standard | ~$10 – $15 | SWA $9, Function App < $5 at current load. Storage public access required. |
| Earlier "split apps + Redis VM + Terminal VM + SWA + Service Bus + Cosmos" plan | **~$385** | 3 Container Apps (~$171), Standard_B2s Redis VM (~$30), Standard_D4s_v5 Terminal VM (~$140), Service Bus Standard (~$10), Cosmos serverless (~$25), SWA ($9). |
| Previous "5 sidecars + SWA" plan | **~$140** | One bundled Container App + SWA. |
| **Current "6 sidecars, no SWA"** | **~$132** | One bundled Container App. SWA folded in. |

The current bundled topology spends roughly **$120 / month more** than the
existing Function App + SWA setup, in exchange for:

- Day-1 `publicNetworkAccess: Disabled` on every Storage account.
- No SAS to the browser.
- No Remote Terminal VM, no SSH, no admin password.
- No Service Bus, no managed database, no managed Redis.
- One billable Azure resource for the entire control plane.

It is roughly **$253 / month cheaper** than the earlier multi-app + VM
revision of the migration plan.

### What can move the number

- **Active fraction.** Real operator usage tends to be < 1% active on average.
  At 0.5% active the per-second meters drop to ~$52, total ~$127 / month.
- **Resize.** Halving CPU on `api` and `worker` (to 0.25 each) brings the
  replica total to 1.75 vCPU / 3.5 GiB and trims ~$13 / month, but eats the
  proxy headroom. Wait for telemetry before resizing.
- **Use Front Door for the SPA** (optional, not in the day-1 plan). Adds
  ~$35 / month for Front Door Standard plus per-GB egress; gains a CDN.
- **Use Premium Files for `redis-data`** (e.g. for a future multi-node
  Redis). Premium has a 100 GiB minimum at $0.16/GiB → ~$16 / month per
  share, vs. the ~$0.30 Standard estimate above. Not needed for the
  single-broker design.

## Storage Network Isolation (Hard Requirement)

This is the most important non-functional requirement of the migration. Every
rule in the rest of this document is consistent with it.

### Rules

1. **Platform Storage account** (job state table, audit blobs, payload blobs,
   schedule blob, dead-letter blobs, `redis-data` Azure Files share):
   - `publicNetworkAccess` is `Disabled` from the moment the account is in
     production use.
   - `networkAcls.defaultAction` is `Deny`.
   - `networkAcls.bypass` is `None` (not `AzureServices`).
   - No IP allow-list entries.
   - Reachable only via three private endpoints in `snet-private-endpoints`:
     blob, table, and file. Each endpoint is wired into its private DNS zone
     and the zone is linked to the platform VNet.
2. **Workload Storage account** (ElasticBLAST `blast-db`, `queries`,
   `results`):
   - Same rules. `publicNetworkAccess: Disabled`, `defaultAction: Deny`,
     `bypass: None`.
   - Reachable via blob (and dfs, if HNS) private endpoints in
     `snet-private-endpoints`.
   - AKS nodes live in `snet-aks` in the same VNet, so they reach workload
     storage privately. The terminal sidecar reaches workload storage from
     `snet-containerapps` over the same private endpoint.
   - **The current temporary-public-access window code path is removed in this
     migration**, including the `auto-keep-enabled` toggle and any
     `bypass: AzureServices` workaround. Anything that currently depends on
     the public window must instead be reachable via private endpoint or be
     re-architected.
3. **Browser ↔ storage**: the SPA never talks to Storage directly. **All
   browser downloads and uploads are proxied by the api sidecar.** No SAS
   tokens (user delegation or otherwise) are ever issued to the browser. See
   the next section for the full proxy contract.

### Container Apps Environment requirements that make rule 1 enforceable

- The Container Apps Environment **must** be VNet-integrated. Use the
  workload-profile environment with an `infrastructureSubnetId` pointing at
  `snet-containerapps`.
- `internal: true` is recommended (the SPA reaches the API through Front Door
  or a SWA-linked backend). For phase 1, an external ingress is acceptable
  *if and only if* the egress path to Storage still goes through the VNet.
  Egress through the VNet is the property that lets Storage stay private,
  not the ingress mode.
- `snet-containerapps` is delegated to `Microsoft.App/environments` and sized
  per Microsoft guidance (`/27` for Consumption-only, `/23` for workload
  profile environments). Pick `/23` so the topology can grow without renaming.
- All private DNS zones (`privatelink.blob.core.windows.net`,
  `privatelink.table.core.windows.net`, `privatelink.file.core.windows.net`,
  `privatelink.vaultcore.azure.net`, `privatelink.azurecr.io`) are linked to
  the platform VNet so the Container App resolves storage hostnames to
  private IPs.
- The Container App's outbound DNS must be the Azure-provided 168.63.129.16
  (default for Container Apps). Do **not** override `dnsConfig` in a way that
  bypasses the linked private DNS zones.

### What this forbids

- No code path enables Storage public access “just for a moment.” The previous
  `auto-keep-enabled` storage-window orchestrator and the
  `bypass: AzureServices` shortcut both go away.
- **No SAS token of any kind is issued to the browser.** Not user delegation
  SAS, not account SAS, not service SAS. The api sidecar is the sole client
  the browser sees.
- No `kubectl` / `azcopy` step in the operator runbook that assumes the
  storage endpoint is publicly resolvable.

### Verification (must be part of CI / smoke tests)

- `az storage account show -n <plat> --query "{p:publicNetworkAccess, a:networkAcls.defaultAction, b:networkAcls.bypass, ips:networkAcls.ipRules}"`
  returns `Disabled / Deny / None / []` for both platform and workload accounts.
- From inside the Container App (`az containerapp exec ... -- nslookup
  <account>.blob.core.windows.net`), the resolved address is a `10.x.x.x`
  private IP.
- An external curl to `https://<account>.blob.core.windows.net/` returns
  `403 PublicAccessNotPermitted` (or DNS NXDOMAIN if the public record was
  removed for the account).
- The Redis sidecar successfully mounts the `redis-data` Azure Files share
  while the storage account `publicNetworkAccess` is `Disabled`.

## Browser ↔ Storage Proxy (No SAS to the Browser)

This is the contract that lets `publicNetworkAccess: Disabled` hold on day 1
without breaking the existing user workflows (uploading queries, downloading
results).

### Rules

- The api sidecar is the **only** Storage client the browser sees.
- All transfers are **streamed** in chunks. The api sidecar must never buffer a
  full blob in memory or to local disk.
- Authentication: every byte the browser sends or receives is on a request
  that carries a valid MSAL access token and passes the standard authorization
  check (caller is `owner_oid` of the job, or has the right tenant role).
- Authorization: the api sidecar resolves browser-supplied logical names
  (`job_id`, `result_filename`) to the concrete container/path internally.
  The browser never names a Storage account, container, or blob path
  directly.
- The api sidecar uses its managed identity + the private endpoint to talk to
  Storage. No SAS is ever generated, even server-side, for browser-facing
  flows.
- Concurrency: a per-replica semaphore caps simultaneous proxy transfers
  (initial: 4 concurrent transfers). Excess requests get `429 Too Many Requests`
  with `Retry-After`. This protects the api sidecar's modest CPU/memory
  budget inside the bundled Container App.

### Download contract (`GET /api/blast/jobs/{job_id}/results/{name}`)

Behaviour:

- Validate token + `owner_oid`; resolve `(job_id, name)` to a workload-storage
  blob path; refuse if the job's `status` is not in a terminal-success state.
- Open a streaming download from Storage with a small chunk size (1 MiB).
- Pass through `ETag`, `Content-Type`, `Content-Length`, and
  `Last-Modified` headers from the Storage response.
- Honor `Range` requests by passing the same `Range` header to Storage and
  returning `206 Partial Content` with the storage response's
  `Content-Range`. This is required to keep large result downloads resumable
  inside the Container Apps 240-second per-request timeout.
- For results larger than what fits inside one 240-second window at the
  user's link speed, the SPA must use range requests. The proxy advertises
  `Accept-Ranges: bytes` so browsers and `curl --range` work.
- Use Python `httpx` (or the Azure Storage SDK's streaming download) with
  `chunk_size=1 MiB` and async iteration so the FastAPI worker is not
  blocked.
- Never decompress on the proxy. Pass the Storage `Content-Encoding`
  through.

### Upload contract (`POST /api/blast/jobs/{job_id}/queries`)

Behaviour:

- Validate token + `owner_oid`; resolve `(job_id, filename)` to a
  workload-storage blob path inside the `queries` container; refuse if the
  job's `status` does not allow new uploads.
- Accept the request body as a stream (`request.stream()` in FastAPI), not
  via `multipart` form parsing into memory.
- Use the Azure Storage SDK's **block-blob staged upload**: call
  `stage_block` for each chunk (initial chunk size: 4 MiB) as it arrives,
  then `commit_block_list` once the request body ends. This caps proxy
  memory use at one chunk plus internal SDK overhead, regardless of total
  upload size.
- Set a per-blob upload size limit (initial: 256 MiB) at the API layer and
  reject larger requests with `413 Payload Too Large`. This keeps a single
  upload inside the 240-second Container Apps request timeout at a typical
  upload speed.
- For the rare case of larger uploads (NCBI database imports, multi-GB
  reference inputs): those are not browser-driven. The Celery worker
  performs them server-side over the private endpoint, with progress
  written to the Storage state row. The browser monitors progress via
  `GET /api/storage/jobs/{import_id}`.
- Do not generate a SAS. The browser PUT goes to the api sidecar; the api
  sidecar PUTs to Storage with managed identity.

### Why not user delegation SAS?

User delegation SAS would let the browser hit Storage directly and bypass
the proxy's CPU/memory cost. It does not work in this design because:

1. The Storage endpoint is unreachable from the public internet
   (`publicNetworkAccess: Disabled`). A SAS to `<account>.blob.core.windows.net`
   resolves to a private IP that the browser cannot route to.
2. Issuing a SAS to a public hostname (some bypass that re-exposes the
   account) violates rule 1 of Storage Network Isolation.
3. Removing SAS from the browser surface also removes a class of token-leak
   incidents (logs, browser history, screenshots, support tickets).

The trade-off is real: the api sidecar pays CPU and bandwidth for every
download. The bundled Container App has a single replica, so a sustained
many-user download workload would saturate it. This is acceptable for the
project's expected scale (operator-driven, low concurrency). If future scale
breaks the assumption, the escalation path is to split the api sidecar into
its own Container App with `maxReplicas` > 1, **not** to re-introduce SAS.

### Verification

- A test that uploads a 32 MiB random file via the proxy, downloads it back
  via the proxy, and verifies SHA-256 round-trip integrity.
- A test that the api sidecar's RSS does not exceed `chunk_size + small
  overhead` while a 256 MiB upload is in flight.
- A test that 5 concurrent downloads of a 64 MiB blob complete and that the
  6th request gets `429 Too Many Requests`.
- A test that a `Range: bytes=10485760-` request returns `206 Partial
  Content` with the correct `Content-Range`.
- A SAST/grep check in CI: any code path that calls
  `generate_blob_sas`, `generate_container_sas`, or
  `BlobClient.url` for a browser-bound response fails the build. The only
  permitted SAS use is internal (e.g. `WEBSITE_RUN_FROM_PACKAGE` for the
  legacy Function App during cutover); these call sites are explicitly
  allow-listed.

## Target Architecture

```text
Browser
  |
  | HTTPS (TLS terminated by Container Apps ingress)
  | + MSAL access token on /api/* and the WebSocket upgrade
  v
Container Apps Environment, VNet integrated
  |
  +-- ca-elb-control  (one Container App, one revision, one replica)
        |
        +-- container: api      (FastAPI, public ingress on :8080)
        |     - serves /api/* directly
        |     - reverse-proxies everything else to 127.0.0.1:8081 (frontend)
        |     - upgrades /api/terminal/ws to a duplex copy with 127.0.0.1:7681 (terminal)
        +-- container: frontend (nginx:alpine, listens on 127.0.0.1:8081)
        |     - serves the built /usr/share/nginx/html (Vite dist/)
        |     - SPA navigation fallback to /index.html for non-asset paths
        |     - immutable cache for /assets/*, no-cache for /index.html
        +-- container: worker   (Celery worker, no ingress)
        +-- container: beat     (Celery beat, no ingress)
        +-- container: redis    (redis:7-alpine, listens on 127.0.0.1:6379)
        |     |
        |     +-- volume mount: /data         -> Azure Files share `redis-data`
        |
        +-- container: terminal (ttyd + bash + elastic-blast toolchain,
        |                        listens on 127.0.0.1:7681)
              |
              +-- volume mount: /home/azureuser -> Azure Files share `terminal-home`

All six sidecars share:
  - the same network namespace
      - api reverse-proxies non-/api/* requests to frontend at 127.0.0.1:8081
      - api upgrades /api/terminal/ws to terminal's loopback ttyd at 127.0.0.1:7681
      - worker reaches Redis at 127.0.0.1:6379
  - the same user-assigned managed identity (id-elb-control)
  - the same lifecycle (start, stop, restart together)

Private endpoints and managed identity
  |
  +-- Key Vault
  +-- Storage accounts (platform + workload, including the redis-data file share)
  +-- Azure Container Registry
  +-- AKS private or restricted API server
```

## Component Plan

| Component | Target service | Purpose | Notes |
|-----------|----------------|---------|-------|
| `ca-elb-control` | Azure Container Apps | Single Container App, six sidecars | `minReplicas: 1`, `maxReplicas: 1`. Public ingress only on the `api` container. |
| `frontend` sidecar | Container in `ca-elb-control` | nginx:alpine serving the built React SPA `dist/` | Listens on `127.0.0.1:8081`. SPA navigation fallback to `/index.html`. Security headers (CSP, HSTS, X-Frame-Options, etc.) move from `staticwebapp.config.json` into `nginx.conf`. Image tag matches the SPA build hash so cache-busting is automatic across revisions. |
| `api` sidecar | Container in `ca-elb-control` | FastAPI HTTP API on Python 3.11 + reverse proxy for non-`/api/*` to the frontend sidecar | Exposes the same `/api/*` contract during phase 1. Public ingress restricted (Container Apps ingress with optional `allowedCidrs`). Forwards requests that do not match `/api/*` to `127.0.0.1:8081`. Terminates the browser WebSocket and proxies it to the `terminal` sidecar's loopback `ttyd` after MSAL + tenant-role check. |
| `worker` sidecar | Container in `ca-elb-control` | Celery worker | Pulls from `redis://127.0.0.1:6379/0`. Writes progress to Storage. |
| `beat` sidecar | Container in `ca-elb-control` | Celery beat scheduler | Reads schedule definitions from Storage. Singleton by construction (one container, one replica). |
| `redis` sidecar | Container in `ca-elb-control` | Broker + result backend | `redis:7-alpine`. Binds to `127.0.0.1` only. AOF on, RDB off. `/data` mounted from Azure Files share `redis-data`. |
| `terminal` sidecar | Container in `ca-elb-control` | Browser-accessible operator shell with the `elastic-blast` toolchain | Image based on Ubuntu 22.04 with `azure-cli`, `kubectl`, `azcopy`, `python3.11`, `primer3`, `tmux`, `git`, `jq`, `make`, and the `elastic_blast` package + venv pre-installed. Runs `ttyd -p 7681 -i 127.0.0.1 -W tmux new -A -s elb` so each browser session attaches to the same persistent tmux. `/home/azureuser` mounted from Azure Files share `terminal-home` for file persistence across revision restarts. Authenticates to ARM with `id-elb-control` via the env-injected MSI endpoint. |
| Job state | Azure Storage table + blob | Job registry, audit log, command history, schedule records | Table for indexed lookups (`PartitionKey=job_id`); blob (append) for audit trail; blob for large request/response payloads. |
| Secrets | Azure Key Vault | App configuration references and any future SSH material | Use private endpoint and RBAC. Keep purge protection enabled. No VM admin password is stored anywhere because there is no VM. |
| Runtime storage | Azure Storage | Query, config, DB, and result blobs | Use private endpoints, HNS where needed, and managed identity auth. |
| Images | Azure Container Registry | App containers (frontend, api, worker, beat, terminal) and ElasticBLAST images | Disable anonymous pulls. Use private endpoint where supported by environment. |
| Workload cluster | AKS | ElasticBLAST compute plane | Keep Workload Identity and Blob CSI. Prefer private cluster or authorized IP ranges. |
| Observability | App Insights plus Log Analytics | Logs, metrics, traces, audit | Use shared `job_id`, `task_id`, and `correlation_id` fields across sidecar logs. Each sidecar emits its own log stream. |

## Service Boundaries

All six sidecars run in the same Container App revision. Boundaries below
describe the responsibilities of each container, not separate Azure resources.

### `frontend` sidecar

Responsibilities:

- Serve the built React SPA (`web/dist/`) over loopback HTTP on
  `127.0.0.1:8081`.
- Provide SPA navigation fallback (any non-asset path that 404s on disk →
  serve `/index.html` with `200`).
- Apply the security headers that today live in
  [web/staticwebapp.config.json](web/staticwebapp.config.json):
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: strict-origin-when-cross-origin`,
  `Strict-Transport-Security: max-age=31536000; includeSubDomains`, and the
  Content-Security-Policy. These move from the SWA config into
  `nginx.conf`.
- Serve `/assets/*` with `Cache-Control: public, immutable, max-age=31536000`
  (Vite hashes asset filenames). Serve `/index.html` with
  `Cache-Control: no-cache` so a redeploy is picked up immediately.
- Run as non-root, no shell, no extra packages. `nginx:alpine` with a
  three-line custom config baked into the image.

Image build (`elb-frontend:<tag>`):

- Multi-stage Dockerfile: stage 1 runs `npm ci && npm run build` against
  [web/](web/); stage 2 is `FROM nginx:alpine` and copies `web/dist/` into
  `/usr/share/nginx/html` plus the custom `nginx.conf`.
- Image tag = the SPA build hash so cache busting is automatic across
  revisions.
- No managed identity needed; the container makes no outbound calls.

What this sidecar replaces:

| Old (Azure Static Web Apps) | New (`frontend` sidecar) |
|-----------------------------|--------------------------|
| `Microsoft.Web/staticSites` resource billed separately | Container in `ca-elb-control` (no extra Azure resource) |
| `staticwebapp.config.json` `routes` and `globalHeaders` | `nginx.conf` `location` blocks + `add_header` lines |
| `navigationFallback` `rewrite` to `/index.html` | nginx `try_files $uri /index.html;` |
| SWA's free TLS + global CDN | Container Apps ingress TLS (no CDN). Acceptable for low-traffic operator workload; Front Door can be added later if needed. |
| SWA hostname (`<name>.azurestaticapps.net`) registered as MSAL redirect URI | Container App ingress hostname (`<app>.<region>.azurecontainerapps.io`) registered as MSAL redirect URI. One-time MSAL App Registration update at cutover. |
| `/api/*` linked-backend rewrite to the Function App | Same hostname, no rewrite needed: api sidecar matches `/api/*` directly and forwards everything else to the frontend sidecar. |
| Separate CORS origin for SPA → Function App | None. Same origin. |

### `api` sidecar

Responsibilities:

- Validate MSAL bearer tokens on `/api/*` and on the WebSocket upgrade.
- Authorize requests against the caller identity and configured tenant.
- Serve fast read endpoints for dashboard state.
- Create command records in Storage and dispatch Celery tasks via
  `redis://127.0.0.1:6379/0`.
- Return `202 Accepted` for long-running operations with the Celery `task_id`
  and the `job_id` written to Storage.
- Expose status endpoints backed by Storage state, not by Celery's transient
  task result API.
- **Reverse-proxy non-`/api/*` requests to the frontend sidecar at
  `127.0.0.1:8081`.** This is the only routing rule the api needs: if the
  path starts with `/api/`, handle it; otherwise forward verbatim (preserve
  method, headers, body, query string) to the frontend.

The API should not block on Azure SDK long-running pollers except for small,
bounded reads. Any operation expected to exceed the frontend proxy timeout is
dispatched as a Celery task.

### `worker` sidecar

Responsibilities:

- Run a Celery worker process that pulls tasks from `redis://127.0.0.1:6379/0`.
- Execute tasks idempotently (use `job_id` as the idempotency key, guarded by
  status transitions in Storage).
- Use Azure SDK pollers for VM, AKS, ACR, Storage, and Key Vault operations.
- Persist each step transition to the Storage state document.
- Append audit events for security-relevant operations.
- Use Celery `autoretry_for` + exponential backoff with explicit retryability
  decisions.
- Clean up network exposure and temporary storage access in `finally` paths or
  `task_failure` signals.

Start with one worker process consuming a single `default` queue. Use named
queues (`azure`, `blast`, `storage`) only when there is real contention; even
then, all consumers run inside the same worker container because horizontal
scale-out is not available in this topology.

### `beat` sidecar

Responsibilities:

- Run a single Celery beat process.
- Read schedule definitions from Storage (custom scheduler implementation that
  reads from a blob/table on startup and on a short interval) so that schedules
  survive container restarts without an external database.
- Dispatch periodic tasks: AKS health snapshot, ACR tag drift check, storage
  access window auto-close reconciler, dead-letter scan, in-flight task
  reconciler (re-dispatch tasks observed as `running` whose worker disappeared).
- Singleton by construction: one container, one replica.

### `redis` sidecar

Responsibilities:

- `redis:7-alpine` (or pinned digest), no auth required because the listener is
  bound to `127.0.0.1` and is not reachable from outside the replica.
- `appendonly yes`, `appendfsync everysec`, RDB snapshots disabled.
- `/data` mounted from Azure Files share `redis-data` so AOF survives revision
  restart.
- Resource limits: 0.1 vCPU / 256 MiB initial; revisit after load testing.
- No outbound traffic; lifecycle managed entirely by the Container App.

This sidecar is a single point of failure for queued work within one revision.
Mitigation: tasks in flight are visible in Storage state, the AOF file is
persisted to Azure Files, and the `beat` reconciler re-dispatches tasks that
were observed as `running` but whose worker disappeared.

### `terminal` sidecar

This replaces the previous Remote Terminal VM. The user gets a browser-based
shell with the full `elastic-blast` toolchain, reached only through the api
sidecar's authenticated WebSocket proxy.

Image build (`elb-terminal:<tag>`, pushed to the platform ACR):

- Base: `ubuntu:22.04`.
- Apt: `azure-cli`, `kubectl` (or installed via direct binary download for
  version pinning), `azcopy`, `python3.11`, `python3.11-venv`,
  `python3-pip`, `primer3`, `git`, `make`, `jq`, `unzip`, `curl`, `tmux`,
  `ttyd`.
- Pre-installed Python deps: `requirements/test.txt` from
  `dotnetpower/elastic-blast-azure`, the Azure mgmt SDKs (`azure-mgmt-resource`,
  `azure-mgmt-network`, `azure-mgmt-compute`, `azure-mgmt-storage`,
  `azure-mgmt-containerregistry`, `azure-mgmt-containerservice`,
  `azure-mgmt-authorization`, `azure-mgmt-msi`, `azure-mgmt-monitor`), and the
  `elastic_blast` package itself (installed `--no-build-isolation --no-deps`
  exactly like the cloud-init script does today). Versions pinned in the
  `IMAGE_TAGS` table so a single bump propagates atomically.
- `/etc/profile.d/elb-env.sh` exports `PYTHONPATH=src:$PYTHONPATH`,
  `AZCOPY_AUTO_LOGIN_TYPE=MSI`, `ELB_SKIP_DB_VERIFY=true`,
  `ELB_DISABLE_AUTO_SHUTDOWN=1`.
- Entry point: `ttyd -p 7681 -i 127.0.0.1 -W tmux new -A -s elb`.
  - `-i 127.0.0.1` binds to loopback so only the api sidecar (same network
    namespace) can reach it.
  - `-W` makes the shell writable (default ttyd is read-only).
  - `tmux new -A -s elb` attaches every browser session to a single
    persistent tmux session called `elb`, so refreshing the browser does not
    lose work and multiple browser tabs share state. tmux also keeps
    long-running `elastic-blast submit` from dying when the WebSocket drops.

Auth and authorization on the WebSocket:

- Browser opens `wss://<api-host>/api/terminal/ws` with the MSAL access token
  in the `Sec-WebSocket-Protocol` header (or as a `?token=` query parameter
  with a short-lived API-issued one-time-use ticket; see verification).
- The api sidecar validates the token, requires the caller to hold a tenant
  role such as `elb-operator`, and only then upgrades the WebSocket and
  starts a duplex copy with the loopback ttyd.
- Per-session correlation id (`session_id`) is logged at upgrade and on
  close, with `owner_oid` and `tenant_id`.
- Idle-timeout: api closes the WebSocket after 30 minutes of no activity in
  either direction. tmux survives so reconnecting resumes the same session.

Azure auth from inside the terminal:

- Container Apps exposes a managed-identity endpoint to the workload
  (`IDENTITY_ENDPOINT` and `IDENTITY_HEADER` env vars). The shell startup
  script runs `az login --identity` (or, if the user prefers their own
  identity, `az login --use-device-code`). The MOTD explains both options.
- `AZCOPY_AUTO_LOGIN_TYPE=MSI` means `azcopy` picks up the same identity.
- `kubectl` uses kubeconfig generated by `az aks get-credentials --admin` (or
  via `aksAadAuth` once the cluster is configured for AAD); the AKS
  permissions on `id-elb-control` cover this.

Persistence:

- `/home/azureuser` is mounted from Azure Files share `terminal-home` (SMB).
- Survives revision restart and image rebuild.
- Holds: `~/.azure/` profile, `~/.kube/config`, ssh known_hosts (if any),
  user-staged query files, downloaded result snippets, the cloned
  `elastic-blast-azure` repo (read-only convenience copy; the venv and
  pre-installed tooling live inside the image, not the share).
- The share is on the platform Storage account whose `publicNetworkAccess`
  is `Disabled`, mounted via the file private endpoint.

Lifecycle:

- Starts and stops with the rest of the Container App revision. There is no
  per-user provisioning, no per-VM cloud-init wait, and no admin password to
  reveal.
- Resource limits: 0.5 vCPU / 1 GiB initial; revisit after the first real
  user session that runs an `elastic-blast submit`. The terminal is the
  single largest sidecar in the bundle because it carries the toolchain.

What this sidecar replaces (full inventory of the Remote Terminal VM's
responsibilities):

| Old (VM) | New (sidecar) |
|----------|---------------|
| Ubuntu 22.04 VM (`vm-elb-terminal`) | `elb-terminal:<tag>` container in `ca-elb-control` |
| 10-15 min cloud-init bootstrap (apt, pip, clone, venv, defender-onboarding retry) | Image build does this once at CI time. Cold start is whatever the container engine takes (seconds). |
| `azure-cli`, `kubectl`, `azcopy`, `git`, `make`, `jq`, `python3.11`, `primer3`, `tmux` installed via cloud-init | All baked into the image at build time, with retry / failure handling moved to CI |
| `~/elastic-blast-azure` clone + venv + `pip install -r requirements/test.txt` + `pip install --no-build-isolation --no-deps elastic_blast` | All baked into the image; venv at `/opt/elb/venv`. The cloned repo is also surfaced under `/home/azureuser/elastic-blast-azure` via Azure Files for user convenience but is not on the critical path. |
| `azure-mgmt-*` SDKs installed via cloud-init | Baked into the image |
| `/etc/profile.d/elb-env.sh` env vars | Same content baked into the image |
| `elb-az-login-mi` script that `az login --identity` from IMDS | Same script runs from the image; uses Container Apps' MI endpoint instead of IMDS. The end result (`az account show` works) is identical. |
| MOTD with onboarding hints | Same MOTD baked into the image |
| SSH on port 22 + 443 | **Removed.** No SSH. Browser → api WebSocket → ttyd. |
| `Port 22 / Port 443` in `sshd_config` | **Removed.** |
| Per-VM admin password generated and stored in Key Vault, revealed once via `/api/terminal/{vm}/password` | **Removed.** No password. Access is gated by MSAL + tenant role on the WebSocket upgrade. |
| NSG with `AllowSSH` rule scoped to caller IP via `/api/terminal/{vm}/open-ssh` | **Removed.** No NSG, no IP allow-list. |
| `/api/terminal/{vm}/start` (deallocate the VM) | **Removed.** Terminal lifecycle is the Container App revision lifecycle; stopping the terminal would mean stopping the whole control plane. |
| `/api/terminal/{vm}/stop` (deallocate the VM) | **Removed** for the same reason. |
| `/api/terminal/{vm}/destroy` (delete VM, NIC, IP, KV secret) | Replaced by container-image redeploy. There is no per-user resource to delete. |
| `/api/terminal/{vm}/health` (power state, cloud-init progress, reachability) | Replaced by the Container App revision health and a cheap `/api/terminal/health` ping that checks `tcp://127.0.0.1:7681` from the api sidecar. |
| `/api/terminal/provision` Durable orchestrator (RG, network, KV, password, VM, RBAC, cloud-init poll) | **Removed.** Provisioning is `azd up` + revision rollout. The first time the platform is deployed there is one-time AKS workload-identity / RBAC setup, but no per-user provisioning. |
| Persistent `/home/azureuser` on the OS disk | Azure Files share `terminal-home` mounted at `/home/azureuser`. |
| Operator runbook step: "wait for cloud-init", "open NSG to your IP", "reveal password", "ssh in" | Operator runbook step: "open the Terminal tab in the dashboard". |

Verification:

- A test that opening `wss://<api-host>/api/terminal/ws` without a token
  returns `401`; without the required tenant role returns `403`; with both
  succeeds and returns a working bash prompt.
- A test that two concurrent browser tabs see the same tmux session and that
  closing one tab does not kill the other or kill any process started in
  the shared session.
- A test that running `az account show` from the terminal sidecar returns
  the `id-elb-control` identity by default, and that running `az login
  --use-device-code` lets the user override with their own identity for the
  duration of the session (without leaking back into the shared tmux for
  other users — sessions are per-tmux-window, and the docs make this
  explicit).
- A test that `kubectl get nodes`, `azcopy ls`, and `elastic-blast --help`
  all work without further setup.
- A test that the api sidecar refuses to upgrade the WebSocket when the
  `terminal` sidecar's loopback port is unreachable, returning a 503 with a
  clear "terminal sidecar unhealthy" message.

## Command and State Model

Replace Durable Functions with an explicit Celery task model backed by Storage.

```text
HTTP POST /api/blast/submit
  -> validate request
  -> write Storage state row: PartitionKey=job_id, status=queued
  -> dispatch Celery task: submit_blast.delay(job_id=...)
  -> return 202 + { job_id, task_id }

ca-elb-control / worker sidecar pulls task from Redis sidecar (127.0.0.1:6379)
  -> update Storage: status=running, phase=checking_vm
  -> execute steps with autoretry_for + exponential backoff
  -> append audit event after each step
  -> update Storage: status=completed or failed
  -> on failure, run cleanup compensations (close storage window, etc.)
```

Recommended Storage layout (platform storage account):

| Container / table | Purpose | Format |
|-------------------|---------|--------|
| `job-state` (table) | Indexed lookup of current job status | `PartitionKey=job_id`, `RowKey="current"`, columns: `status`, `phase`, `owner_oid`, `tenant_id`, `created_at`, `updated_at`, `task_id`, `error_code` |
| `job-history` (table) | Per-step transitions (queryable by job) | `PartitionKey=job_id`, `RowKey=ulid(timestamp)`, columns: `phase`, `event`, `payload_blob_uri` |
| `job-payloads` (blob, append) | Sanitised request and result payloads, large step outputs | One append-blob per `job_id`; immutable once `status` is terminal |
| `audit` (blob, append) | Security-relevant events (storage open/close, role assignment changes, terminal lifecycle) | Daily-rolled append blobs, JSON Lines |
| `schedules` (blob) | Celery beat schedule definitions | Single JSON blob, versioned by ETag |
| `dead-letter` (blob) | Tasks that exhausted retries | One blob per failure, includes task name, args (sanitised), traceback |

State document shape (table row, JSON-encoded `payload` column for variable
fields):

```json
{
  "PartitionKey": "job_id",
  "RowKey": "current",
  "type": "blast_job",
  "tenant_id": "...",
  "owner_oid": "...",
  "status": "queued|running|completed|failed|cancelled",
  "phase": "checking_vm|opening_storage|uploading|submitting|polling|closing_storage",
  "created_at": "2026-05-14T00:00:00Z",
  "updated_at": "2026-05-14T00:00:00Z",
  "task_id": "celery-uuid",
  "error_code": null,
  "payload_blob_uri": "https://stelb*/job-payloads/<job_id>.jsonl"
}
```

Keep request payloads sanitised. Do not store bearer tokens, SAS URLs, VM
passwords, or raw command output that may contain secrets in any Storage
artifact.

### Why Storage instead of a database

- Workload is append-mostly with single-key lookups (`job_id`).
- Consistency model needed is single-row ETag updates, not multi-row
  transactions.
- Storage tables are billed per operation, with no minimum throughput.
- A future move to Cosmos DB or PostgreSQL is straightforward because the
  repository layer hides the storage shape.

## Route Migration Map

| Current area | Target owner | Migration notes |
|--------------|--------------|-----------------|
| `/api/health`, `/api/me` | `api` sidecar | Direct FastAPI routes. |
| `monitor/*` | `api` sidecar with optional cache | Keep as fast reads. Add short TTL cache for expensive AKS and blob-count calls. |
| `resources/ensure-*` | `worker` sidecar (Celery) | API dispatches resource tasks and reads progress from Storage state. |
| `terminal/provision` | **Removed** | The terminal is now a sidecar that ships with the Container App; there is no per-user provisioning. |
| `terminal/status/{instance_id}` | **Removed** | No Durable orchestration to track; replaced by `GET /api/terminal/health`. |
| `terminal/{vm}/start`, `stop`, `destroy` | **Removed** | Lifecycle is the Container App revision; no per-VM action. |
| `terminal/{vm}/password` | **Removed** | No SSH password to reveal. |
| `terminal/{vm}/open-ssh` | **Removed** | No SSH path. |
| `terminal/{vm}/health` | `api` sidecar (renamed `terminal/health`) | Cheap loopback ping to `127.0.0.1:7681` plus revision-state passthrough. |
| `terminal/ws` (new) | `api` sidecar (WebSocket upgrade) | After MSAL + tenant-role check, duplex-copy bytes between the browser and the terminal sidecar's loopback `ttyd`. |
| `aks/provision`, `aks/openapi/deploy` | `worker` sidecar (Celery) | Long-running ARM and AKS Run Command operations must run as Celery tasks. |
| `aks/openapi/proxy` | `api` sidecar initially | Later replace public LoadBalancer with private service access or API gateway pattern. |
| `acr/build-images` | `worker` sidecar (Celery) | One task per image; track ACR run IDs in Storage state. |
| `storage/prepare-db` | `worker` sidecar (Celery, `storage` queue) | Avoid background threads in API. Worker owns NCBI download/copy progress. |
| `blast/submit`, `blast/delete`, `warmup/start` | `worker` sidecar (Celery, `blast` queue) | Celery tasks with explicit state transitions in Storage. |
| `blast/jobs/*` | `api` sidecar | Reads from Storage state and Storage data plane. |
| Durable entities | Storage state | Replace job registry, audit trail, and schedules with Storage table + append blobs. |
| Scheduled work (DB refresh, monitoring, reconciler) | `beat` sidecar | Celery beat dispatches to the appropriate worker queue. |

## Networking Plan

Use one platform VNet with purpose-specific subnets.

| Subnet | Purpose |
|--------|---------|
| `snet-containerapps` | Container Apps Environment infrastructure (the single `ca-elb-control` app and its six sidecars). |
| `snet-private-endpoints` | Private endpoints for Key Vault, Storage (blob + table + file), and ACR. |
| `snet-aks` | AKS nodes when the workload cluster is created by this platform. |

No `snet-redis` subnet: Redis runs as a sidecar inside the Container App and
is bound to `127.0.0.1` only.

No `snet-terminal` and no `snet-bastion` subnet: there is no Remote Terminal
VM and no Bastion. The browser shell is the `terminal` sidecar, reached via
the api sidecar's authenticated WebSocket proxy.

Private DNS zones:

- `privatelink.vaultcore.azure.net`
- `privatelink.blob.core.windows.net`
- `privatelink.table.core.windows.net`
- `privatelink.file.core.windows.net`
- `privatelink.azurecr.io`

(No `privatelink.servicebus.windows.net` and no Cosmos/PostgreSQL DNS zones.)

Network rules:

- Key Vault `publicNetworkAccess` is `Disabled` from day 1; reachable only via
  its private endpoint.
- Platform Storage `publicNetworkAccess` is `Disabled` from day 1, including
  the Azure Files share that backs the Redis AOF mount. Reachable only via
  blob, table, and file private endpoints in `snet-private-endpoints`.
- Workload Storage `publicNetworkAccess` is `Disabled` from day 1. AKS reaches
  it through the blob (and dfs, if HNS) private endpoints because AKS nodes
  run in `snet-aks` in the same VNet. The terminal sidecar reaches workload
  storage from `snet-containerapps` over the same private endpoint.
- The previous temporary-public-access window for ElasticBLAST (auto-enable
  -> wait -> auto-disable) is **removed**. There is no operational state in
  which any in-scope storage account is publicly reachable.
- ACR `publicNetworkAccess` is `Disabled` once private endpoint is verified
  from the Container App and AKS.
- No public SSH path exists in the final design because there is no Remote
  Terminal VM. The browser shell is reached only through the api sidecar's
  authenticated WebSocket proxy.
- Restrict AKS API access with private cluster or authorized IP ranges.

## Identity and RBAC Plan

Use user-assigned managed identities so identities survive app recreation and
can be referenced cleanly from Bicep.

| Identity | Assigned to | Required scopes |
|----------|-------------|-----------------|
| `id-elb-control` | `ca-elb-control` Container App (shared by all six sidecars including `frontend` and `terminal`) | Contributor plus User Access Administrator on workload RGs; Storage Table Data Contributor + Storage Blob Data Contributor + Storage File Data SMB Share Contributor on platform storage; data-plane roles on workload Storage and ACR; Key Vault Secrets User; AcrPull on the platform ACR; AKS RBAC reader / `Azure Kubernetes Service Cluster User` so the terminal sidecar can run `kubectl` against the cluster. The `frontend` sidecar makes no Azure calls and inherits the MI only because it lives in the same revision. |
| `id-elb-openapi` | AKS Workload Identity | Storage Blob Data Contributor, AKS permissions, workload RG permissions needed by ElasticBLAST. |

Because the six sidecars share one MI, the api sidecar technically holds the
same ARM Contributor rights as the worker, the terminal, and the frontend.
Scope abuse is mitigated by:

- Mutating ARM operations only run inside Celery task handlers (in the
  worker process) or as user-typed shell commands inside the terminal sidecar
  (which is gated by MSAL + tenant role at the WebSocket upgrade).
- The api sidecar's request handlers do not call ARM mutation methods; this
  is enforced by static analysis (allow-list of Azure SDK call sites per
  sidecar package).
- The frontend sidecar is `nginx:alpine` with no Azure SDK and no shell; it
  cannot use the MI even if it wanted to.

A future split into separate Container Apps would re-introduce per-process
identities; this is an explicit, documented compromise in exchange for the
cost saving.

Keep the browser token as proof of caller identity. Do not exchange or persist
the token in Celery task arguments. Store `owner_oid`, `tenant_id`, and approved
operation parameters in the Storage state row. The worker sidecar uses the
shared managed identity (`id-elb-control`) for all Azure operations.

## Storage Plan

Storage has three roles:

1. **Platform state storage** for the control plane: job registry table, audit
   append blobs, schedule definitions, dead-letter records, the `redis-data`
   Azure Files share that backs the Redis sidecar's AOF, and the
   `terminal-home` Azure Files share that backs the terminal sidecar's
   `/home/azureuser`.
2. **ElasticBLAST workload storage** for `blast-db`, `queries`, and `results`.
3. **Operational artifacts**: container release zips, diagnostic dumps.

Target rules:

- Use managed identity and Azure RBAC; do not use shared keys.
- **Every Storage account in scope (platform + workload) has
  `publicNetworkAccess: Disabled`, `networkAcls.defaultAction: Deny`, and
  `bypass: None`. This is enforced from creation, not as a later hardening
  step. See the “Storage Network Isolation” section for the full requirement
  set and verification steps.**
- Keep HNS enabled on workload storage when ElasticBLAST needs it. Platform
  state storage does **not** need HNS. The `redis-data` and `terminal-home`
  file shares live on the platform account.
- Keep containers private. The `redis-data` and `terminal-home` file shares
  are exposed only via private endpoint and mounted by the Container Apps
  Environment.
- Generate **no** SAS for browser-facing flows. All browser uploads and
  downloads go through the api sidecar as a streaming proxy. See the
  "Browser ↔ Storage Proxy" section for the contract, chunk sizes,
  concurrency limits, and verification tests.
- Internal SAS use (e.g. legacy Function App `WEBSITE_RUN_FROM_PACKAGE` during
  cutover) is allow-listed and time-bounded. No new internal SAS use is added
  in this migration.
- Store DB preparation progress in the platform state table, not background
  threads.
- For large NCBI database imports, the worker downloads through the private
  Storage endpoint. Server-side copy is not relied upon if the source forces
  public-only access.
- Apply lifecycle policies on `dead-letter` and `audit` blobs (e.g. cool tier
  after 30 days, delete after 365 days) to bound cost.

## AKS Plan

AKS remains the compute plane for ElasticBLAST. The migration should improve how
the control plane talks to AKS, not replace AKS.

Target rules:

- Keep OIDC issuer and Workload Identity enabled.
- Keep Blob CSI driver enabled if BLAST DB access depends on it.
- Prefer private cluster for production environments.
- If private cluster is not feasible during phase 1, configure authorized IP
  ranges and audit the exception.
- Replace public `elb-openapi` LoadBalancer with an internal service or ingress
  once the Container Apps Environment and AKS can communicate privately.
- Continue to surface AKS node, pod, warmup, and job state through API routes;
  do not make the browser talk to AKS directly.

## Infrastructure Changes

Add new Bicep modules before removing the Function App module:

- `infra/modules/containerAppsEnvironment.bicep` (Environment + Azure Files
  storage definition that mounts `redis-data`).
- `infra/modules/containerAppControl.bicep` (single `ca-elb-control` Container
  App with six sidecars wired together; Redis and terminal volume mounts;
  shared MI).
- `infra/modules/storageState.bicep` (platform table + audit/payload/schedule
  containers + the `redis-data` file share, lifecycle policies).
- `infra/modules/privateEndpoints.bicep`
- `infra/modules/identities.bicep`
- `infra/modules/acr.bicep`, if platform app images use a platform-owned ACR

Deleted vs the previous (multi-app) revision of this plan:

- `serviceBus.bicep` (no Service Bus).
- `stateStore.bicep` (no Cosmos / PostgreSQL).
- `redisVm.bicep` (no Redis VM).

Update `azure.yaml` only after the new containers can be built and deployed.
The first deployment should support both backends in parallel:

- Existing Function App remains the production API.
- Container Apps backend is deployed under a separate hostname.
- The SPA points to the new API only in a dev or staging environment.

## Application Refactor Plan

### Phase 0: Preparation

- Add this architecture document.
- Add a shared service layer package that does not import `azure.functions` or
  `azure.durable_functions`.
- Inventory every route and classify it as fast read, queued command, or stream.
- Add correlation IDs to current API responses and logs so parity testing is
  easier.

### Phase 1: Containerize the API on a private network

- Provision the platform VNet and subnets (`snet-containerapps` /23 delegated
  to `Microsoft.App/environments`, `snet-private-endpoints`, `snet-aks`).
- Provision the Container Apps Environment with workload profile and the
  `snet-containerapps` infrastructure subnet.
- Provision the platform Storage account, Key Vault, and ACR with
  `publicNetworkAccess: Disabled` from creation. Add the private endpoints
  and link the private DNS zones to the platform VNet **before** the
  Container App tries to use them.
- Add a FastAPI app under `api_app/` or `api/asgi/`.
- Reuse existing Pydantic models and Azure service wrappers.
- Add a Dockerfile for Python 3.11.
- Implement `/api/health`, `/api/me`, and read-only `monitor/*` routes first.
- Deploy `ca-elb-control` to the Container Apps Environment with public
  ingress restricted to the SWA origin where possible.
- Verify from inside the api sidecar that storage hostnames resolve to
  private IPs and that an external curl to the same hostname is rejected.
- Keep the Function App route contract unchanged for the SPA.

### Phase 2: Add Storage State, Redis Sidecar, Terminal Sidecar, Frontend Sidecar, and Celery

- Provision the platform Storage state table/containers and the `redis-data`
  and `terminal-home` Azure Files shares.
- Build the `elb-terminal:<tag>` image (Ubuntu 22.04 + azure-cli + kubectl +
  azcopy + python3.11 + primer3 + tmux + ttyd + pre-installed
  `elastic_blast` venv) and push to the platform ACR.
- Build the `elb-frontend:<tag>` image (multi-stage: Vite build of
  [web/](web/) → `nginx:alpine` with the built `dist/` and a custom
  `nginx.conf` carrying the `staticwebapp.config.json` security headers).
- Update the Container App definition to bundle the six sidecars (`frontend`,
  `api`, `worker`, `beat`, `redis`, `terminal`) with the two Azure Files
  volume mounts.
- Add the api sidecar's catch-all reverse proxy to `127.0.0.1:8081` for any
  request whose path does not start with `/api/`.
- Add command, job, audit, and schedule repositories backed by Storage.
- Wire Celery into the API container (task dispatch only) using
  `redis://127.0.0.1:6379/0` as broker and result backend.
- Implement `POST -> dispatch task -> 202` flow for one low-risk operation,
  such as `storage/public-access/window` (only as a transitional shim during
  rollout — the day-1 invariant removes it).
- Add the `WS /api/terminal/ws` WebSocket upgrade in the api sidecar with
  MSAL + tenant-role check, and the duplex-copy proxy to `127.0.0.1:7681`.
- Use `job_id` as the idempotency key, guarded by status transitions in the
  Storage table.
- Add Celery `task_failure` signal handlers that write to the `dead-letter`
  blob container and an operator-visible retry story.

### Phase 3: Move Long-Running Work and Delete the Remote Terminal VM

- Move `terminal/provision` and the rest of the per-VM terminal API surface
  to **deletion**: there is no Remote Terminal VM in the new design. The
  `terminal` sidecar replaces it. See the Service Boundaries → `terminal`
  section for the full inventory of removed endpoints and what replaces them.
- Move `acr/build-images` to queue-backed worker execution.
- Move `storage/prepare-db` out of API background threads.
- Move `aks/provision` and `aks/openapi/deploy` to worker commands.
- Move `blast/submit`, `blast/delete`, and warmup flows last because they have
  the largest user-visible surface.

### Phase 4: Tighten and Verify Private Networking

- Confirm `publicNetworkAccess: Disabled` on platform Storage, workload
  Storage, Key Vault, and ACR.
- Confirm private DNS zones are linked to the platform VNet (blob, table,
  file, vault, acr).
- Run the verification checks listed in “Storage Network Isolation →
  Verification” section as part of the smoke suite.
- Convert `elb-openapi` service from public LoadBalancer to private access.
- Replace public SSH access with Bastion or a private terminal gateway.

This phase is verification-heavy because the network isolation invariants
were already established in phase 1.

### Phase 5: Cutover and Removal

- Run the SPA, served by the `frontend` sidecar, against the Container Apps
  backend in staging.
- Update the MSAL App Registration redirect URI to the Container App ingress
  hostname (keep the SWA hostname as a fallback URI until cutover is
  confirmed).
- Replay core workflows: open the browser shell, ensure resources, build
  images, provision AKS, prepare DB, submit BLAST, upload queries via the
  proxy, download results via the proxy (with Range), delete job.
- Switch production traffic to the Container App ingress hostname (DNS or
  Front Door upstream change, depending on what fronts the app).
- Keep the Static Web App and Function App deployed but unused for one
  release window.
- Remove the SWA hostname from the MSAL redirect URI list once the new
  hostname has been the only one used for a full release window.
- Remove the Static Web App resource (`Microsoft.Web/staticSites`).
- Remove Durable Functions code and Function App IaC after parity is proven.

## Validation Plan

Minimum validation before production cutover:

- Unit tests for repositories (Storage table + blob), Celery task handlers,
  auth, and Azure SDK wrappers.
- Container build test for every sidecar image.
- Local `docker compose` smoke test that runs all six sidecars together
  (or `docker run` of the api image with `nginx:alpine` and
  `redis:7-alpine` sidecars and Azurite for state).
- Integration test for Celery dispatch → worker execution → Storage state
  update with managed identity in an Azure dev environment.
- `azd provision --preview` or subscription-scope `what-if` for Bicep changes.
- End-to-end browser test of the dashboard against Container Apps.
- End-to-end BLAST smoke test with a tiny query.
- Network validation proving Key Vault and Storage can be reached privately and
  public access can be disabled.
- Failure-path validation that storage access is closed after failed BLAST work.
- Revision restart drill: trigger a Container App revision restart, confirm AOF
  on the Azure Files mount restores the queue, and confirm the beat reconciler
  re-dispatches any task observed as `running` whose worker disappeared.

## Cutover Checklist

- [ ] New `ca-elb-control` Container App is deployed in staging with all four
      sidecars healthy.
- [ ] `api` sidecar validates the same MSAL tokens as the Function App.
- [ ] Shared identity `id-elb-control` has all required RBAC at workload
      scopes.
- [ ] **Platform Storage account: `publicNetworkAccess=Disabled`,
      `defaultAction=Deny`, `bypass=None`, `ipRules=[]`. Verified.**
- [ ] **Workload Storage account: same as above. Verified.**
- [ ] **From inside the api sidecar, `<account>.blob.core.windows.net`,
      `.table.core.windows.net`, and `.file.core.windows.net` all resolve to
      `10.x.x.x` private IPs.**
- [ ] **From the public internet, `curl https://<account>.blob.core.windows.net/`
      returns `403 PublicAccessNotPermitted` or fails DNS resolution.**
- [ ] Storage state table contains migrated job registry, audit, and schedule
      records or the migration intentionally starts with a clean state.
- [ ] Celery dead-letter container is monitored.
- [ ] Redis sidecar successfully mounts `redis-data` Azure Files share while
      the platform storage account is `publicNetworkAccess=Disabled`. AOF
      survives a revision restart.
- [ ] **Terminal sidecar successfully mounts `terminal-home` Azure Files
      share while the platform storage account is
      `publicNetworkAccess=Disabled`.**
- [ ] Dashboard polling routes meet current response-time expectations.
- [ ] **Browser opens the Container App ingress hostname and gets the SPA
      served by the `frontend` sidecar; `/api/health` returns 200 from the
      same origin (no CORS preflight observed).**
- [ ] **MSAL App Registration redirect URI has been updated to the
      Container App ingress hostname; sign-in completes without redirect_uri
      mismatch.**
- [ ] **Static Web App resource has been deleted (or marked for deletion in
      the next cleanup window).**
- [ ] **Browser opens the Terminal tab, completes MSAL + tenant-role check,
      and gets a working bash prompt with `az account show`, `kubectl get
      nodes`, `azcopy --version`, and `elastic-blast --help` all responding
      successfully.**
- [ ] **Closing and re-opening the Terminal tab reattaches to the same tmux
      session (no work lost).**
- [ ] **No SSH endpoint exists in the deployed environment: `nmap -p 22
      <api-host>` finds no open SSH port and there is no public IP that
      points at a VM.**
- [ ] **`az resource list -g rg-elb-terminal --query "[?type=='Microsoft.Compute/virtualMachines']"`
      returns an empty list (or the resource group has been deleted).**
- [ ] ACR image build workflow is verified.
- [ ] AKS provision and OpenAPI deployment workflows are verified.
- [ ] BLAST submit/status/delete workflow is verified **without ever toggling
      Storage public access**. The legacy storage-window code path has been
      removed from the codebase.
- [ ] **Browser uploads of query files succeed via the api sidecar proxy with
      no SAS in browser DevTools.**
- [ ] **Browser downloads of result files succeed via the api sidecar proxy,
      including a Range request that returns `206 Partial Content`.**
- [ ] **CI grep check passes: no code path generates a SAS for a browser-bound
      response.**
- [ ] Private endpoint DNS resolution is verified from the Container App
      (blob, table, file, vault, acr).
- [ ] App Insights dashboards are updated for each sidecar's log stream.
- [ ] Rollback DNS or SWA backend setting is documented.

## Rollback Plan

Keep the Function App backend intact until the Container Apps backend has passed
one full release window.

Rollback steps:

1. Point SWA backend or `VITE_API_BASE_URL` back to the Function App.
2. Set `ca-elb-control` to `minReplicas: 0, maxReplicas: 0` to stop the
   `worker` and `beat` sidecars (and the `api` ingress) from issuing further
   commands.
3. Leave the Storage state table and the `redis-data` Azure Files share intact
   for forensic inspection.
4. Keep private endpoint changes only if they do not break the Function App
   runtime path. Otherwise restore public access with the documented exception.
5. File follow-up issues for every command that was in-flight during rollback.
5. File follow-up issues for every command that was in-flight during rollback.

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Duplicate task execution | Use `job_id` as the idempotency key and guard worker entry by expected status (`queued -> running` ETag swap on the Storage table row). |
| RBAC propagation delays | Surface retryable authorization failures and include role assignment diagnostics in worker logs. |
| Private DNS misconfiguration | Add a deployment validation script that resolves each private endpoint from the Container App (including Azure Files for the Redis mount). |
| Storage public access still needed by ElasticBLAST | Removed as a supported design state. The current `auto-keep-enabled` toggle and `bypass: AzureServices` shortcut are deleted in this migration. Anything that still depends on them is re-architected to use private endpoints (AKS in same VNet, browser uploads/downloads proxied through the api sidecar, NCBI imports performed by the worker over the private endpoint). |
| API sidecar saturated by proxy traffic | Initial guardrails: 4 concurrent transfers per replica (semaphore + `429`), 256 MiB upload cap, 1 MiB download chunk size, 4 MiB upload block size. Escalation path: split api into its own Container App with `maxReplicas` > 1; do **not** re-introduce SAS to the browser. |
| Large result download exceeds 240s Container Apps timeout | The proxy advertises `Accept-Ranges: bytes` and honors `Range` requests; the SPA chunks downloads larger than ~200 MiB. Single-shot downloads above the limit return a `Retry with Range` body. |
| Sidecar crash during cleanup | Store compensating cleanup tasks as separate Celery tasks and run a periodic reconciler from `beat`. |
| API contract drift | Keep typed frontend clients and run the SPA against both backends during staging. |
| Cannot scale API horizontally | Accepted trade-off for cost. The bundled topology pins `minReplicas: 1, maxReplicas: 1`. If sustained load ever requires scale-out, split `beat` and `redis` into a separate Container App first, then move `worker` to a separately scalable app. |
| Whole stack restarts on any image change | Deploy revisions during low-traffic windows. The reconciler in `beat` re-dispatches any task that was in flight at restart. |
| Redis sidecar is a single point of failure within the revision | AOF on Azure Files survives revision restarts; in-flight tasks are visible in Storage state and re-dispatched by the reconciler. |
| Shared identity over-grants the API sidecar | Mutating ARM operations only run inside Celery task handlers; document and review the surface. A future split into per-sidecar Container Apps restores per-process identities. |
| Celery beat duplicate scheduling | Beat is one container in one replica by construction; reject schedule writes if the beat heartbeat (written to a Storage row) is stale. |
| Browser terminal session lost on revision restart | tmux survives only within one revision. The user-visible files persist on `terminal-home` Azure Files. The terminal MOTD makes this explicit so the user runs long submissions inside `tmux` (the default attachment) rather than relying on tail-following. The api closes the WebSocket cleanly so the browser shows a banner instead of hanging. |
| Multiple operators sharing one tmux session | Single shared `elb` tmux is intentional for low-traffic operator-driven use. Separate per-user tmux is added if more than one operator regularly uses the terminal: ttyd is launched with `-W tmux new -A -s elb-${owner_oid}` and the api passes the validated `owner_oid` as an env var on session upgrade. |
| Terminal sidecar carries the heaviest image | The terminal image is built and tagged in CI; pinned through the same `IMAGE_TAGS` table that the worker uses. Cold start cost is amortised across the always-on `minReplicas: 1` revision. |
| WebSocket auth bypass | The api sidecar refuses any upgrade without a valid MSAL token and the configured tenant role. The loopback `ttyd` is bound to `127.0.0.1`, so even a misconfigured ingress cannot expose the shell directly. CI test asserts both. |
| Loss of Static Web App's global CDN | Container Apps ingress provides TLS but no CDN. Acceptable for low-traffic operator workload. Escalation: put Front Door in front of the Container App ingress (separate, optional resource). |
| MSAL redirect URI mismatch at cutover | One-time App Registration update during cutover. Cutover checklist gates production switch on a successful sign-in against the new hostname. Document a rollback step that re-points the redirect URI back to the SWA hostname while the SWA resource is still alive. |
| nginx misconfiguration breaks SPA routing | The `nginx.conf` is short (one `try_files` line + a few `add_header` lines + cache rules). It is part of the image and covered by an integration test that fetches `/`, `/some/deep/route`, and `/assets/<known-hash>.js` from a running container and asserts the right status + headers. |
| Frontend image cache busting | Image tag derived from the SPA build hash so a code change always produces a new image and a new revision. The api sidecar reverse proxy passes through the frontend's `Cache-Control` headers unchanged, so browser caching follows nginx's policy (immutable for `/assets/*`, no-cache for `/index.html`). |

## Open Decisions

| Decision | Recommended default | Why |
|----------|---------------------|-----|
| State store | Azure Storage table + append blobs | No managed database needed; append-mostly workload, single-key lookup. Repository layer hides shape so a future move to Cosmos DB is local. |
| Queue / broker | Redis 7 alpine sidecar inside the Container App | Removes the cost of any managed broker and the cost/operations of a dedicated VM. Acceptable because traffic is low and reconciliation handles in-flight loss. |
| Topology | One Container App with six sidecars | Minimum cost: one billable revision, smallest viable CPU/memory split, no Remote Terminal VM, no Static Web App. Trade-offs documented in Risks. |
| Scheduler | Celery beat sidecar | One scheduling system; reuses the Celery worker pool. |
| API framework | FastAPI | Matches Python/Pydantic style and supports OpenAPI naturally. |
| App image registry | Platform ACR | Keeps app container lifecycle separate from user ElasticBLAST ACRs. |
| SPA hosting | `frontend` sidecar (nginx:alpine) inside the same Container App | Removes the Static Web App resource; same-origin with the api removes CORS surface; one MSAL redirect URI. CDN can be added later via Front Door. |
| Browser terminal | `terminal` sidecar with loopback `ttyd`, proxied by the api sidecar's authenticated WebSocket | No SSH, no VM, no NSG, no admin password. tmux gives session continuity across browser refreshes. |
| Control plane in AKS | No | Keep control plane independent from the workload cluster it manages. |

## First Implementation Slice

The smallest useful PR should avoid moving BLAST execution immediately.

Scope:

- Add platform ACR and Container Apps Environment Bicep modules.
- Add the `ca-elb-control` Container App with only the `api` sidecar enabled
  initially, exposing `/api/health`, `/api/me`, and one monitor route.
- Add Dockerfile and azd wiring for the api image.
- Deploy side-by-side with the existing Function App.
- Add a staging-only frontend environment variable that points to Container
  Apps.

Exit criteria:

- The api image builds locally.
- The Container App deploys with managed identity.
- Health and identity routes work through HTTPS.
- One dashboard card can read from the Container Apps backend.
- Existing Function App production path remains unchanged.

The `worker`, `beat`, `redis`, `terminal`, and `frontend` sidecars (plus the
Azure Files mounts and Storage state schema) are introduced in phase 2 — the
first slice does not need them. Until the `frontend` sidecar is in place, the
existing Static Web App keeps serving the SPA and points its linked-backend at
the Function App, exactly as today.