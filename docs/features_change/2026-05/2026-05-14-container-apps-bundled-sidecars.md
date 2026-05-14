# 2026-05-14 — Cost-minimised Container Apps topology: one app, four sidecars

## Motivation

The 2026-05-14 Celery + self-hosted Redis plan still provisioned three Container
Apps (`ca-control-api`, `ca-control-worker`, `ca-control-beat`) plus a dedicated
Redis VM. The user asked whether everything could be collapsed into a single
Container App to minimise cost.

Container Apps supports multiple containers per revision (sidecar pattern),
sharing one network namespace and lifecycle. For this low-traffic, single-tenant
control plane, a single Container App with `api`, `worker`, `beat`, and `redis`
sidecars is operationally simpler and substantially cheaper than four separate
billable units.

## User-facing change

None at runtime (planning document update).

## Architecture diff summary

| Area | Previous (multi-app) | Now (bundled sidecars) |
|------|----------------------|------------------------|
| Container Apps | `ca-control-api`, `ca-control-worker`, `ca-control-beat` (3 apps) | `ca-elb-control` (1 app) with four containers: `api`, `worker`, `beat`, `redis` |
| Redis broker | Self-hosted on `vm-elb-redis` (Linux VM, Standard_B2s) | `redis:7-alpine` sidecar in the same revision, listening on `127.0.0.1:6379` |
| Redis persistence | Local AOF + nightly backup blob from VM | AOF written to an Azure Files share `redis-data` mounted at `/data` |
| Subnets | `snet-containerapps` + dedicated `snet-redis` for the broker VM | `snet-containerapps` only |
| NSG for Redis | NSG locked to ingress from `snet-containerapps` | None — Redis is loopback-bound inside the replica |
| Identities | `id-elb-control-api`, `id-elb-control-worker`, `id-elb-redis` | One shared `id-elb-control` for all four sidecars |
| Replica scaling | Per-app, with `worker` scalable | Whole app pinned to `minReplicas: 1, maxReplicas: 1` (forced by beat singleton + Redis state locality) |
| Private DNS zones | vault, blob, table, acr | + `privatelink.file.core.windows.net` (needed for the Redis AOF mount once Storage public access is disabled) |
| Bicep modules | `redisVm.bicep`, separate Container Apps module | `containerAppControl.bicep` (single app + sidecars + volume mount); `redisVm.bicep` removed |

## Trade-offs (now explicit in the doc)

1. **No horizontal scale-out.** The whole stack is one replica because beat is
   a singleton and Redis state must stay co-located. The doc records the
   escalation path: split `beat` + `redis` into a separate app first if scale-
   out is ever needed, then move `worker`.
2. **Whole-stack restart on any image change.** Acceptable because the API
   surface is small. Beat's reconciler re-dispatches in-flight tasks after
   restart.
3. **In-flight task loss on restart is bounded by AOF on Azure Files** plus the
   reconciler that watches Storage state for `running` rows whose worker
   disappeared.
4. **Shared MI over-grants the API sidecar.** Mutating ARM operations only run
   inside Celery task handlers; documented as an explicit compromise. Future
   split into per-sidecar Container Apps would restore per-process identities.

## Files changed

- `docs/container-apps-migration.md` — Decision Summary, Resources to Create,
  Target Architecture diagram, Component Plan, Service Boundaries (collapsed
  three sections into four sidecar sections), Route Migration Map, Networking
  Plan (removed `snet-redis`, added `privatelink.file.core.windows.net`),
  Identity table (single shared identity), Storage Plan (added `redis-data`
  Azure Files share), Infrastructure Changes (Bicep modules), Phases 2 and 4,
  Validation Plan, Cutover Checklist, Rollback, Risks, Open Decisions
  (added `Topology` row), First Implementation Slice.
- `README.md` — Architecture Planning bullet rewritten for the bundled
  topology.

## Validation evidence

Documentation-only change. Verified with:

```bash
grep -n "vm-elb-redis\|snet-redis\|id-elb-redis\|ca-control-" \
  docs/container-apps-migration.md
```

All remaining matches are inside the "Explicitly removed from the prior plan"
table — no active recommendation references the deleted resources.

## Resource count delta

Removed (vs the 2026-05-14 multi-app plan):

- 1 × Linux VM (`vm-elb-redis`) including OS disk, NIC, and public IP if any
- 1 × subnet (`snet-redis`) and its NSG
- 1 × user-assigned MI (`id-elb-redis`)
- 1 × Key Vault secret for the Redis password (no longer needed; loopback)
- 1 × nightly backup orchestration target (Redis VM AOF copy)
- 2 × Container App revisions (3 apps → 1 app)

Added:

- 1 × Azure Files share `redis-data` on the platform Storage account (no new
  account required)
- 1 × additional private DNS zone `privatelink.file.core.windows.net`
