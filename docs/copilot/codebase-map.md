---
title: Codebase Map (Agent Detail)
description: Pre-computed which-file-owns-X lookup for agents working on elb-dashboard — replaces several grep / semantic_search calls per session.
tags:
  - agent
---

# Codebase Map — fast lookup for agents

> **Purpose**: skip 7-8 grep/semantic_search calls. Read this first whenever
> you need to know "which file owns X?" or "what does service Y do?".
> Verified 2026-09-16 against `api/main.py`, `api/celery_app.py`,
> `api/run_celery_workers.py`, and `web/src/App.tsx`.

This is the *index*. For policy read [.github/copilot-instructions.md](../../.github/copilot-instructions.md);
for navigation prose read [AGENTS.md](../../AGENTS.md).

---

## 1. Backend route map (api/routes/)

Registration order is enforced in [api/main.py](../../api/main.py#L371-L397).
Anything new MUST be inserted **above** `frontend_proxy.router`.

| Prefix | File | Verb / path | Notes |
|--------|------|-------------|-------|
| `/api/health` | [routes/health.py](../../api/routes/health.py) | GET `/health`, `/health/ready`, `/health/celery`, `/health/celery/result/{task_id}`, POST `/health/celery/enqueue-noop`, GET `/health/azure-discovery` | No auth. Celery diag — see "Celery shared_task trap" in repo memory. |
| `/api/me` | [routes/me.py](../../api/routes/me.py) | GET `/me`, `/me/permissions`, `/me/access-review` | `require_caller`: MSAL bearer, shared M2M token when enabled, or local-only dev bypass; `/me` also applies the dashboard-entry RBAC gate. |
| `/api/monitor` | [routes/monitor/](../../api/routes/monitor/) | `/aks`, `/aks/nodes`, `/aks/pods`, `/aks/top-nodes`, `/aks/pod-logs`, `/aks/service-ip`, `/aks/warmup-status`, `/aks/events`, `/aks/run-command`, `/metrics`, `/sidecar-requests`, `/storage`, `/acr`, `/terminal`, `/cluster`, `/jobs`, `/jobs/{id}`, `/sidecars`, `/sidecars/ticket`, `/sidecars/events`, `/logs/{container}/events` | Read-only package. Common `_graceful` lives in [routes/monitor/common.py](../../api/routes/monitor/common.py) and is re-exported for ARM routes. |
| `/api/ncbi` | [routes/ncbi.py](../../api/routes/ncbi.py) | GET `/nuccore/{accession}`, `/nuccore/{accession}/genbank`, `/nuccore/{accession}/fasta` | NCBI accession metadata + FASTA lookup. |
| `/api/arm` | [routes/arm.py](../../api/routes/arm.py) | `/subscriptions`, `/subscriptions/{sid}/resource-groups`, `/resource-group/tags` (GET/POST), `…/storage-accounts`, `…/acrs`, `…/vms` | ARM proxy via shared MI. |
| `/api/resources` | [routes/resources.py](../../api/routes/resources.py) | POST `/ensure-rg`, `/ensure-storage`, `/ensure-acr` | Synchronous wizard provisioning. |
| `/api/storage` | [routes/storage/](../../api/routes/storage/) | POST `/prepare-db`, GET `/local-debug`, POST `/local-debug/open` | Storage package; `prepare_db.py` owns DB copy, `local_debug.py` owns the IP-allowlist toggle (see charter §9). |
| `/api/v1/elastic-blast` | [routes/elastic_blast.py](../../api/routes/elastic_blast.py) | POST `/submit`, GET `/jobs`, `/jobs/{id}`, `/jobs/{id}/files/{file_id}` | External facade. |
| `/api/terminal` | [routes/terminal_ws.py](../../api/routes/terminal_ws.py) | POST `/ticket`, GET `/health`, `/azure-cli`, WS `/ws` | WebSocket → loopback ttyd. MSAL on handshake. |
| `/api/terminal/{vm}/...` | [routes/terminal_legacy.py](../../api/routes/terminal_legacy.py) | `/provision`, `/status/{iid}`, `/{vm}/password`, `/start`, `/stop`, `/destroy` | **HTTP 410 by design** — VM model retired. |
| `/api/tasks` | [routes/tasks.py](../../api/routes/tasks.py) | GET `/{task_id}` | Celery `AsyncResult` polling. |
| `/api/operations` | [routes/operations.py](../../api/routes/operations.py) | GET `/{operation_id}` | Async operation status polling. |
| `/api/aks` | [routes/aks/](../../api/routes/aks/) | `aks_router` package — SKUs, provisioning, OpenAPI deploy/spec/proxy, lifecycle, and role assignment. |
| `/api/acr` | [routes/acr.py](../../api/routes/acr.py) | `acr_build_router` — ACR build dispatch. |
| `/api/blast` | [routes/blast/](../../api/routes/blast/) | `blast_router` package — [jobs.py](../../api/routes/blast/jobs.py), [submit.py](../../api/routes/blast/submit.py), [databases.py](../../api/routes/blast/databases.py), [taxonomy.py](../../api/routes/blast/taxonomy.py), [schedules.py](../../api/routes/blast/schedules.py), [results.py](../../api/routes/blast/results.py), [capacity.py](../../api/routes/blast/capacity.py). Uses [_blast_shared.py](../../api/routes/_blast_shared.py) for shared HTTP helpers. |
| `/api/warmup` | [routes/warmup.py](../../api/routes/warmup.py), [routes/auto_oracle.py](../../api/routes/auto_oracle.py) | DB warmup planning/status plus DB order-oracle preferences. |
| `/api/audit` | [routes/audit.py](../../api/routes/audit.py) | `audit_router` — append-blob audit log. |
| `/api/notifications` | [routes/notifications.py](../../api/routes/notifications.py) | GET feed, POST `/seen`, POST `/clear` | Per-caller terminal-job notification feed and durable marker updates. |
| `/api/cost` | [routes/cost.py](../../api/routes/cost.py) | GET estimate and budget; PUT budget | Approximate cluster cost and per-cluster budget preference. |
| `/api/client-log` | [routes/client_log.py](../../api/routes/client_log.py) | POST `` (204) | Browser client diagnostic logging. |
| `/api/upgrade` | [routes/upgrade.py](../../api/routes/upgrade.py) | GET `/status`, `/candidates`, `/history`, `/escape-hatch`, `/rollback-preflight`, POST `/check`, `/start`, `/rollback` | In-app control-plane upgrade flow. |
| `/api/settings` | [routes/settings/](../../api/routes/settings/) | `settings_router` package — `/app-insights/*`, `/aks-observability/*`, `/control-plane`, `/ncbi`, `/openapi-token`, `/performance`, `/service-bus/*`, `/webhooks/*`, and `/vnet-peering/*`. |
| `/api/diagnostics` | [routes/diagnostics.py](../../api/routes/diagnostics.py) | GET `/{category}` | Read-only, severity-ranked reliability and availability checks. |
| `/*` (catch-all) | [routes/frontend_proxy.py](../../api/routes/frontend_proxy.py) | reverse-proxy to `127.0.0.1:8081` | Must stay last. |

> The monolithic `api/routes/stubs.py` (503-only) was split into the above per-domain routers in 2026-05-19. Old `from api.routes import stubs` imports are gone.

---

## 2. Backend services (api/services/)

Single source of truth for Azure SDK calls. Routes/tasks import from here;
never `azure.mgmt.*` directly outside `services/`.

### Azure SDK boundary

| File | Purpose |
|------|---------|
| [azure_clients.py](../../api/services/azure_clients.py) | `DefaultAzureCredential` singleton + `resource_client`, `network_client`, `compute_client`, … factories. **Only place that imports `azure.mgmt.*`.** |
| [keyvault.py](../../api/services/keyvault.py) | KV provisioning + access policy + Secrets Officer role assignment. |
| [storage/data.py](../../api/services/storage/data.py) | Blob upload/read facade and bounded result streaming (SDK-managed chunks; 8 download permits by default). **Never imports `generate_blob_sas` / `get_user_delegation_key`** — load-bearing comment at file bottom. |
| [storage/network.py](../../api/services/storage/network.py) | Private endpoint wiring for workload Storage. |
| [storage/public_access.py](../../api/services/storage/public_access.py) | Local-debug IP-allowlist toggle. `CONTAINER_APP_NAME` guard so deployed apps cannot flip Storage open. |
| [network.py](../../api/services/network.py) | VNet/subnet/NSG provisioning for workload network. |
| [passwords.py](../../api/services/passwords.py) | `generate_admin_password()` for VM creation paths (legacy, kept for tests). |

### BLAST domain logic

| File | Purpose |
|------|---------|
| [auto_warmup_reconcile.py](../../api/services/auto_warmup_reconcile.py) | Auto warmup reconcile policy, workload-node readiness gate, and Redis inflight dedupe. |
| [blast/config.py](../../api/services/blast/config.py) | `generate_config()` — ElasticBLAST YAML composer. |
| [blast/db_metadata.py](../../api/services/blast/db_metadata.py) | DB name normalisation + metadata resolution. |
| [blast/job_state.py](../../api/services/blast/job_state.py) | BLAST job projection, external OpenAPI context/cache, Table sync, file preview, and read authorization helpers re-exported by `_blast_shared.py`. |
| [blast/oracles.py](../../api/services/blast/oracles.py) | Tie-order + DB-order oracle upload to Storage. |
| [blast/result_analytics.py](../../api/services/blast/result_analytics.py) | Result blob validation, hit annotation, filtering/sorting, subject rollups, taxonomy rollups. |
| [blast/results_parser.py](../../api/services/blast/results_parser.py) | XML/tabular parser + hit aggregation. |
| [blast/submit_payload.py](../../api/services/blast/submit_payload.py) | Submit body normalization, option extraction, inline query upload, Web BLAST `searchsp` defaulting. |
| [blast/task_config.py](../../api/services/blast/task_config.py) | BLAST Celery task config URL/path normalization and node-warmup submit readiness policy. |
| [blast/capacity_gate.py](../../api/services/blast/capacity_gate.py) | Optional Redis-backed capacity decision and reservation model behind `BLAST_GATE_ENABLED`. |
| [blast/coordination.py](../../api/services/blast/coordination.py) | Cross-path submit coordination settings and invariants; `BLAST_COORD_BACKEND=k8s` takes precedence over the Redis capacity gate. |
| [db/order_oracle.py](../../api/services/db/order_oracle.py) | `DbOrderOracleJobPlan` builder. |
| [db/sharding.py](../../api/services/db/sharding.py) | `ShardLayout` + `read_blastdb_stats` for sharded DBs. |
| [external_blast.py](../../api/services/external_blast.py) | Streaming downloads from external BLAST sources. |
| [query_grouping.py](../../api/services/query_grouping.py) | `QueryGroupPlan` + split planning. |
| [query_metadata.py](../../api/services/query_metadata.py) | FASTA parser → `QueryRecordSummary`. |
| [sharding_precision.py](../../api/services/sharding_precision.py) | Outfmt merge compatibility + `PrecisionReport`. |
| [warmup/jobs.py](../../api/services/warmup/jobs.py) | `WarmupJobPlan` + warmup ConfigMap builder. |
| [warmup/planner.py](../../api/services/warmup/planner.py) | `compute_warmup_feasibility` + SKU upgrade recs. |
| [warmup/task_planning.py](../../api/services/warmup/task_planning.py) | Storage warmup task shard selection, molecule type, and ELB image planning helpers. |
| [auto_warmup.py](../../api/services/auto_warmup.py) | Auto-warmup preferences (Table-backed). |
| [web_blast_searchsp.py](../../api/services/web_blast_searchsp.py) | NCBI Web BLAST `searchsp` defaults (see [BLAST SearchSP Discovery](../research/blast-searchsp-discovery.md)). |
| [taxonomy/](../../api/services/taxonomy/) | NCBI taxonomy search, detail, sibling lookup, XML parsing, and caches. |
| [taxonomy_image.py](../../api/services/taxonomy_image.py) | Taxonomy thumbnail fetcher + cache. |

### Monitoring + state

| File | Purpose |
|------|---------|
| [monitoring/](../../api/services/monitoring/) | Compatibility façade and focused AKS, ACR, Storage, VM, and provisioning modules. **Use `k8s_*` helpers — NEVER Azure Run Command** (charter §11). |
| [k8s/client.py](../../api/services/k8s/client.py) | AKS kubeconfig credential cache and direct Kubernetes `requests.Session` setup. |
| [k8s/nodes.py](../../api/services/k8s/nodes.py) | Kubernetes node list projection and Ready warmup-node selection. |
| [k8s/monitoring.py](../../api/services/k8s/monitoring.py) | Direct K8s API facade for warmup, BLAST job, pod, service, metric, and event helpers. |
| [monitor_cache.py](../../api/services/monitor_cache.py) | Cached snapshot for dashboard polling. |
| [state_repo.py](../../api/services/state_repo.py) | `JobStateRepository` — Azure Tables (`jobstate`/`jobhistory`) + local JSON fallback (repo-root anchored, `fcntl.flock`). |
| [request_metrics.py](../../api/services/request_metrics.py) | Per-sidecar request percentiles. |
| [sidecar_metrics.py](../../api/services/sidecar_metrics.py) | Health thresholds + Redis CPU sampler. |
| [event_emitter.py](../../api/services/event_emitter.py) | Redis-backed event channel (dashboard SSE). |
| [aks_skus.py](../../api/services/aks_skus.py) | `SkuCatalogEntry` + allowlist. |
| [image_tags.py](../../api/services/image_tags.py) | `IMAGE_TAGS` dict — cross-check vs `dotnetpower/elastic-blast-azure` `src/elastic_blast/constants.py`. |
| [openapi/runtime.py](../../api/services/openapi/runtime.py) | OpenAPI base-url store (Redis). |
| [service_bus.py](../../api/services/service_bus.py) + `service_bus_*` modules | Optional external request queue, completion outbox/events, health, management, preview, telemetry, and tracking. Redis remains Celery's internal broker. |
| [upgrade/](../../api/services/upgrade/) | Self-upgrade git workspace, image build, Container App revision/traffic, state, and rollback helpers. |
| [cgroup_reporter.py](../../api/services/cgroup_reporter.py) | Container resource snapshot (CPU/mem). |

### Terminal exec channel

| File | Purpose |
|------|---------|
| [terminal_exec.py](../../api/services/terminal_exec.py) | HTTP client → terminal sidecar `127.0.0.1:7682`. `run()` / `stream()` / `healthz()`. Bearer = `EXEC_TOKEN` secret. argv[0] allowlist enforced server-side. |
| [sanitise.py](../../api/services/sanitise.py) | `sanitise()` — strips tokens/SAS/subscription IDs from `run()` output before HTTP boundary. |

---

## 3. Celery tasks (api/tasks/)

Eager-imported by `api/main.py` to defeat the `@shared_task` current-app
trap (see repo memory). Queue names map directly to module names.

| Module | Tasks | Queue |
|--------|-------|-------|
| [tasks/acr/](../../api/tasks/acr/) | `build_images`, `_schedule_acr_build` | `acr` |
| [tasks/azure/](../../api/tasks/azure/) | `diag_noop` + Azure provisioning helpers | `azure` |
| [tasks/blast/](../../api/tasks/blast/) | BLAST submit/delete + status sync (uses `terminal_exec`) | `blast` |
| [tasks/blast_artifacts.py](../../api/tasks/blast_artifacts.py) | Terminal artifact reconciliation and cleanup | `blast-artifacts` |
| [tasks/openapi/](../../api/tasks/openapi/) | Workload Identity bootstrap, OpenAPI deployment | `azure` |
| [tasks/servicebus/](../../api/tasks/servicebus/) | Request drain/retry, transition outbox publication, DLQ reconciliation, health | `servicebus` |
| [tasks/storage/](../../api/tasks/storage/) | Interactive DB prep + warmup orchestration; periodic repair tasks | `storage` / `reconcile` |
| [tasks/upgrade/](../../api/tasks/upgrade/) | Upgrade checks, build/deploy, rollback, revision and history reconciliation | `default` / `reconcile` |
| [tasks/webhooks.py](../../api/tasks/webhooks.py) | Terminal-job webhook dispatch | `reconcile` |

The worker sidecar runs four isolated parents: `worker-main` consumes
`default,acr,azure,blast,storage`; `worker-reconcile` consumes `reconcile`;
`worker-servicebus` consumes `servicebus`; and `worker-artifacts` consumes
`blast-artifacts`. An unexpected `celery` queue still indicates the
`@shared_task` current-app routing trap.

---

## 4. Frontend top-level pages (web/src/pages/)

| Page | File | Backend it talks to |
|------|------|----------------------|
| Dashboard | [pages/Dashboard/Dashboard.tsx](../../web/src/pages/Dashboard/Dashboard.tsx) | `/api/monitor/*` (polled via TanStack Query) |
| BLAST Submit | [pages/BlastSubmit.tsx](../../web/src/pages/BlastSubmit.tsx) | `/api/v1/elastic-blast/submit`, `/api/storage/prepare-db` |
| BLAST Jobs | [pages/BlastJobs/](../../web/src/pages/BlastJobs/) | `/api/monitor/jobs`, `/api/v1/elastic-blast/jobs` |
| BLAST Results | [pages/BlastResults.tsx](../../web/src/pages/BlastResults.tsx) | `/api/v1/elastic-blast/jobs/{id}/files/{file_id}` (streamed) |
| BLAST Analytics | [pages/BlastAnalytics.tsx](../../web/src/pages/BlastAnalytics.tsx) | `/api/monitor/metrics`, `/api/monitor/sidecar-requests` |
| Sequence Detail | [pages/sequence/](../../web/src/pages/sequence/) | NCBI metadata plus handoff to New Search |
| Browser Terminal | [pages/RemoteTerminal.tsx](../../web/src/pages/RemoteTerminal.tsx) | `/api/terminal/ticket` → WS `/api/terminal/ws` |
| Live Wall | [pages/Monitor/](../../web/src/pages/Monitor/) | Sidecar/log SSE and monitor snapshots; preview-gated |
| Database Builder | [pages/DatabaseBuilder/](../../web/src/pages/DatabaseBuilder/) | DB catalogue, prepare/update, warmup, and oracle preferences; preview-gated |
| Service Bus Playground | [pages/ServiceBusPlayground.tsx](../../web/src/pages/ServiceBusPlayground.tsx) | `/api/settings/service-bus/*`; preview-gated |
| API Reference | [pages/ApiReference.tsx](../../web/src/pages/ApiReference.tsx) | static OpenAPI from backend |
| Upgrade | [pages/UpgradePage.tsx](../../web/src/pages/UpgradePage.tsx) | `/api/upgrade/*` |
| Diagnostics | [pages/diagnostics/](../../web/src/pages/diagnostics/) | `/api/diagnostics/{category}` |
| Tools | [pages/ToolsPage.tsx](../../web/src/pages/ToolsPage.tsx) | mixed |
| Sign In | [pages/SignIn.tsx](../../web/src/pages/SignIn.tsx) | MSAL only |

All `/api/*` calls go through generated typed clients in
[web/src/api/](../../web/src/api/) — no raw `fetch` in components.

---

## 5. Import rules (tripwires)

* Routes / tasks → `api.services.*` ONLY. Never `azure.mgmt.*` / `azure.identity` directly.
* All Python imports start with `api.…`. Bare `from services.X` / `from auth.X` / `from _http_utils` break (no sys.path bridge anymore).
* No `azure.functions` imports — not in `pyproject.toml`; loads in dev (system-wide) and crashes in the container image.
* Never `from api.routes import stubs` then call its task functions — stubs are HTTP 503 only.
* Never reach for `ManagedClusters.begin_run_command` / `VirtualMachines.begin_run_command`. Use `k8s_*` helpers or `terminal_exec.run()`.
* Never `generate_blob_sas` / `get_user_delegation_key` / `BlobSasPermissions`. Stream through the api sidecar.
* `ttyd` binds `127.0.0.1` only. Public ingress targets `:8080` (api sidecar).

---

## 6. When this map is wrong

Update it in the same change that altered route prefixes, service responsibilities,
or task names. Stale maps cost more than the change itself.
