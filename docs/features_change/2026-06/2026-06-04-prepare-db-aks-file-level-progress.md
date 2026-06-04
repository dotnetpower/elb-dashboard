# prepare-db AKS-fanout: surface file-level download progress in the BLAST Databases modal

## Motivation

On the AKS-fanout prepare-db path (`mode=aks`), the BLAST Databases modal showed
`Copying 0 / 10 shards · 0%` for many minutes even while files were actively
landing in Storage. The progress callback only reported pod-level counts
(`succeeded_pods` / `shard_count`), and a pod is only marked `succeeded` once
**all** ~480 of its files finish. With 10 shards each doing a slow per-file scan
before/between downloads, no shard completes for a long time, so the SPA's
shard-progress fallback sat at `0 / 10` and `0%`. The download was healthy
(blob count climbing steadily) but the UI gave the user no visible signal,
which read as "is it actually downloading?".

## User-facing change

The modal now shows a **moving per-file bar** (e.g. `3723 / 4814 files · 77%`)
plus a throughput-derived ETA during an AKS download/update, instead of a static
`0 / 10 shards · 0%`. No frontend change was required — `BlastDbRow.tsx` already
prefers per-file progress (`copy_status.success`) over shard progress when the
field is present; the backend now populates it.

In addition, the progress line now shows a **live download speed** (e.g.
`· 42.3 MB/s`) for the AKS path, computed from the total bytes landed in Storage
divided by elapsed time.

## API / IaC diff summary

- `api/tasks/storage/prepare_db_via_aks.py`
  - New `_count_staged_blobs(container, db_name)` — best-effort
    `(count, total_bytes)` of blobs under `<db>/` via
    `container.list_blobs(name_starts_with=...)`. The byte sum reuses the same
    listing pass (`blob.size`), so it adds no extra Storage calls. Returns
    `None` on listing failure so the caller falls back to pod-level counts
    rather than reporting a wrong/zero file count.
  - `_on_job_progress(...)` now adds `copy_status.success = <count>` and
    `copy_status.bytes_done = <total_bytes>` to the copying-phase metadata when
    the listing succeeds. All existing fields (`phase`, `mode`, `total_files`,
    `active_pods`, `succeeded_pods`, `failed_pods`, `shard_count`) are
    unchanged; `success` / `bytes_done` are additive/optional so the readiness
    contract (`copy_status.phase == "completed"`) is untouched.
- `web/src/components/cards/storage/blastDbProgress.ts`
  - New `formatSpeed(bytesDone, elapsedSeconds)` pure helper — renders
    `B/s` → `TB/s` with a 5 s stability gate (returns `""` before that).
- `web/src/components/cards/storage/BlastDbRow.tsx`
  - Computes a monotonic-clamped `speedLabel` from `copy_status.bytes_done` and
    renders it in the copying progress line.
- `web/src/api/blast.ts`, `web/src/components/cards/storage/useBlastDb.ts`
  - Added optional `copy_status.bytes_done?: number` to the types.
- No IaC change. The worker already reaches Storage via the private endpoint in
  the platform VNet (same container client used by the post-Job `_poll_copy_completion`),
  so no new network surface is opened — Storage stays `publicNetworkAccess: Disabled`.

## Validation evidence

- `uv run pytest -q api/tests/test_prepare_db_aks_task.py` → 9 passed, including
  two tests:
  - `test_on_job_progress_reports_file_level_success` — asserts
    `copy_status.success` (3 blobs, `core_ntx/` decoy excluded) and
    `copy_status.bytes_done == 3500` (summed sizes, decoy excluded).
  - `test_on_job_progress_falls_back_when_listing_fails` — a `list_blobs` raise
    omits both `success` and `bytes_done` (no poisoned progress) and keeps
    pod-level fields.
- `cd web && npm test -- --run src/components/cards/storage/blastDbProgress.test.ts`
  → 13 passed, including the new `formatSpeed` suite (MB/s, GB/s, KB/s, B/s,
  stability gate, decimal rules).
- `cd web && npm run build` → built clean (no type errors).
- `uv run pytest -q api/tests/test_prepare_db_aks_route.py api/tests/test_blast_database_readiness.py api/tests/test_prepare_db_aks_manifest.py`
  → 46 passed (readiness contract + manifest guards unaffected).
- `uv run ruff check api/tasks/storage/prepare_db_via_aks.py api/tests/test_prepare_db_aks_task.py`
  → All checks passed.
