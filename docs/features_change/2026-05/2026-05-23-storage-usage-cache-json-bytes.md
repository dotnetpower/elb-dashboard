# storage_usage_cache — JSON bytes storage, no per-hit deepcopy

## Motivation
Two `deepcopy(summaries)` calls on the hot path: one when caching the
loader result, one on every `UsageCacheResult` returned to a caller.
The Storage card on the dashboard polls usage every few seconds — the
deepcopy traversal of the nested per-container dict added up under
sustained dashboard activity.

## User-facing change
None. Same `UsageCacheResult` shape; the dict isolation contract is
preserved by a `json.loads` round-trip on each hit (strictly cheaper
than `deepcopy` for dict-of-primitives data).

## API / IaC diff
* `api/services/storage_usage_cache.py`
  * `_UsageEntry.summaries` (`dict | None`) → `_UsageEntry.summaries_bytes`
    (`bytes | None`). `_refresh` writes `json.dumps(..., default=str)`
    bytes once.
  * `_result_from_summaries` accepts either a raw dict (cold path) or
    bytes (cache hit) and always returns a fresh mutable dict.
  * All eight `entry.summaries` / `refreshed.summaries` reads switched
    to `summaries_bytes`.
  * `import deepcopy` removed.

## Validation
* `uv run pytest -q api/tests/test_storage_usage_cache.py` — 3 passed.
* `uv run ruff check api/services/storage_usage_cache.py` — clean.
