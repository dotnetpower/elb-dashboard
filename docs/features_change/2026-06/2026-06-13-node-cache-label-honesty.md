---
title: Node Resources cache bar — honest "reclaimable file cache" labelling
description: The teal node-memory segment no longer claims to be exactly the warm BLAST DB; it now reads as node-wide reclaimable file cache that the DB dominates, with an active-search caveat.
tags:
  - ui
  - blast
---

# Node Resources cache bar: honest labelling

## Motivation

The Cluster Diagnostics → **Node Resources** memory bar renders a teal overlay
computed in [api/services/k8s/node_cache.py](../../../api/services/k8s/node_cache.py)
as `usageBytes - workingSetBytes` from the kubelet `/stats/summary` proxy. That
difference is the **node-wide reclaimable (inactive) file page cache** — it is
*dominated* by a warmed BLAST DB but is not a DB-scoped measurement. It also
includes container image layers, logs, the SSD-staged DB copy, and any other
file I/O, and it reflects the **uncompressed resident** size, which is larger
than the DB's compressed catalogue/download size.

The legend and tooltip nonetheless asserted "File cache (warm BLAST DB)" and
"cache holds the warm BLAST DB", presenting a node-wide upper bound as if it were
the exact DB footprint. A reviewer reasonably noticed the cache often looks
larger than the data they warmed — a correct observation that the label was
hiding rather than explaining. A second subtlety: during an **active search**
the DB pages are promoted to the working set (purple) and leave the teal
segment, so the teal number is only DB-representative while the node is idle.

## User-facing change

- Legend swatch: `File cache (warm BLAST DB · reclaimable)` →
  `Reclaimable file cache (mostly warm DB)`.
- Per-node memory bar tooltip: now states the teal value is node-wide page cache
  dominated by the warmed BLAST DB volumes (also images/logs/other file I/O), so
  it can exceed the DB's catalogue size, and that during an active search the DB
  pages count as working set instead.

No data-flow, endpoint, or computation change — purely label/doc honesty. The
two-colour overlay and the `usageBytes - workingSetBytes` derivation are
unchanged.

## API / IaC diff summary

- [web/src/components/ClusterDiagnostics/NodeResourcesSection.tsx](../../../web/src/components/ClusterDiagnostics/NodeResourcesSection.tsx)
  — legend label + bar tooltip wording.
- [web/src/api/monitoring.ts](../../../web/src/api/monitoring.ts) — `cache_ki`
  doc comment clarified (node-wide, uncompressed-resident, active-search caveat).
- No backend change; no `K8sNodeMetrics` field added or removed.

## Validation evidence

- `cd web && npm run build` — green (type-check + bundle).
- Grep: no remaining references to the old legend/tooltip strings and no test or
  mock asserted them, so the wording change is contract-safe.

## Recommended follow-up (not in this change)

To let users compare *expected* vs *observed* DB residency directly, surface the
warmup-plan's per-node expected resident estimate next to the teal bar
(`expected DB ≈ X GiB`). That requires plumbing the warmup-plan residency into
the `/aks/top-nodes` payload (new backend surface) and is intentionally deferred
to keep this change label-only and reversible.
