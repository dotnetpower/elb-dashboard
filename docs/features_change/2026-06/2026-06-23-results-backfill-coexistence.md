---
title: Results-layout backfill + flat/dated coexistence — flag-gated
description: An idempotent, dry-run-first backfill that moves legacy flat results/{job_id}/ trees into the dated layout via atomic dfs rename, plus the coexistence guarantee that flat and dated jobs read correctly side by side.
tags:
  - storage
  - architecture
---

# Results-layout backfill + flat/dated coexistence

Epic #64, issue #73. Closes the main migration sequence.

## Coexistence (already guaranteed by #66/#67)

Flat (legacy) and dated (new) jobs read correctly **side by side** with no
migration, because every result read resolves through
`resolve_results_prefix(job_id)`:

- a flat job's row carries `results_prefix = {job_id}/` → reads `results/{job_id}/`;
- a dated job's row carries `results_prefix = YYYY/MM/DD/{job_id}/` → reads there.

This is proven by the resolver tests (flag-off skip-lookup, flag-on dated/flat
rows, lookup-failure degrade) and the backfill tests (flat jobs move, dated jobs
skip).

## Optional backfill (move old flat jobs into the dated layout)

`api/services/storage/results_backfill.py` `backfill_results_layout(dry_run=, limit=)`:

- **Gated** on BOTH `STORAGE_DFS_ENABLED` and `STORAGE_DATE_LAYOUT_ENABLED`
  (moving to a dated layout only makes sense when the dated layout is the live
  target). No-op otherwise.
- **dry-run by default** — returns a `plan` (`from`/`to` per job) without touching
  storage.
- **Atomic move**: `dfs_io.rename_directory_dfs` moves `results/{job_id}` →
  `results/YYYY/MM/DD/{job_id}` in one metadata op (no blob copy), where the date
  is derived from the job's `created_at`. `expected_src_leaf=job_id` guards it.
- **Idempotent + self-healing**: a job already on the dated layout is skipped; the
  row's `results_prefix` is stamped (`JobStateRepository.update(results_prefix=…)`)
  only after a successful rename, so a partial prior run re-attempts cleanly.
- **Bounded** by `limit` so a manual/beat drain proceeds gradually.

## Migration runbook

1. **Prereqs** (do NOT skip): `STORAGE_DFS_ENABLED=true`, blob soft-delete enabled
   (#76 — recoverability net), split jobs date-aware (#75 — the flag gate), and a
   live-cluster validation pass.
2. **Flip** `STORAGE_DATE_LAYOUT_ENABLED=true` — new submissions go dated; old jobs
   stay flat and keep reading correctly (coexistence).
3. **Dry-run** the backfill, review the plan:
   `backfill_results_layout(dry_run=True, limit=50)`.
4. **Drain** in bounded batches: `backfill_results_layout(dry_run=False, limit=50)`
   repeatedly until `scanned == skipped` (every completed job dated).
5. **Cutover** (optional, future): once all jobs are dated, the legacy
   `{job_id}/` fallback in `resolve_results_prefix` can be removed — tracked
   separately; not required (the fallback is harmless).

Retention purge + archive live in #76.

## Validation evidence

- `uv run pytest api/tests/test_results_backfill.py` → **12 passed** (rename
  guards: moves/true, absent/false, rejects bad src/dest, refuses wrong src leaf;
  backfill: flags-off no-op, dry-run plans-without-moving, live moves+stamps row,
  skips already-dated, records error without raising or stamping the row).
- `uv run pytest api/tests/test_state_repo.py` → green (`update(results_prefix=…)`
  additive kwarg).
- `uv run ruff check api` → clean.

## Self-critique (design pass)

- **Atomicity**: rename then stamp; on rename failure the row is NOT stamped (no
  dated-row/flat-blobs desync); on stamp-after-rename failure the next run finds
  the source gone, re-stamps → self-heals. ✓
- **Idempotency**: already-dated rows skipped; bounded + resumable. ✓
- **Security**: `expected_src_leaf=job_id` guard; no SAS; flag-gated OFF. ✓
- **Liveness**: bounded by `limit`; no unbounded loop. ✓
- Verdict: no Critical/High.
