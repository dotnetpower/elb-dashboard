# Storage Container Usage And Grouping

## Motivation

The Storage Account card showed every [Azure Blob Storage](https://learn.microsoft.com/azure/storage/blobs/storage-blobs-introduction) container equally, including control-plane containers such as `dead-letter`, `job-payloads`, and `job-artifacts`. That made first-run storage look noisier than necessary and did not show how much space each container used.

## User-facing change

The Storage Account card now keeps researcher-facing containers visible first and places control-plane containers under a collapsed `Platform state` disclosure. Each container row can show total size and blob count when the backend can enumerate blobs. Large containers are capped by `STORAGE_USAGE_MAX_BLOBS_PER_CONTAINER` and shown as a lower-bound estimate when the scan is truncated. Usage totals are cached with stale-while-refresh semantics, so cold or expired totals render immediately as `calculating usage` while the backend refreshes the blob enumeration in the background.

## API/IaC diff summary

- `/api/monitor/storage` container entries now include nullable `blob_count`, `size_bytes`, `usage_pending`, `usage_truncated`, `usage_error`, `usage_cache_state`, and `usage_refreshed_at` fields.
- Storage usage totals are cached in-process by `api.services.storage_usage_cache`; tuning knobs are `STORAGE_USAGE_CACHE_TTL_SECONDS`, `STORAGE_USAGE_CACHE_STALE_SECONDS`, and `STORAGE_USAGE_CACHE_MAX_ENTRIES`.
- No Bicep changes.

## Validation evidence

- `uv run pytest -q api/tests/test_smoke.py -k storage_summary`
- `uv run pytest -q api/tests/test_storage_usage_cache.py`
- `cd web && npm run test -- src/components/cards/storage/StorageContainersTable.test.ts`
- `cd web && npm run build`