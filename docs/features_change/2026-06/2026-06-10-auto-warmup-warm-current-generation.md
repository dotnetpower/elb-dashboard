# Auto-warmup warms the current generation when an NCBI update is available

## Motivation

Operators reported BLAST database chips stuck at `warm stale` on a Running
cluster, with auto-warmup enabled, that never recovered. Live triage
(`elb-cluster-02`) showed both `16S_ribosomal_RNA` and `core_nt` as warm
`Stale` with `nodes_ready=0`, `force_rewarm_pending=true`, yet the every-2-minute
`reconcile_auto_warmup` returned `status: ready_noop` and enqueued nothing.

Root cause: the per-database reconcile loop skipped any DB whose downloaded
`source_version` differed from the current NCBI `latest-dir` with
`reason="update_required"` and a hard `continue`. NCBI rolls a new daily
snapshot, so within a day of any download the condition is permanently true.
Auto-warmup does not auto-download a new generation (that is an explicit,
hundreds-of-GB prepare-db action), so the DB was left cold/`Stale` forever once
node invalidation (AKS stop/start or node rotation) flipped the warm Jobs stale.

## User-facing change

Auto-warmup now **warms the currently downloaded generation** even when a newer
NCBI snapshot exists, instead of skipping. Node invalidation therefore recovers
automatically: searches keep working on the warm cache the operator already
downloaded. The available update is surfaced as an informational
`update_available` entry on the reconcile result (per cluster) rather than a
`skipped` / `update_required` entry. Auto-downloading the new generation remains
an explicit user action (Storage card → Update).

A DB that is already healthily warm for the current generation is still skipped
(`reason="Ready"`), so there is no re-warm churn — only `Stale` / not-warm /
`Failed` DBs are (re)warmed, exactly as for a DB with no pending update.

## API / behaviour diff summary

- [api/services/auto_warmup_reconcile.py](../../../api/services/auto_warmup_reconcile.py):
  the `update_required` branch no longer `continue`s. It records
  `result["update_available"].append({db, source_version, latest_version})` and
  falls through to the existing warm-state logic, which (re)warms the current
  generation when the DB is not healthily warm. No change to the warm/re-warm
  mechanics: the warmup task already calls `k8s_release_stale_warmup_jobs(...)`
  unconditionally, so Jobs pinned to gone nodes are released and recreated on
  the current Ready nodes with `force_rewarm=False`.
- Reconcile result shape: new optional `update_available` list (only present on
  clusters with drift). The `skipped` list no longer carries `update_required`
  entries. No frontend consumed `update_required` (grep-verified); no IaC change.

## Validation evidence

- Rewrote `test_reconcile_auto_warmup_warms_current_generation_when_update_available`
  (was `…_skips_stale_downloaded_generation`) to assert `status="triggered"`,
  `enqueued=[core_nt]`, `update_available=[…]`, and no `update_required` skip.
- `uv run pytest -q api/tests/test_auto_warmup.py` → 32 passed.
- `uv run pytest -q api/tests/test_auto_warmup.py api/tests/test_warmup_jobs.py
  api/tests/test_warmup_route.py api/tests/test_storage_data.py` → 117 passed.
- `uv run ruff check api/services/auto_warmup_reconcile.py
  api/tests/test_auto_warmup.py` → clean.
- Live diagnosis evidence (read-only): NCBI `latest-dir=2026-06-06-01-05-02`,
  downloaded `16S/core_nt=2026-06-02` → `update_required` skip → `ready_noop`
  every tick while DBs sat `Stale nodes_ready=0`. Cluster nodes were Ready 5/5.

## Deploy

Baked into the api/worker image (Celery task + service). Needs an api/worker
redeploy to take effect on the live cluster; after deploy the next beat tick
re-warms the current generation and the chips clear from `warm stale`.
