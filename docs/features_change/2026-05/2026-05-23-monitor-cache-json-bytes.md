# monitor_cache — JSON bytes storage (no more deepcopy)

## Motivation
Every cache hit in `cached_snapshot` paid a `copy.deepcopy(value)` —
`_refresh` deepcopied the loaded payload before storing, and
`_with_cache_meta` deepcopied again on every read. AKS monitor payloads
(nodes/pods/jobs) on a cluster of any size are tens of KB to MB; the
SPA polls 6 monitor routes every few seconds, so this was one of the
most expensive paths on the api sidecar event loop's thread pool.

## User-facing change
None. Same dict shape returned; serialization roundtrip yields a fresh
mutable dict (same isolation as deepcopy) at a fraction of the cost.

## API / IaC diff
* `api/services/monitor_cache.py`
  * `_SnapshotEntry.value` (`dict`) → `_SnapshotEntry.payload_bytes`
    (`bytes`). `_refresh` does `json.dumps(payload, default=str)` once
    and stores the encoded bytes.
  * `_with_cache_meta` takes `bytes`, does `json.loads` once, then
    inserts the cache-meta dict. Behaves identically when the loader
    returned a non-dict value (wrapped under `{"value": …}`).
  * `default=str` keeps datetime / UUID values from raising during
    serialization — matches the previous deepcopy's tolerance for
    arbitrary value types.
  * All four `entry.value` / `fallback.value` / `refreshed.value` /
    `loader()` call sites updated to pass bytes.

## Validation
* `uv run pytest -q api/tests/test_monitor_cache.py
  api/tests/test_warmup_route.py` — 20 passed.
* `uv run ruff check api/services/monitor_cache.py` — clean.
