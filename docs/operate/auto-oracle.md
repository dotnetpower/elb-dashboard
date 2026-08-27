---
title: Auto oracle operations
description: Enable, observe, recover, and retain automatic DB order-oracle builds after Auto warm converges.
tags:
  - operate
  - blast
---

# Auto oracle operations

Auto oracle moves DB order-oracle generation out of the BLAST submit path and into the database lifecycle. After Auto warm reports the exact database generation and shard layout Ready on [Azure Kubernetes Service](https://learn.microsoft.com/azure/aks/what-is-aks), a bounded [Celery](https://docs.celeryq.dev/en/stable/) reconciler creates one order-extraction Job per warmed shard. Normal BLAST submit only attaches pointers to an already-ready oracle.

This automation does **not** calculate or predict Web BLAST `searchsp`. Search-space calibration and order-oracle generation are independent contracts: `searchsp` affects E-values, while the order oracle supplies deterministic accession order for tied merged hits.

## Activation

The feature ships dormant for its first soak window:

| Setting | Initial value | Purpose |
| --- | --- | --- |
| `AUTO_ORACLE_RECONCILE_ENABLED` | `false` | Enables targeted and 120-second recovery reconciliation. |
| `ENFORCE_AUTO_ORACLE_RBAC` | `false` | Enforces current AKS/Storage caller capabilities on preference reads, writes, and background execution. |
| `AUTO_ORACLE_RETENTION_ENABLED` | `false` | Enables the destructive daily 14-day history sweep. |
| `AUTO_ORACLE_MAX_ENQUEUES_PER_TICK` | `2` | Global build admission cap per reconcile pass. |
| `AUTO_ORACLE_MAX_ENQUEUES_PER_STORAGE` | `1` | Prevents concurrent automatic builds from saturating one Storage account. |
| `AUTO_ORACLE_MAX_INSPECTIONS_PER_TICK` | `50` | Bounds preference and Azure API work per pass. |
| `ORACLE_UNCLAIMED_REDELIVERY_SECONDS` | `120` | Reissues a delivery only when no durable execution claim appeared. |

Enable `ENFORCE_AUTO_ORACLE_RBAC=true` first and run the Persona Matrix with the gate forced ON. After that gate soaks, enable reconciliation and leave retention off for at least one full release cycle. The planned RBAC/reconcile flip review date is **2026-09-10**, after a dogfood run confirms owner-RBAC checks, duplicate suppression, progress convergence, and retry exhaustion. Retention requires a separate review after reconciliation soaks with zero incorrect deletions.

The runtime enforces this order: setting `AUTO_ORACLE_RECONCILE_ENABLED=true` while `ENFORCE_AUTO_ORACLE_RBAC` remains false leaves execution dormant and emits a warning. Preferences can still be staged safely before both gates are enabled.

Set the same reconcile value on the `api` and `worker` sidecars. The API value controls immediate targeted triggers after a preference save; the worker value controls execution. Beat emits a recovery tick every 120 seconds even while dormant, and the worker returns a cheap `disabled` result.

## Eligibility

An Auto oracle preference is a versioned shared setting for one cluster, Storage account, and database, not a private per-user record. Any operator who passes the enforced AKS and Storage write checks may update it using the latest opaque `version`; create-only and If-Match writes return a conflict for concurrent/stale editors. The latest successful modifier becomes the identity revalidated before background mutation. Caller identities are never returned by the API or emitted in feature events.

Preference reconciliation reads one indexed 50-row Azure Table page and stores a private `reconcile` continuation cursor after processing. Retention uses an independent `retention` cursor. A crash before cursor persistence safely replays the same page through idempotent oracle claims; large preference fleets therefore do not strand rows beyond a fixed global list cap. The browser lists a selected cluster/Storage scope in 200-row pages and refuses mutation if its bounded page collection is truncated.

At the default 120-second cadence, a full preference scan takes approximately $\lceil N / 50 \rceil \times 120$ seconds. For example, 500 enabled preferences complete a cycle in about 20 minutes. Preference saves and successful Auto warm runs also emit targeted reconciliation, so normal interactive changes do not wait for their cursor position; the page cycle is the recovery path.

A preference is eligible only when all of these remain true on every pass:

- Auto oracle is enabled for the database.
- Auto warm is enabled for the same subscription, AKS resource group, cluster, Storage resource group, Storage account, and database.
- The stored owner still has AKS write and Storage write capability; an indeterminate RBAC lookup fails closed.
- The database copy is complete, no update is active, shard metadata matches the current source generation, and node-local warmup is Ready for every shard.
- No identical ready oracle or other active identity already owns the database.
- Retry backoff is not active or exhausted.

The dashboard only enables **Auto oracle** after **Auto warm** is selected. It shows the current ready oracle independently from an active rebuild, so a new build never hides or replaces a usable pointer until all parts validate and publication succeeds.

## Retry and recovery

Automatic execution failures use durable workload-Storage state, not Redis, as authority. Delays are 5 minutes, 30 minutes, and 2 hours. The third failed attempt sets `retry_exhausted=true`; the dashboard then exposes an explicit **Retry** command that rechecks current permissions and Auto warm before resetting the budget.

A broker restart can lose an unclaimed Redis message. Each dispatch persists its task ID and delivery token before publication. If no execution claim appears for 120 seconds, the reconciler issues a new token. A late or duplicate delivery cannot acquire the Blob ETag-protected execution claim and exits without creating Kubernetes Jobs. Running tasks extend a durable deadline; a later pass terminalizes a hard-crashed owner after that deadline and applies normal backoff.

A second 120-second dispatch reconciler scans at most ten active oracle JobState rows. It recovers manual and automatic accepted runs independently of preference readiness, but only replays a run whose active claim still exists; missing, published, or expired claims never create replacement work. The generic stale-DB-operation fallback uses the oracle hard timeout plus a ten-minute buffer instead of the 24-hour prepare-db window.

## Retention

Oracle artifacts live in private [Azure Blob Storage](https://learn.microsoft.com/azure/storage/blobs/storage-blobs-introduction). The daily sweep is bounded to 20 runs and 200 blobs per database and preserves:

- the current ready run;
- the previous ready run;
- the active build;
- every run younger than 14 days;
- every run with a BLAST reverse reference;
- any malformed, timestamp-less, nonterminal, or incompletely inventoried run.

Deletion uses a create-only GC tombstone. BLAST submit checks that tombstone before and after writing its immutable reverse reference, so a run cannot become newly referenced while its parts are being removed. Parts are deleted before the run status document. A malformed run is preserved while other valid runs may drain; uncertainty in the current or active control document blocks that database's sweep.

The sweep reads at most 50 run documents per database per pass and persists a private Blob continuation cursor, so a page containing only referenced runs cannot starve later unreferenced history. It deletes at most 20 runs and 200 blobs. A completed run's GC marker remains as a permanent tombstone: removing it could let a resolver that selected the run before deletion attach a late reference to missing parts. When a BLAST result is explicitly or age-purged, its reverse reference is removed only if all directory operations avoided errors and the stored path exactly matches the path reconstructed from database, run, and hashed job identity.

## Operational checks

Use the database row to inspect current part count, active rebuild progress, blocked reason, next retry, and exhaustion state. Worker logs emit sanitized database/run classifications without subscription IDs or tokens. During the initial soak, verify that one database update produces exactly one accepted run, that the prior ready run remains usable during rebuild, and that disabling Auto warm moves automation to `auto_warm_disabled` without enqueueing work.
