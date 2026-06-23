---
title: Recursive job-storage delete via dfs (fix soft-delete leak) — flag-gated
description: blast_job_delete now recursively deletes a job's result/query directories via the ADLS Gen2 delete_directory when STORAGE_DFS_ENABLED is on, fixing the leak where deleted jobs left their blobs forever.
tags:
  - storage
  - blast
---

# Recursive job-storage delete via dfs — flag-gated

Epic #64, issue #69 (core). Builds on the dfs pool (#65), stored prefix (#66/#67),
and dfs listing (#68).

## Motivation (the leak)

`blast_job_delete` was **soft-delete only**: it flipped the jobstate row to
`deleted` and **never removed the result blobs**, so every deleted job left its
`results/{job_id}/...` (and query) blobs in Storage forever. On an HNS account the
fix is a single atomic `delete_directory(recursive=True)` — no per-blob loop.

## User-facing change

With `STORAGE_DFS_ENABLED=true`, deleting a job now also purges its result and
query directories from Storage (best-effort); the delete response carries
`storage_purged: true`. With the flag OFF (default) behaviour is unchanged
(tombstone only) — the leak fix activates with the dfs data-plane.

## What landed

- `api/services/storage/dfs_io.py` — `delete_directory_dfs(...)`: one atomic HNS
  recursive delete; idempotent (absent dir → `False`). **Safety guards**: rejects
  empty / `..` paths, and when `expected_leaf` is given the directory's last
  segment MUST equal it — a per-job delete passes `expected_leaf=job_id` so a bug
  can never target a parent date bucket (`results/2026/06/23`) and wipe a whole
  day of unrelated jobs.
- `api/services/storage/job_purge.py` (new) — `purge_job_result_storage(state)`:
  orchestrates the per-job purge (results + `queries/{job_id}` +
  `queries/uploads/{job_id}`), each guarded by `expected_leaf=job_id`. **Never
  raises** (storage cleanup must not block the tombstone). No-op when dfs is off,
  the job is external (the sibling owns its storage), or scope is missing.
- `api/routes/blast/jobs_lifecycle.py` — `blast_job_delete` calls the purge
  (best-effort) **before** writing the tombstone, and returns `storage_purged`.

## Deferred to follow-ups (honest scope)

The issue title also lists **archive move** and **retention purge**:

- **Retention purge** (delete aged date buckets / jobs older than N days) and
- **Archive move** (atomic rename hot→archive + a Storage lifecycle policy)

are deferred — they delete/relocate user data in bulk, need the date layout ON
(gated by #75) and an infra lifecycle policy, and warrant their own focused
issue + review. Tracked as a #69 follow-up.

## ⚠️ Prerequisite before flipping `STORAGE_DFS_ENABLED` ON

The recursive delete is **irreversible at the API level** and the platform
Storage account currently has **no blob soft-delete** (`deleteRetentionPolicy`)
configured (verified in `infra/modules/storage.bicep`). Before enabling the flag
in any environment, enable blob + container soft-delete as a recoverability
safety net (it was not added here because blob soft-delete support on HNS
accounts must be validated against the target region during a real
`azd provision`, which is the maintainer's call).

## Validation evidence

- `uv run pytest api/tests/test_job_purge.py` → **12 passed** (delete guards:
  deletes/returns-true, absent→false, dated-leaf-ok, rejects empty/`..`, refuses
  wrong leaf; purge: noop-when-off, skips-external, missing-scope, deletes
  result+query dirs, never-raises).
- `uv run pytest api/tests/test_blast_jobs_routes.py` → **24 passed** (delete
  route + additive `storage_purged` field, no regression).
- `uv run ruff check api` → clean.
- Frontend: `deleteJob` mutation only invalidates caches; it does not read the
  response body, so the additive field is safe.

## Self-critique (design pass)

- **Contract**: additive `storage_purged`; no consumer breakage. ✓
- **Ordering / partial failure**: purge is best-effort and never raises, and runs
  before the tombstone, so the row is always tombstoned even if Storage fails. ✓
- **Security / irreversibility**: flag-gated OFF, owner check, `leaf == job_id`
  guard (tested), external jobs skipped, no SAS. Medium: no soft-delete net →
  documented as a flip prerequisite. ✓
- **Idempotency / concurrency**: absent dir → `False`; racing deletes both safe. ✓
- Verdict: no Critical/High; one documented-mitigated Medium.
