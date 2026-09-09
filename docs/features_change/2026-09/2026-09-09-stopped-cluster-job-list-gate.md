---
title: Stopped-cluster external job-list gate
description: Avoid querying the external OpenAPI job list when ARM proves the selected AKS cluster is stopped.
tags: [blast, operate]
---

# Stopped-cluster external job-list gate

## Motivation

A scoped `GET /api/blast/jobs` request still resolved the Kubernetes Service and
called the external OpenAPI plane while the selected
[AKS](https://learn.microsoft.com/azure/aks/what-is-aks) cluster was stopped.
The route returned HTTP 200 with local Table rows, but the handled DNS failure
created an Application Insights exception and added avoidable latency. The
subscription-wide discovery path already excluded stopped clusters; only the
explicit-cluster branch lacked the same guard.

## Change

The external-jobs service now consults the existing cached ARM cluster-health
gate before resolving an explicitly selected OpenAPI endpoint. A proven stopped
or missing cluster returns no external target, while local persisted jobs remain
available. Missing scope or an unavailable ARM plane preserves the existing
degrade-open behavior.

No API schema, persisted row, job state, or running-cluster behavior changed.

## Validation

- `uv run pytest -q api/tests/test_external_blast_api.py -k 'external_jobs_target or discover_subscription_clusters or collect_and_sync_external_jobs'` - 5 passed.
- Production evidence: the affected cluster reported `Stopped/Succeeded`; the
  correlated `/api/blast/jobs` request returned HTTP 200 while its unnecessary
  service lookup emitted the handled DNS exception.
