# Cache hot path: drop deepcopy + use OrderedDict LRU eviction

## Motivation
Two hot caches on the dashboard polling path used `copy.deepcopy` on every
hit and `min(..., key=)` linear scans on every eviction:

* `_JOBS_LIST_CACHE` in `api/routes/blast/jobs.py` (10 s TTL, polled ~14 s)
  — every cache hit deepcopied the full job list payload, every cache set
  walked all entries to find the oldest.
* `_DISPLAY_METADATA_CACHE` in `api/services/blast_db_metadata.py`
  (24 h TTL, hit on every `/api/blast/jobs` row that surfaces DB metadata)
  — same deepcopy on hit + linear-scan eviction at the 256-entry cap.

Both ran inside the cache lock, so contention scaled with payload size and
cache fill.

## User-facing change
None. Callers still receive a fresh mutable `dict[str, Any]`. Cache hit
latency drops materially under sustained polling (no full-tree deepcopy);
eviction is now O(1).

## API / IaC diff
* `api/routes/blast/jobs.py`
  * `_JOBS_LIST_CACHE` switched to `OrderedDict[str, tuple[float, bytes]]`
    storing the response as compact JSON bytes.
  * `_blast_jobs_list_cache_get` does `json.loads(...)` outside the lock
    (after `move_to_end`), so deserialization no longer blocks other
    readers. `_blast_jobs_list_cache_set` pops the key first then assigns
    so the LRU order is correct on overwrites; eviction is
    `popitem(last=False)`.
  * `import copy` removed; no longer used.
* `api/services/blast_db_metadata.py`
  * `_DISPLAY_METADATA_CACHE` switched to
    `OrderedDict[..., tuple[float, bytes | None]]`. Single-flight inflight
    map unchanged.
  * Read path: `move_to_end` + `json.loads` (deepcopy gone). Write path:
    pop-then-set + `popitem(last=False)` eviction.
  * `_DISPLAY_METADATA_CACHE_MAX_ENTRIES = 256` constant for the cap.
  * `import copy` removed; no longer used.

## Validation
* `uv run pytest -q api/tests/test_blast_db_metadata.py
  api/tests/test_blast_tasks.py api/tests/test_smoke.py` — 209 passed.
* `uv run ruff check api/routes/blast/jobs.py
  api/services/blast_db_metadata.py` — clean.
