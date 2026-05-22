---
title: Storage Network Isolation & Browser ↔ Storage Proxy
description: The hard requirement that workload Storage stays publicNetworkAccess Disabled — and the api-sidecar streaming proxy contract that makes the browser workflow work without issuing SAS tokens.
social:
  cards_layout_options:
    title: Storage Isolation & Proxy Contract
    description: publicNetworkAccess Disabled + api-sidecar streaming proxy — no SAS tokens to the browser, ever.
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
    3. All browser uploads/downloads stream through the `api` sidecar in
       1 MiB chunks (download) / 4 MiB blocks (upload), capped to 4
       concurrent transfers per replica.

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
3. **Browser ↔ storage**: the SPA never talks to Storage directly. **All
   browser downloads and uploads are proxied by the api sidecar.** No SAS
   tokens (user delegation or otherwise) are ever issued to the browser. See
   the next section for the full proxy contract.

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
  `BlobClient.url` for a browser-bound response fails the build. There is no
  permitted browser-bound SAS use.

## See also

- [Container Apps Architecture](container-apps.md) — full sidecar / sizing / cost reference.
- [Authentication & Authorization](authentication.md) — MSAL + managed identity that gates this proxy.
