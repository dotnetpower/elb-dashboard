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

- Keep the React SPA on Static Web Apps for now.
- Replace the Function App backend with a FastAPI container named `control-api`.
- Replace Durable Functions orchestration with Service Bus messages, a durable
  job-state store, and one or more worker containers.
- Run long or scheduled operations in `control-worker` containers or Azure
  Container Apps Jobs.
- Move platform resources behind VNet integration and private endpoints.
- Prefer user-assigned managed identities for API, workers, and AKS workloads.

Do not move the control plane into AKS as the first target. AKS is the workload
plane for ElasticBLAST. Hosting the control plane outside AKS keeps recovery,
upgrades, and cluster troubleshooting independent from the cluster being
managed.

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

## Target Architecture

```text
Browser
  |
  | MSAL access token
  v
Static Web App
  |
  | HTTPS /api
  v
Container Apps Environment, VNet integrated
  |
  +-- control-api
  |     - Validates JWTs
  |     - Serves REST endpoints
  |     - Enqueues long-running commands
  |     - Reads job state and monitoring snapshots
  |
  +-- control-worker
  |     - Processes Service Bus messages
  |     - Runs Azure SDK management operations
  |     - Updates job state and audit records
  |
  +-- scheduler or Container Apps Jobs
  |     - Starts scheduled BLAST and DB maintenance work
  |
  +-- terminal-gateway, optional
        - Proxies browser terminal sessions to private Remote Terminal access

Private endpoints and managed identity
  |
  +-- Key Vault
  +-- Storage accounts
  +-- Azure Container Registry
  +-- Service Bus
  +-- Cosmos DB or Azure Database for PostgreSQL
  +-- AKS private or restricted API server
```

## Component Plan

| Component | Target service | Purpose | Notes |
|-----------|----------------|---------|-------|
| SPA | Azure Static Web Apps | Browser UI and MSAL sign-in | Keep initially to reduce migration scope. Front Door can be added later if custom WAF/routing is required. |
| `control-api` | Azure Container Apps | REST API replacement for HTTP triggers | FastAPI on Python 3.11. Exposes the same `/api/*` contract during phase 1. |
| `control-worker` | Azure Container Apps | Long-running Azure and BLAST work | Consumes Service Bus messages. Writes progress to the state store. |
| Scheduled work | Container Apps Jobs or Service Bus scheduled messages | BLAST schedules, DB refresh checks, periodic monitoring | Prefer Service Bus scheduled messages when the worker already owns the command handler. |
| Job state | Cosmos DB or PostgreSQL | Job registry, audit log, orchestration status, schedules | Cosmos DB is best for JSON event documents; PostgreSQL is best if relational reporting becomes important. |
| Queue | Azure Service Bus | Durable command queue | Use sessions or correlation IDs per `job_id` when ordered progress matters. |
| Secrets | Azure Key Vault | VM passwords, SSH material, app configuration references | Use private endpoint and RBAC. Keep purge protection enabled. |
| Runtime storage | Azure Storage | Query, config, DB, and result blobs | Use private endpoints, HNS where needed, and managed identity auth. |
| Images | Azure Container Registry | App containers and ElasticBLAST images | Disable anonymous pulls. Use private endpoint where supported by environment. |
| Workload cluster | AKS | ElasticBLAST compute plane | Keep Workload Identity and Blob CSI. Prefer private cluster or authorized IP ranges. |
| Remote Terminal | VM plus optional gateway | Browser-accessible operator shell | Replace public SSH rules with Bastion or private terminal gateway in a later phase. |
| Observability | App Insights plus Log Analytics | Logs, metrics, traces, audit | Use shared `job_id`, `operation_id`, and `correlation_id` fields across API and worker logs. |

## Service Boundaries

### `control-api`

Responsibilities:

- Validate MSAL bearer tokens.
- Authorize requests against the caller identity and configured tenant.
- Serve fast read endpoints for dashboard state.
- Create command records and enqueue Service Bus messages for mutations.
- Return `202 Accepted` for long-running operations.
- Expose status endpoints backed by the state store, not Durable status APIs.

The API should not block on Azure SDK long-running pollers except for small,
bounded reads. Any operation expected to exceed the frontend proxy timeout goes
to the queue.

### `control-worker`

Responsibilities:

- Execute queued commands idempotently.
- Use Azure SDK pollers for VM, AKS, ACR, Storage, and Key Vault operations.
- Persist each step transition to the state store.
- Emit audit events for security-relevant operations.
- Use retry policies with explicit retryability decisions.
- Clean up network exposure and temporary storage access in `finally` paths.

Start with one worker container. Split into dedicated workers only when there is
real contention:

- `azure-worker` for ARM and resource lifecycle operations.
- `blast-worker` for submit/status/delete/warmup work.
- `storage-worker` for DB preparation and blob-heavy operations.

### `terminal-gateway`

This is optional for the first migration. If implemented, keep it separate from
`control-api` because terminal sessions have different scaling, timeout, and
network characteristics.

Target options, in preference order:

1. Azure Bastion based browser access.
2. Private VM plus terminal gateway in the Container Apps Environment.
3. Current public IP plus NSG allow-listing as a transitional fallback only.

## Command and State Model

Replace Durable Functions with an explicit command model.

```text
HTTP POST /api/blast/submit
  -> validate request
  -> create job record: status=queued
  -> enqueue Service Bus message: command=submit_blast, job_id=...
  -> return 202 + job_id

control-worker receives message
  -> status=running, phase=checking_vm
  -> execute steps with retries
  -> write phase progress after each step
  -> status=completed or failed
  -> write audit event
```

Recommended state documents:

```json
{
  "id": "job_id",
  "type": "blast_job",
  "tenant_id": "...",
  "owner_oid": "...",
  "status": "queued|running|completed|failed|cancelled",
  "phase": "checking_vm|opening_storage|uploading|submitting|polling|closing_storage",
  "created_at": "2026-05-14T00:00:00Z",
  "updated_at": "2026-05-14T00:00:00Z",
  "request": {},
  "steps": [],
  "result": {},
  "error": null
}
```

Keep request payloads sanitized. Do not store bearer tokens, SAS URLs, VM
passwords, or raw command output that may contain secrets.

## Route Migration Map

| Current area | Target owner | Migration notes |
|--------------|--------------|-----------------|
| `/api/health`, `/api/me` | `control-api` | Direct FastAPI routes. |
| `monitor/*` | `control-api` with optional cache | Keep as fast reads. Add short TTL cache for expensive AKS and blob-count calls. |
| `resources/ensure-*` | `control-worker` | API enqueues resource commands and reads progress from state store. |
| `terminal/provision` | `control-worker` | Replace Durable starter/status with command record and worker progress. |
| `terminal/*/start`, `stop`, `destroy` | `control-worker` | Fast actions can remain synchronous only if bounded. Otherwise enqueue. |
| `terminal/*/password` | `control-api` | Read Key Vault directly, preserve one-shot reveal semantics. |
| `aks/provision`, `aks/openapi/deploy` | `control-worker` | Long-running ARM and AKS Run Command operations must be queue-backed. |
| `aks/openapi/proxy` | `control-api` initially | Later replace public LoadBalancer with private service access or API gateway pattern. |
| `acr/build-images` | `control-worker` | Queue each image build and track ACR run IDs. |
| `storage/prepare-db` | `storage-worker` or `control-worker` | Avoid background threads in API. Worker owns NCBI download/copy progress. |
| `blast/submit`, `blast/delete`, `warmup/start` | `blast-worker` | Queue-backed command handlers with explicit state transitions. |
| `blast/jobs/*` | `control-api` | Reads from state store and Storage data plane. |
| Durable entities | State store | Replace job registry, audit trail, and schedules with durable documents/tables. |

## Networking Plan

Use one platform VNet with purpose-specific subnets.

| Subnet | Purpose |
|--------|---------|
| `snet-containerapps` | Container Apps Environment infrastructure. |
| `snet-private-endpoints` | Private endpoints for Key Vault, Storage, ACR, Service Bus, and state store. |
| `snet-aks` | AKS nodes when the workload cluster is created by this platform. |
| `snet-terminal` | Remote Terminal VM NICs. |
| `snet-bastion` | Azure Bastion, if used. |

Private DNS zones:

- `privatelink.vaultcore.azure.net`
- `privatelink.blob.core.windows.net`
- `privatelink.queue.core.windows.net`, if Storage Queue remains in use
- `privatelink.servicebus.windows.net`
- `privatelink.azurecr.io`
- Cosmos DB or PostgreSQL private DNS zone, depending on selected state store

Network rules:

- Set Key Vault `publicNetworkAccess` to `Disabled` after private endpoint
  validation.
- Set platform Storage `publicNetworkAccess` to `Disabled` after private
  endpoint validation.
- For ElasticBLAST user storage, prefer VNet/private endpoint access and avoid
  the current broad public toggle where possible.
- If ElasticBLAST itself still requires public blob access for specific
  operations, keep the existing temporary access-window behavior as a scoped
  fallback and record it in audit logs.
- Avoid public SSH to Remote Terminal in the final design. Use Bastion or a
  private terminal gateway.
- Restrict AKS API access with private cluster or authorized IP ranges.

## Identity and RBAC Plan

Use user-assigned managed identities so identities survive app recreation and
can be referenced cleanly from Bicep.

| Identity | Assigned to | Required scopes |
|----------|-------------|-----------------|
| `id-elb-control-api` | `control-api` | Read Key Vault secrets, read dashboard resources, read job state. |
| `id-elb-control-worker` | `control-worker` and jobs | Contributor plus User Access Administrator on workload RGs; data-plane roles on Storage, ACR, Service Bus, and state store. |
| `id-elb-terminal` | Remote Terminal VM | Storage Blob Data Contributor, AcrPull, limited RG access as required. |
| `id-elb-openapi` | AKS Workload Identity | Storage Blob Data Contributor, AKS permissions, workload RG permissions needed by ElasticBLAST. |

Keep the browser token as proof of caller identity. Do not exchange or persist
the token in queued messages. Store `owner_oid`, `tenant_id`, and approved
operation parameters in the command record. The worker uses managed identity for
Azure operations.

## Storage Plan

Storage has two roles:

1. Platform storage for application internals, logs, and optional queues.
2. ElasticBLAST workload storage for `blast-db`, `queries`, and `results`.

Target rules:

- Use managed identity and Azure RBAC; do not use shared keys.
- Keep HNS enabled on workload storage when ElasticBLAST needs it.
- Keep containers private.
- Generate user delegation SAS only for explicit result-download workflows.
- Store DB preparation progress in the state store, not background threads.
- For large NCBI database imports, prefer worker-managed download/upload through
  private Storage access over server-side copy if public access is blocked by
  policy.

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

- `infra/modules/containerAppsEnvironment.bicep`
- `infra/modules/containerApps.bicep`
- `infra/modules/serviceBus.bicep`
- `infra/modules/stateStore.bicep`
- `infra/modules/privateEndpoints.bicep`
- `infra/modules/identities.bicep`
- `infra/modules/acr.bicep`, if platform app images use a platform-owned ACR

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

### Phase 1: Containerize the API

- Add a FastAPI app under `api_app/` or `api/asgi/`.
- Reuse existing Pydantic models and Azure service wrappers.
- Add a Dockerfile for Python 3.11.
- Implement `/api/health`, `/api/me`, and read-only `monitor/*` routes first.
- Deploy `control-api` to Container Apps with public ingress restricted to the
  SWA origin where possible.
- Keep the Function App route contract unchanged for the SPA.

### Phase 2: Add State Store and Queue

- Provision Service Bus and the selected state store.
- Add command, job, audit, and schedule repositories.
- Implement `POST -> enqueue -> 202` flow for one low-risk operation, such as
  `terminal/start` or `storage/public-access/window`.
- Add idempotency keys per operation.
- Add dead-letter handling and an operator-visible retry story.

### Phase 3: Move Long-Running Work

- Move `terminal/provision` from Durable Functions to `control-worker`.
- Move `acr/build-images` to queue-backed worker execution.
- Move `storage/prepare-db` out of API background threads.
- Move `aks/provision` and `aks/openapi/deploy` to worker commands.
- Move `blast/submit`, `blast/delete`, and warmup flows last because they have
  the largest user-visible surface.

### Phase 4: Private Networking

- Deploy Container Apps Environment into the platform VNet.
- Add private endpoints and DNS links for Key Vault, Storage, ACR, Service Bus,
  and the state store.
- Turn off public access one resource at a time after smoke tests.
- Convert `elb-openapi` service from public LoadBalancer to private access.
- Replace public SSH access with Bastion or a private terminal gateway.

### Phase 5: Cutover and Removal

- Run SPA against Container Apps in staging.
- Replay core workflows: provision terminal, ensure resources, build images,
  provision AKS, prepare DB, submit BLAST, download results, delete job.
- Switch production `VITE_API_BASE_URL` or SWA backend link to Container Apps.
- Keep Function App deployed but unused for one release window.
- Remove Durable Functions code and Function App IaC after parity is proven.

## Validation Plan

Minimum validation before production cutover:

- Unit tests for repositories, command handlers, auth, and Azure SDK wrappers.
- Container build test for every image.
- Local `docker run` smoke test for `control-api`.
- Integration test for Service Bus enqueue/dequeue with managed identity in an
  Azure dev environment.
- `azd provision --preview` or subscription-scope `what-if` for Bicep changes.
- End-to-end browser test of the dashboard against Container Apps.
- End-to-end BLAST smoke test with a tiny query.
- Network validation proving Key Vault and Storage can be reached privately and
  public access can be disabled.
- Failure-path validation that storage access is closed after failed BLAST work.

## Cutover Checklist

- [ ] New Container Apps backend is deployed in staging.
- [ ] `control-api` validates the same MSAL tokens as the Function App.
- [ ] Worker identity has all required RBAC at workload scopes.
- [ ] State store contains migrated job registry, audit, and schedule records or
      the migration intentionally starts with a clean state.
- [ ] Service Bus dead-letter queue is monitored.
- [ ] Dashboard polling routes meet current response-time expectations.
- [ ] Terminal lifecycle workflow is verified.
- [ ] ACR image build workflow is verified.
- [ ] AKS provision and OpenAPI deployment workflows are verified.
- [ ] BLAST submit/status/delete workflow is verified.
- [ ] Storage public access returns to the secure state after success and
      failure.
- [ ] Private endpoint DNS resolution is verified from Container Apps.
- [ ] App Insights dashboards are updated for API and worker containers.
- [ ] Rollback DNS or SWA backend setting is documented.

## Rollback Plan

Keep the Function App backend intact until the Container Apps backend has passed
one full release window.

Rollback steps:

1. Point SWA backend or `VITE_API_BASE_URL` back to the Function App.
2. Stop Container Apps workers to prevent duplicate long-running operations.
3. Leave state store and Service Bus intact for forensic inspection.
4. Keep private endpoint changes only if they do not break the Function App
   runtime path. Otherwise restore public access with the documented exception.
5. File follow-up issues for every command that was in-flight during rollback.

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Duplicate command execution | Use idempotency keys and state transitions guarded by expected status. |
| RBAC propagation delays | Surface retryable authorization failures and include role assignment diagnostics in worker logs. |
| Private DNS misconfiguration | Add a deployment validation script that resolves each private endpoint from Container Apps. |
| Storage public access still needed by ElasticBLAST | Keep the temporary access-window fallback and audit every opening. |
| Worker crash during cleanup | Store compensating cleanup commands and run a periodic reconciler. |
| API contract drift | Keep typed frontend clients and run the SPA against both backends during staging. |
| Cost growth from always-on workers | Start with minimum replicas of zero where safe and KEDA scale rules on Service Bus. |

## Open Decisions

| Decision | Recommended default | Why |
|----------|---------------------|-----|
| State store | Cosmos DB | Durable JSON job events are a natural fit and query needs are modest. |
| Queue | Service Bus Standard | Better DLQ, scheduling, and operational controls than Storage Queue. |
| API framework | FastAPI | Matches Python/Pydantic style and supports OpenAPI naturally. |
| App image registry | Platform ACR | Keeps app container lifecycle separate from user ElasticBLAST ACRs. |
| Terminal access | Bastion first, gateway second | Removes public SSH from the steady-state design. |
| Control plane in AKS | No | Keep control plane independent from the workload cluster it manages. |

## First Implementation Slice

The smallest useful PR should avoid moving BLAST execution immediately.

Scope:

- Add platform ACR and Container Apps Environment Bicep modules.
- Add `control-api` FastAPI app with `/api/health`, `/api/me`, and one monitor
  route.
- Add Dockerfile and azd wiring for the API container.
- Deploy side-by-side with the existing Function App.
- Add a staging-only frontend environment variable that points to Container
  Apps.

Exit criteria:

- `control-api` container builds locally.
- Container App deploys with managed identity.
- Health and identity routes work through HTTPS.
- One dashboard card can read from the Container Apps backend.
- Existing Function App production path remains unchanged.
