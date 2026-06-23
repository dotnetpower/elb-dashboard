---
title: ADLS Gen2 (dfs) client pool + capability probe (default-OFF foundation)
description: Pooled DataLakeServiceClient layer, STORAGE_DFS_ENABLED feature gate, and a postprovision dfs capability probe — the foundation for migrating the Storage data-plane to native HNS directory operations.
tags:
  - storage
  - architecture
---

# ADLS Gen2 (dfs) client pool + capability probe

Epic #64, issue #65.

## Motivation

The platform Storage account has HNS (ADLS Gen2) enabled
(`infra/modules/storage.bicep` `isHnsEnabled: true`) and a `dfs` private
endpoint, but the entire data-plane used `BlobServiceClient` only — so the HNS
advantages (atomic recursive directory delete, metadata-only rename, true
hierarchical listing) were latent. This change lands the **foundation** every
later migration issue (#68 read, #69 delete/retention, #71 prepare-db) builds
on, with **zero behaviour change** by default.

## User-facing change

None yet. The dfs data-plane is gated OFF behind `STORAGE_DFS_ENABLED`
(default unset = OFF, charter §12a Rule 4). With the flag off, every code path
keeps using the Blob API exactly as before.

## What landed

- `api/services/storage/dfs_client_pool.py` — pooled `DataLakeServiceClient` /
  `FileSystemClient` mirroring the proven `client_pool.py` BlobServiceClient
  pool: thread-local fast path, LRU eviction, idle prune, deadlock-safe
  credential weakref finalizer, and `reset_dfs_service_pool` for test/rotation
  teardown. Exposes `dfs_enabled()`.
- `api/services/__init__.py` — credential rotation now also resets the dfs pool
  so a rotated MI token invalidates pooled dfs clients (parity with blob).
- `api/tests/conftest.py` — per-test reset of the dfs pool for isolation.
- `scripts/dev/probe_capabilities.py` — new `Storage ADLS Gen2 dfs (data plane)`
  probe. It is **gated on `STORAGE_DFS_ENABLED`**: skips when the feature is OFF
  (the dfs path is unused, so a missing dfs permission is irrelevant), and is
  **required / fail-closed** when the feature is ON. The dfs data-plane reuses
  the existing `Storage Blob Data Contributor`/`Reader` role — no new RBAC.
- `pyproject.toml` + `uv.lock` — added `azure-storage-file-datalake==12.17.0`
  (verified it does not bump the `azure-storage-blob==12.23.1` pin; lock diff is
  +17/-0, one new package only).

## API / IaC diff summary

- No API route changes.
- No Bicep changes (HNS + dfs PE already provisioned). The probe documents the
  required role against `infra/modules/storage.bicep`.
- New env var `STORAGE_DFS_ENABLED` (default OFF).

## Validation evidence

- `uv run pytest -q api/tests/test_storage_dfs_client_pool.py` → 27 passed
  (flag gating, account-name validation, pooling/reuse, LRU eviction,
  deadlock-safe finalizer defer/drain, idle prune, reset).
- `uv run pytest -q api/tests/test_storage_client_pool.py
  api/tests/test_storage_data.py` → 42 passed (no regression from the conftest /
  rotation changes).
- `uv run ruff check api` → clean.
- Probe: `probe_dfs_list()` raises `SkipProbe` with the flag OFF; registered in
  `PROBES` as required.
- Dependency stability: `git diff --numstat uv.lock` = `17 0`, only
  `azure-storage-file-datalake` added; `azure-storage-blob` stays `12.23.1`.

## Self-critique (design pass)

- **Contract**: new isolated module, no production consumers yet (D/E/H will
  use it); rotation + conftest resets wired. ✓
- **Liveness**: LRU loop bounded by pool size; SDK `retry_total=0` +
  connection/read timeouts (read_timeout=30 for directory ops, documented). ✓
- **Concurrency**: mirrors the proven blob-pool locking; deadlock-safe finalizer
  is unit-tested (defer-when-locked + drain-next-op). ✓
- **Security**: account-name regex reused; dfs URL re-validates; MI auth; no
  SAS; no public-access flip. ✓
- **Backward-compat**: flag default-OFF, additive dep, no pin bump. ✓
- Verdict: no Critical/High; the two Medium notes (dep pin, timeout doc) were
  closed in the hardening rounds.
