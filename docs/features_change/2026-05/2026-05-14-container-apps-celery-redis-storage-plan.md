# 2026-05-14 — Container Apps migration plan: Celery + self-hosted Redis + Storage state

## Motivation

The original Container Apps migration plan (`docs/container-apps-migration.md`)
proposed Service Bus as the queue and Cosmos DB / PostgreSQL as the state store.
Re-evaluating the workload shape (append-mostly, single-key lookups, modest
throughput, single broker user) showed that those managed services are
over-scoped for this control plane. The user requested:

- Use **Celery** for both task dispatch and scheduling.
- Use a **self-installed Redis** (single VM), not Azure Cache for Redis.
- Do not provision a managed database; persist state to **Azure Storage**.
- Drop **Service Bus** entirely.

This change rewrites the migration plan to reflect those decisions and produces
an authoritative resource list that future Bicep modules will track.

## User-facing change

None at runtime (this is a planning document update). Operators and reviewers
now see a single, consistent target architecture instead of mixed references to
Service Bus / Cosmos / PostgreSQL.

## Architecture diff summary

| Area | Before | After |
|------|--------|-------|
| Queue / broker | Azure Service Bus Standard | Self-hosted Redis 7 on `vm-elb-redis` (single VM, AOF on, NSG-restricted to `snet-containerapps`) |
| Worker model | Service Bus message consumers | Celery workers (`ca-control-worker`) |
| Scheduler | Container Apps Jobs *or* Service Bus scheduled messages | Celery beat singleton (`ca-control-beat`, `minReplicas: 1`, `maxReplicas: 1`) |
| State store | Cosmos DB *or* PostgreSQL | Azure Storage table (`job-state`, `job-history`) + append blobs (`audit`, `dead-letter`, `job-payloads`) + JSON blob (`schedules`) |
| Redis (cache/broker) | (implicit Service Bus, no Redis) | Self-installed Redis 7 on a VM. Password from Key Vault, AOF backup nightly to platform Storage. |
| Private DNS zones | vault, blob, queue, servicebus, acr, cosmos/postgres | vault, blob, **table**, acr (no servicebus, no cosmos/postgres) |
| Subnets | containerapps, private-endpoints, aks, terminal, bastion | + new `snet-redis` for the broker VM |
| Identities | api / worker / terminal / openapi | + new `id-elb-redis` (Key Vault Secrets User + Storage Blob Data Contributor for AOF backup) |
| Bicep modules added | `serviceBus.bicep`, `stateStore.bicep` | `redisVm.bicep`, `storageState.bicep` |

A new "Resources to Create" table in the doc enumerates every resource the new
architecture provisions, marked `New` or `Existing`, so cost and IaC reviewers
have one source of truth.

## Files changed

- `docs/container-apps-migration.md` — comprehensive rewrite of Decision
  Summary, Resources to Create (new section), Target Architecture, Component
  Plan, Service Boundaries (added `ca-control-beat` and Redis VM sections),
  Command and State Model (Celery + Storage shape), Route Migration Map,
  Networking Plan, Identity table, Storage Plan, Infrastructure Changes
  (Bicep module list), Phases 2 and 4, Validation Plan, Cutover Checklist,
  Rollback, Risks, Open Decisions, First Implementation Slice.
- `README.md` — Architecture Planning bullet now reflects Celery + self-hosted
  Redis + Azure Storage (no Service Bus, no managed DB).

## Validation evidence

Documentation-only change. Verified with:

```bash
grep -n "Service Bus\|servicebus\|Cosmos\|PostgreSQL\|state store" \
  docs/container-apps-migration.md
```

All remaining matches are either explicit "removed from prior plan" entries,
"future migration path" notes (Storage → Cosmos is straightforward), or text
that names what is *not* being created. No active recommendation in the doc
points at Service Bus, Cosmos DB, PostgreSQL, or managed Redis.

## Follow-up tickets to file

1. Add `infra/modules/redisVm.bicep` (Ubuntu 22.04, Redis 7 cloud-init,
   `id-elb-redis` user-assigned MI, NSG locked to `snet-containerapps`).
2. Add `infra/modules/storageState.bicep` (table `job-state`, table
   `job-history`, blob containers `audit`, `dead-letter`, `job-payloads`,
   `schedules`, lifecycle policies).
3. Add `infra/modules/containerApps.bicep` covering `ca-control-api`,
   `ca-control-worker`, `ca-control-beat` with the right replica bounds.
4. Add Celery skeleton under `api_app/` (or wherever the FastAPI port lands)
   with a Storage-backed beat scheduler implementation.
