---
title: Storage Network Isolation & Browser ↔ Storage Proxy
description: The hard requirement that workload Storage stays publicNetworkAccess Disabled — and the current API-mediated transfer contract that avoids browser SAS tokens.
social:
  cards_layout_options:
    title: Storage Isolation & Proxy Contract
    description: publicNetworkAccess Disabled + API-mediated transfers — no SAS tokens to the browser, ever.
tags:
  - architecture
  - security
  - infra
---

# Storage Network Isolation & Browser ↔ Storage Proxy

This page is the **load-bearing security contract** of the ElasticBLAST control
plane. Every other rule in [Container Apps Architecture](container-apps.md) is
consistent with these requirements; this page is extracted so it can be cited,
audited, and reviewed on its own.

!!! danger "Hard requirements (NON-NEGOTIABLE)"

    1. Every workload Storage account stays `publicNetworkAccess: Disabled` in
       production. No code path enables it, even temporarily.
    2. The browser **never** receives a SAS token — not user delegation, not
       account, not service. The `api` sidecar is the only Storage client the
       browser sees.
     3. Browser query and result traffic terminates at the `api` sidecar. Inline
       query text is staged by the API, while result-file downloads are
       streamed through it. The browser never connects to a Storage endpoint.

The sanctioned exceptions are explicit, IP-allowlisted, local-debug only.
See [.github/copilot-instructions.md §9](https://github.com/dotnetpower/elb-dashboard/blob/main/.github/copilot-instructions.md#9-storage-network-isolation-hard-requirement)
for the toggle helpers (`scripts/dev/local-run.sh storage-on|storage-off`).

## Storage Network Isolation (Hard Requirement)

This is the most important non-functional requirement of the control plane. Every
rule in the rest of the architecture documents is consistent with it.

### Rules

1. **Platform Storage account** (job state table, audit blobs, payload blobs,
   schedule blob, dead-letter blobs):
   - `publicNetworkAccess` is `Disabled` from the moment the account is in
     production use.
   - `networkAcls.defaultAction` is `Deny`.
   - `networkAcls.bypass` is `None` (not `AzureServices`).
   - No IP allow-list entries.
   - Reachable only via two private endpoints in `snet-private-endpoints`:
     blob and table. Each endpoint is wired into its private DNS zone
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
   - There is **no temporary public-access window**, no `auto-keep-enabled`
     toggle, and no `bypass: AzureServices` workaround. Anything that needs to
     reach Storage must do so via private endpoint from inside the VNet.
3. **Browser ↔ storage**: the SPA never talks to Storage directly. Query input,
  result-file downloads, and result previews all pass through an API route.
  No SAS tokens (user delegation or otherwise) are ever issued to the
  browser. See the next section for the current transfer contract.

### Container Apps Environment requirements that make rule 1 enforceable

- The Container Apps Environment **must** be VNet-integrated. Use the
  workload-profile environment with an `infrastructureSubnetId` pointing at
  `snet-containerapps`.
- `internal: true` is recommended (the SPA reaches the API through Front Door
  or via the Container App's external ingress). External ingress is acceptable
  *if and only if* the egress path to Storage still goes through the VNet.
  Egress through the VNet is the property that lets Storage stay private,
  not the ingress mode.
- `snet-containerapps` is delegated to `Microsoft.App/environments` and sized
  per Microsoft guidance (`/27` for Consumption-only, `/23` for workload
  profile environments). Pick `/23` so the topology can grow without renaming.
- All private DNS zones (`privatelink.blob.core.windows.net`,
  `privatelink.table.core.windows.net`,
  `privatelink.vaultcore.azure.net`, `privatelink.azurecr.io`) are linked to
  the platform VNet so the Container App resolves storage hostnames to
  private IPs.
- The Container App's outbound DNS must be the Azure-provided 168.63.129.16
  (default for Container Apps). Do **not** override `dnsConfig` in a way that
  bypasses the linked private DNS zones.

### What this forbids

- No code path enables Storage public access "just for a moment." The previous
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

## Browser ↔ Storage Proxy (No SAS to the Browser)

This is the contract that lets `publicNetworkAccess: Disabled` hold on day 1
without breaking the existing user workflows (uploading queries, downloading
results).

### Rules

- The api sidecar is the **only** Storage client the browser sees.
- Transfer modes differ by operation: result files use a streaming response;
  inline query FASTA is part of the submit JSON body and is therefore held in
  memory before the API stages it in Storage; previews are bounded API
  responses rather than direct blob downloads.
- Authentication: browser transfers carry a valid MSAL access token. Trusted
  automation may use the shared token while universal M2M auth is enabled, and
  Service Bus completion links use a scoped expiring download token.
- Job authorization: owner-scoped deployments require the caller's
  `object_id` to match `owner_oid`; cluster-shared/external rows have no owner.
  The shared deployment currently enables `BLAST_JOBS_SHARED_VISIBILITY`,
  which deliberately relaxes per-owner reads and must be disabled before
  multi-tenant/shared-subscription use.
- Authorization: current dashboard requests carry the configured
  `storage_account`. Job-bound result routes compare it with the account stored
  in `JobState` and reject a mismatch with `403 cross_account_mismatch`; legacy
  or not-yet-projected rows fall back to the supplied value. Account names are
  grammar-validated before they can form an Azure endpoint. The preferred file
  route uses an encoded `file_id`; the compatibility download route still
  accepts a validated job-owned `blob_name`.
- The api sidecar uses its managed identity + the private endpoint to talk to
  Storage. No SAS is ever generated, even server-side, for browser-facing
  flows.
- Concurrency: `stream_blob_bytes` wraps Storage downloads in a process-local
  semaphore. The default is 8 permits, configurable with
  `STORAGE_STREAM_MAX_CONCURRENCY`; acquisition waits up to 60 seconds by
  default (`STORAGE_STREAM_ACQUIRE_TIMEOUT_SECONDS`). The current routes do not
  translate exhaustion into a dedicated `429` response or add `Retry-After`.

### Result-file download contract

The preferred route is
`GET /api/blast/jobs/{job_id}/results/{file_id}`. The compatibility route is
`GET /api/blast/jobs/{job_id}/results/download?blob_name=...`.

Behaviour:

- Validate the caller and job ownership, cross-check the supplied Storage
  account when the state row records one, and ensure the decoded blob belongs
  to the requested job.
- Call the synchronous Azure Blob SDK's `download_blob()` before committing
  the HTTP response, then yield `StorageStreamDownloader.chunks()` through a
  FastAPI `StreamingResponse`. Chunk size is SDK-managed; the application does
  not pin it to 1 MiB.
- Infer `Content-Type` from the safe filename and set `Content-Disposition`.
  The current route does not forward Storage `ETag`, `Content-Length`,
  `Last-Modified`, or `Content-Encoding` metadata.
- The current route does not implement HTTP `Range` / `206 Partial Content` or
  advertise `Accept-Ranges`; interrupted large downloads restart from byte 0.
- For an external OpenAPI `file_id`, fall back to the OpenAPI result stream
  when the id is not a local encoded blob path.

### Inline query staging contract (`POST /api/blast/jobs`)

Behaviour:

- New Search reads the selected FASTA into browser state and sends it as the
  `query_data` field in a JSON request; there is no multipart or
  `POST /api/blast/jobs/{job_id}/queries` streaming-upload route today.
- The global request guard rejects a declared `Content-Length` above 10 MiB by
  default (`MAX_REQUEST_BODY_BYTES`, with a 100 MiB configuration ceiling).
  FastAPI still parses accepted JSON into memory.
- Before queuing the local Celery submit, the API validates the FASTA and
  synchronously calls `upload_blob` to write
  `queries/uploads/{job_id}/query.fa`, then persists the blob reference rather
  than the raw `query_data`.
- The application does not explicitly call `stage_block`, does not define a
  4 MiB application upload block, and has no separate 256 MiB query-upload
  limit.
- Inline OpenAPI `query_fasta` requests are API-mediated and forwarded to the
  sibling execution plane, which owns their staging.
- No browser-facing path generates a SAS or returns a direct upload URL.

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
download and holds accepted inline query JSON in memory while staging it. The
bundled Container App has a single replica, so sustained many-user downloads
or several large simultaneous submissions can saturate it. If future scale
breaks the operator-driven, low-concurrency assumption, the escalation path is
an independently scalable private transfer service or API deployment, **not**
browser SAS.

### Verification

Current automated coverage checks that a Storage download is opened before
FastAPI commits the streaming response, SDK chunks are yielded unchanged,
initial failures are raised eagerly, unsafe blob paths are rejected, and every
job-bound result route invokes the Storage-account cross-check. The security
suite also guards against reintroducing browser-bound SAS issuers.

HTTP Range support, a dedicated overload response, and a true streaming query
upload endpoint are **not** current capabilities and therefore must not appear
in runbooks or client expectations until their routes and tests land.

## See also

- [Container Apps Architecture](container-apps.md) — full sidecar / sizing / cost reference.
- [Authentication & Authorization](authentication.md) — MSAL + managed identity that gates this proxy.
