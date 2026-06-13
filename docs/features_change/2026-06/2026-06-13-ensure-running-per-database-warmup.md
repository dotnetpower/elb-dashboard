---
title: ensure-running waits for per-database warmup, not just node readiness
description: >-
  POST /api/aks/openapi/ensure-running now reports status='ready' only when every
  configured warmup database finished its node-local warmup Job, not merely when
  the warmup nodes are Kubernetes-Ready.
tags:
  - blast
  - operate
---

# ensure-running waits for per-database warmup

## Motivation

The wake-on-request gate `POST /api/aks/openapi/ensure-running` reported
`status='ready'` as soon as the warmup **nodes** were Kubernetes-Ready. Node
readiness only proves the K8s nodes are up — it does **not** prove that every
configured database finished copying onto the node-local SSD. With `core_nt`
still `Loading` and only `16S_ribosomal_RNA` (ribosome) warmed, the endpoint
returned `ready` / `warmup.phase='ready'`, so a caller submitting then would
silently fall back to the slow on-node DB init — exactly the cold submit this
gate exists to prevent.

## User-facing change

`_evaluate_warmup_phase` now runs in two stages once the cluster is Running with
warmup configured:

1. The existing warmup-node K8s readiness gate (`auto_warmup_ready_gate`). If the
   nodes are not all Ready, the cluster is `warming` (unchanged).
2. **New:** a per-database warmup-Job check (`k8s_warmup_status`). Each configured
   database is classified into three buckets, mirroring the retryable/terminal
   split the BLAST submit gate (`ensure_node_warmup_ready_for_submit`) already
   uses:
   - `Ready` — warm on the node-local SSD.
   - still progressing (`Loading` / `Pending` / `Starting` / `Stale` /
     `Unknown` / missing, or any node still active) → keep `warming` with
     `phase='warming_databases'`.
   - terminal `Failed` with no active node → warmup is **best-effort** here (a
     cause retrying cannot fix, and the reconcile circuit breaker stops
     re-enqueuing it). Blocking `ready` forever would strand the whole cluster
     un-submittable, so the cluster reports `ready` with
     `phase='ready_degraded'` and the failed set surfaced; `/v1/jobs` for that
     database falls back to the slow on-node init.

The `warmup` summary in the response gains additive fields once the nodes are
Ready:

- `databases_total` — number of configured warmup databases.
- `databases_ready` — number that finished warming.
- `pending_databases` — list of `{db, status}` for databases still warming.
- `failed_databases` — list of `{db, status}` for databases that terminally
  failed (best-effort, served degraded).

`ready` confirms each database is **warm**, NOT that it is the latest NCBI
generation — the per-submit gate owns the generation / `source_version`
comparison, so a brand-new snapshot may still be re-warming when `ready` is
reported.

## API / IaC diff summary

- `api/services/aks/ensure_running.py`: `_evaluate_warmup_phase` split into a
  node stage + a new `_evaluate_database_warmup` helper; `warmup_summary` gains
  the four database fields; `ready` is gated on every database being warm or
  terminally Failed (best-effort `ready_degraded`).
- `web/src/pages/apiReference/coreEndpoints.ts`: API reference doc + example
  updated to describe the per-database readiness, the `warming_databases` /
  `ready_degraded` phases, the `failed_databases` field, and the generation
  caveat.
- No IaC change.

## Validation evidence

- `uv run pytest -q api/tests/test_aks_ensure_running.py` — 19 passed, including
  the new regression guards: `..._database_still_loading_is_warming`,
  `..._database_missing_is_warming`, `..._terminally_failed_is_ready_degraded`,
  `..._failed_database_with_active_node_keeps_warming`, and
  `..._stale_database_keeps_warming`.
- `uv run pytest -q api/tests` — 3471 passed, 3 skipped (one unrelated flaky
  timing test in `test_cgroup_reporter.py` passes in isolation).
- `uv run ruff check api/services/aks/ensure_running.py
  api/tests/test_aks_ensure_running.py` — clean.
- `cd web && npx vitest run src/pages/apiReference/coreEndpoints.test.ts` —
  4 passed; `npm run build` — succeeded.
