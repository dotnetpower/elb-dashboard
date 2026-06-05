---
title: BLAST database listing performance — kill the per-volume .njs N+1 and reuse the catalogue cache
description: GET /api/blast/databases and /databases/check-updates were slow
  (p50 9s / p95 21-27s in App Insights) because the blast-db enumeration
  downloaded one .njs metadata blob per DB volume and check-updates bypassed the
  shared catalogue cache. The enumeration now downloads one .njs per database,
  check-updates reuses the cached lister, and the Upgrade page only probes the
  escape-hatch when a rollback snapshot exists.
tags:
  - blast
  - operate
---

# BLAST database listing performance + upgrade escape-hatch probe gating

## Motivation
App Insights (production `appi-elb-dashboard`) showed a clear, data-backed
bottleneck and one benign-but-noisy failed request. Over a dogfood window:

- `GET /api/blast/databases` — p50 **9.0s**, p95 **21.7s**.
- `GET /api/blast/databases/check-updates` — p95 **26.6s**.
- Within one databases operation: `BlobClient.download_blob` fired **629** times
  and `ContainerOperations.list_blob_flat_segment` accumulated **423s** — the
  signature of an N+1 over `blast-db` blobs.
- `GET /api/upgrade/escape-hatch` — the only failed request (**404 × 3**), a
  benign probe against a deployment with no rollback snapshot yet.

Exceptions: **0**. Error-level traces: **0**. The codebase is otherwise healthy;
these were the only actionable signals.

## Root cause
1. `api/services/storage/database_list.py::list_databases` downloaded **every
   volume's `.njs`** sidecar (`core_nt.00.njs … core_nt.NN.njs`) while keeping
   only the last lexicographic one. A large multi-volume DB therefore paid
   hundreds of wasted blob downloads per listing.
2. `api/routes/blast/databases.py::blast_databases_check_updates` called
   `storage.data.list_databases` **directly**, bypassing the 300s-TTL
   `database_catalog_cache` that `GET /api/blast/databases` already uses — so it
   re-paid the full enumeration on every New Search load.
3. `web/src/pages/UpgradePage.tsx` probed `/upgrade/escape-hatch` on every
   refresh even when `rollback_target` was empty, where the route correctly
   returns 404 (locked by `test_escape_hatch_404_without_snapshot`).

## User-facing change
- New Search / Database Builder load materially faster — the `.njs` work drops
  from O(volumes) to O(databases) downloads, and check-updates no longer
  re-enumerates Storage on a warm cache.
- No behaviour change to the returned catalogue payload: the "last volume wins"
  `.njs` content contract and `file_count` (which counts `.njs` as a recognised
  BLAST extension) are preserved.
- The Upgrade page no longer emits a 404 escape-hatch probe on never-upgraded
  deployments; escape-hatch / rollback-preflight are fetched only once a
  rollback snapshot exists.

## API / IaC diff summary
- No API surface, response shape, or IaC change.
- `api/services/storage/database_list.py` — record each base's `.njs` blob name
  during enumeration (last wins) and download exactly one per **registered** DB
  after the loop; a `.njs` whose base was filtered out (shards / staging) is
  never downloaded.
- `api/routes/blast/databases.py` — `check-updates` now calls
  `database_catalog_cache.list_databases_cached` instead of the raw lister.
- `web/src/pages/UpgradePage.tsx` — gate the escape-hatch + rollback-preflight
  probes on a non-empty `status.rollback_target`.

## Verified non-changes (App Insights false positives)
- `/api/aks/autostop/status` (p95 7.5s) already has a two-tier L1+Redis cache;
  `/api/monitor/acr` and `/api/monitor/aks` already use `cached_snapshot`. High
  p95 is cold-miss cost, not a missing cache — no change made.
- Periodic reconcilers (`reconcile_auto_warmup`, `reconcile_public_https`)
  already early-exit on no/disabled preferences; adding idle backoff would hurt
  warmup responsiveness — no change made.
- `GET /api/monitor/sidecars/events` InProc 776884ms is SSE stream lifetime, not
  latency — no change made.

## Validation evidence
- `uv run pytest -q api/tests` — **2902 passed, 3 skipped**.
- New regression tests in `api/tests/test_storage_data.py`:
  `test_list_databases_downloads_one_njs_per_multivolume_db` (asserts exactly one
  `.njs` download for a 12-volume DB + "last wins" content) and
  `test_list_databases_skips_njs_for_filtered_base`.
- `uv run ruff check` on the changed backend files — clean.
- `cd web && npx tsc --noEmit` + `npm run build` — succeed.
- `npx vitest run src/api/upgrade.test.ts` — 16/16 passing.
