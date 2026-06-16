---
title: Recover blastn failure detail behind coarse Kubernetes pod-state errors
description: When the sibling OpenAPI service reports a failed BLAST job with a non-actionable Kubernetes pod-state string (CrashLoopBackOff, OOMKilled, ...), the dashboard now still recovers the authoritative cluster-side FAILURE.txt detail instead of surfacing the bare symptom.
tags:
  - blast
  - operate
---

# Recover blastn failure detail behind coarse Kubernetes pod-state errors

## Motivation

A live full-cycle E2E on 2026-06-16 (start `elb-cluster-01` → Service Bus submit
→ stop) surfaced a real reporting gap. A `16S_ribosomal_RNA` search submitted
through Service Bus failed with the dashboard showing only:

```
BLAST_FAILED: pod blastn-batch-16s-ribosomal-rna-job-000-… container blast is CrashLoopBackOff
```

The underlying k8s Job had `BackoffLimitExceeded` — the real cause was that the
DB was not re-staged on the *fresh* blastpool nodes that AKS provisioned after
the cluster stop/start (the previous warmup landed on nodes that were replaced;
only the auto-warmup-registered `core_nt` got re-warmed, not the one-off 16S).
That root cause is a known scheduling/warmup architecture gap, but the **failure
reporting** made it worse: the message named the Kubernetes symptom
(`CrashLoopBackOff`) and stopped, so the operator could not tell *why* the search
failed.

Root cause in the dashboard projection: `_enrich_external_failure_detail`
recovers the authoritative cluster-side blastn detail (`metadata/FAILURE.txt` +
`logs/BLAST_RUNTIME-NNN.out`) only when the sibling error is empty or one of a
small **exact-match** set of generic strings. A Kubernetes pod-state string like
`… container blast is CrashLoopBackOff` is not in that set, so it was treated as
"specific" and the richer FAILURE.txt detail was skipped — even though the
pod-state string is just as non-actionable as the generic ones.

## User-facing change

A failed external/OpenAPI BLAST job whose sibling error is a coarse Kubernetes
pod/container-state string now shows the recovered blastn cause (e.g. "BLAST
search exited with code 2: …") on the Run details page when a `FAILURE.txt` /
`BLAST_RUNTIME` artifact is readable from Storage. When no such artifact exists,
the original message is preserved (no regression). List views are unchanged (the
enrichment stays gated to the detail render to avoid a Storage read per row).

## API / IaC diff summary

* `api/services/blast/external_job_projection.py`:
  * New `_EXTERNAL_COARSE_K8S_FAILURE_SUBSTRINGS` + `_is_coarse_k8s_failure()` —
    case-insensitive substring match for non-actionable pod/container-state
    strings (`crashloopbackoff`, `imagepullbackoff`, `errimagepull`,
    `oomkilled`, `is not ready`, `is not running`, `backoff limit`,
    `backofflimitexceeded`, `container blast is`, `pod is not`).
  * `_enrich_external_failure_detail` now also treats a coarse-k8s message as
    non-specific, so the FAILURE.txt recovery fires for it. A genuinely specific
    sibling error (e.g. a real blastn message) is still left untouched, and a
    missing artifact still preserves the original message.

## Validation evidence

* Live E2E (moonchoi prod, `elb-cluster-01`): Service Bus send → drain →
  OpenAPI submit → k8s BLAST run observed end-to-end; the control plane reported
  the runtime failure honestly (status=`failed`, no 5xx, App Insights clean for
  the 30-min window). Cluster started and stopped cleanly.
* New `api/tests/test_local_to_blast_job.py::test_local_to_blast_job_external_crashloop_recovers_runtime_detail`
  — a `CrashLoopBackOff` sibling error recovers the FAILURE.txt detail on the
  detail view. Existing enrichment + list-view-skip tests stay green.
* `uv run pytest -q api/tests` → 3841 passed, 3 skipped.
* `uv run ruff check` on touched files → clean.

## Out of scope (reported, not fixed here)

* The underlying scheduling gap — a one-off-warmed DB (not registered for
  auto-warmup) is not re-staged on fresh nodes after a cluster stop/start, so a
  search for it fails until re-warmed. The DB catalog still reports such a DB as
  `ready` (Storage files present) even though it is not staged on the node the
  search lands on. Addressing that (re-warm on submit when stale, node-affinity
  pinning, or a "staged on node" readiness signal) is a larger
  scheduling/warmup change tracked separately.
