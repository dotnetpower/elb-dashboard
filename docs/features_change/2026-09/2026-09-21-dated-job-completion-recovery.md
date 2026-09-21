---
title: Recover completed jobs under dated result paths
description: Runtime identity discovery now supports date-tiered Storage prefixes, and reconciliation trusts durable SUCCESS markers over stale submit-task state.
tags:
  - blast
  - operate
  - architecture
---

# Recover completed jobs under dated result paths

## Motivation

A dashboard-submitted sharded search completed in AKS and wrote
`merged_results.out.gz`, `merge-report.json`, and `metadata/SUCCESS.txt`, but the
UI remained `Running`. The runtime identity lookup split the full blob path at a
fixed slash index. With the dated layout
`YYYY/MM/DD/<dashboard-job>/job-<runtime-id>/...`, that index resolved to the
month instead of the ElasticBLAST runtime ID.

The submit Celery task had already returned `SUCCESS` with
`status=running`. Reconciliation treated that result as indefinitely active and
skipped the durable completion check even after the Kubernetes resources had
been cleaned up.

## User-Facing Change

Completed searches with date-tiered result paths now transition to `Completed`
instead of remaining indefinitely `Running`. Existing affected rows are repaired
by either job-detail refresh or the periodic reconciler: both backfill the
runtime identity and use the cluster finalizer's durable `SUCCESS.txt` marker
as completion ground truth.

## API / IaC Diff Summary

- Runtime IDs are extracted from the direct child segment relative to the
  canonical stored results prefix, supporting both flat and dated layouts.
- Submit-time and read-side runtime identity discovery share the same helper.
- When a completed submit task still reports `running`, reconciliation attempts
  runtime-ID backfill and checks the scoped durable success marker after live
  K8s/external refreshes cannot provide a terminal state.
- A direct job-detail refresh applies the same recovery when K8s reports
  `creating` with zero Jobs/Pods after finalizer cleanup, so an affected job
  does not wait behind the reconciler's bounded active-row scan.
- No HTTP schema, Azure resource, queue name, or Storage layout changed.

## Validation Evidence

- The affected job had no remaining Kubernetes resources, but its dated results
  prefix contained a canonical runtime ID, merged output, merge report, and
  `metadata/SUCCESS.txt`; aggregate analytics returned 100 hits.
- Focused prefix/discovery/reconciliation tests passed.
- Full backend suite passed: 6,142 with 4 fixture-dependent skips.
- Ruff, the mypy debt ratchet, the 242-operation OpenAPI contract, generated
  TypeScript API types, and strict MkDocs build passed.
