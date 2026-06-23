---
title: dfs recursive delete for staged BLAST databases — flag-gated
description: The prepare-db delete route collapses a database's per-shard batch delete (up to ~4.8k blobs for nt) into a single atomic ADLS Gen2 delete_directory when STORAGE_DFS_ENABLED is on, with a Blob batch fallback.
tags:
  - storage
  - blast
---

# dfs recursive delete for staged BLAST databases

Epic #64, issue #71. Builds on the dfs delete helper (#69).

## Motivation + honest scope correction

The issue framed three migration targets; investigation corrected two of them:

- ✅ **DB delete (the real win)** — `routes/storage/prepare_db.py prepare_db_delete`
  enumerated `blast-db/{db_name}/` shards (up to ~4.8k blobs for `nt`) and
  batch-deleted them (256/request, ~19 round-trips). On HNS this collapses to a
  single atomic `delete_directory`. **Migrated.**
- ⏭️ **`_safe_delete_job`** is a **Kubernetes** Job + ConfigMap delete, **not** a
  storage op — nothing to migrate to dfs.
- ⏭️ **Progress-count listing** (`tasks/storage/prepare_db_via_aks.py` staged-blob
  count, with a `last_modified`/`since` filter in a hot progress callback) — a
  marginal `list_blobs` count, **left on Blob** (same as #68's read scoping; dfs
  `get_paths` would add the `since` filter for ~zero benefit on a hot path).
- ⏭️ **warmup scripts** (`services/warmup/scripts.py`) are **bash** run in the
  BLAST pod (`find … -exec rm`, azcopy) against the pod's local filesystem — not
  Azure Storage. N/A.

## User-facing change

With `STORAGE_DFS_ENABLED=true`, deleting a staged database removes its shard
directory in one atomic op instead of thousands of serial/batched deletes
(faster, no client-timeout risk). Flag OFF = the existing batch delete
(unchanged).

## What landed

- `api/routes/storage/prepare_db.py` — after enumerating the shard names (kept,
  for the `deleted` count + metadata exclusion), when `dfs_enabled()` the route
  calls `delete_directory_dfs(cred, account, "blast-db", f"{db_name}/",
  expected_leaf=db_name)` once (`deleted = len(names)`), then deletes the
  sibling `{db_name}-metadata.json`. `expected_leaf=db_name` (a single-segment,
  `_RE_DB_NAME`-validated name) guarantees it can only target this DB's
  directory, never the whole `blast-db` container. **Any dfs error falls back to
  the proven Blob batch loop.**

## Validation evidence

- `uv run pytest api/tests/test_prepare_db_delete_route.py` → **9 passed**
  (existing 7 flag-OFF batch tests + new dfs recursive path + dfs-error →
  batch fallback).
- `uv run ruff check api` → clean.
- Frontend `deleteDatabase` does not read the per-shard count fields; the
  response shape is unchanged.

## Self-critique (design pass)

- **Contract**: response shape unchanged (`deleted`/`errors`/`metadata_deleted`/
  `partial`); dfs path sets `deleted=len(names)`, `errors=0`. ✓
- **Idempotency**: absent DB → empty names → dfs skipped → metadata 404 →
  success; re-delete is a no-op. ✓
- **Partial failure**: dfs error → Blob batch fallback (tested); metadata-retain-
  on-partial logic intact. ✓
- **Security**: `expected_leaf=db_name` guard; `require_caller`; no SAS. ✓
- **Backward-compat**: flag OFF = batch loop (7 existing tests green). ✓
- Verdict: no Critical/High.
