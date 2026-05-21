# elb-openapi 3.6.0 cache hardening + transient classification

* **Date**: 2026-05-22
* **Sibling repo commit**: `dotnetpower/elastic-blast-azure` `docker-openapi/app/main.py`
  (master, version bump `3.5.0 → 3.6.0`)
* **Dashboard image bump**:
  [api/services/image_tags.py](../../../api/services/image_tags.py)
  `IMAGE_TAGS["elb-openapi"] = "4.13" → "4.14"`
* **Related issue/PR**: follow-up to
  [2026-05-21 BLAST REST databases endpoints](2026-05-21-blast-rest-databases-endpoints.md).
  Closes the critique items collected after 4.13 went live.

## Motivation

The 4.13 cycle shipped `/v1/databases` + `/v1/databases/{name}` with an in-process
metadata cache, but a 20-item severity sweep identified concurrency, error-handling,
and observability gaps that would silently masquerade Storage outages as 404 and
let cold readers race the cache. The 3.6.0 ship plugs every Critical/High item
without changing the response contract.

## User-facing change

`/v1/databases/{name}` and `/v1/databases` keep their 4.13 schema. New observable
behaviour:

* **`X-Cache` response header** on every `/v1/databases/{name}` response, including
  4xx / 5xx error responses. Possible values: `HIT`, `MISS`, `REVALIDATE`,
  `NEGATIVE_HIT`, and `BYPASS` (for the 503 transient path).
* **Transient Storage failures now surface as `503 Service Unavailable`** instead of
  being collapsed into 404. The previous behaviour could turn a 60-second Storage
  outage into a "database not found" answer that the dashboard would then cache.
* **Negative cache (default TTL 60 s)** keeps the service from hammering Storage
  every time the dashboard sweeps stale entries.
* **Unknown `molecule_type` raw values** now raise instead of being passed through
  as labels — keeps the response shape predictable for the dashboard.
* **Warmup**: a daemon thread primes the metadata cache on `startup` so the first
  dashboard sweep does not pay a cold-fetch tax. Disable with
  `ELB_OPENAPI_DISABLE_WARMUP=1` (useful for unit tests and local debugging).

## API / IaC diff summary

* `docker-openapi/app/main.py`:
  * Routes `list_blast_databases` / `get_blast_database` switched from `async def`
    to sync `def` (blocking `requests` calls under uvicorn — no event-loop stall).
  * Two locks (`_db_metadata_lock`, `_db_list_lock`) replace a single shared lock
    so list refresh no longer blocks per-database lookups.
  * Metadata cache uses `OrderedDict` + `move_to_end` for access-LRU and
    `popitem(last=False)` for trim. Stored entries are `deepcopy`d so callers
    cannot mutate the cache through their returned dict.
  * New `_MetadataFetchError(transient: bool = True)` exception classifies
    Storage outcomes: 5xx / 429 / auth failure / parse errors → transient (503);
    404 → non-transient (404 + negative-cache write); 401 / 403 → non-transient
    error (no negative-cache write).
  * `_fetch_blob_with_etag` honours ETags (304 → `REVALIDATE`); cache writes are
    skipped when `_DB_METADATA_TTL_SECONDS <= 0` (test mode).
  * Snapshot regex misses now log a `warning` so operators see when the layout
    drifts away from `…/YYYY-MM-DD-HH-MM-SS/…`.
  * `cached_at` carries microseconds (`%Y-%m-%dT%H:%M:%S.%fZ`) so consecutive
    revalidations have distinct timestamps.
  * `VERSION = "3.6.0"`.
* `web/src/api/blast.ts`: added a JSDoc guard on
  `BlastDatabaseMetadata.molecule_type` warning future contributors not to swap
  the source for elb-openapi's lowercase token field (`dna`/`protein`); use
  `molecule_label` instead.
* `api/services/image_tags.py`: `elb-openapi: 4.13 → 4.14`.
* **K8s Deployment**: `ELB_OPENAPI_API_TOKEN` migrates from a plaintext `env.value`
  (which also leaked into `kubectl.kubernetes.io/last-applied-configuration`) to
  a `valueFrom.secretKeyRef` pointing at a new `elb-openapi-secrets` Secret. The
  token value itself is unchanged for this cycle; rotation is tracked separately.

## Breaking changes

None. The 3.5.0 → 3.6.0 transition is purely additive (new header, new 503
classification for previously-mishandled outages, new cache statuses). The 3.5.0
breaking surface (`description→title`, `version→snapshot`,
`metadata_version→metadata_schema_version`, `molecule_type: nucl|prot →
dna|protein`) is documented in
[2026-05-21 BLAST REST databases endpoints](2026-05-21-blast-rest-databases-endpoints.md).

## Validation evidence

* `python3.11 -m py_compile docker-openapi/app/main.py` — OK.
* Helper sanity suite (molecule resolver / `_normalise_metadata` regex warning /
  microsecond `cached_at` / `_MetadataFetchError` default `transient=True` /
  `_cache_trim` keeps newest 2 of 3) — all assertions PASS.
* `TestClient` end-to-end scenarios (run from `docker-openapi/app/`):
  1. cold MISS → `200 X-Cache: MISS`
  2. warm HIT → `200 X-Cache: HIT` (no extra Storage fetch)
  3. expired entry → ETag revalidation → `200 X-Cache: REVALIDATE` (`If-None-Match`
     sent, 304 honoured)
  4a. unknown DB → `404 X-Cache: MISS` (negative cache populated)
  4b. unknown DB again → `404 X-Cache: NEGATIVE_HIT` (no extra Storage fetch)
  5. transient Storage outage on nucl + 404 on prot → `503 X-Cache: BYPASS`
     (previously surfaced as 404 — the critical regression fix)
  6. `/v1/databases` list → `200`
  7. LRU touch on `a` then `_cache_trim(limit=2)` keeps `{a, c}` (most-recent and
     newest), evicts `b` — confirms access-promotion semantics.
* AKS rollout: `kubectl rollout status deployment/elb-openapi -n default
  --timeout=180s` + `/openapi.json` reports `"version": "3.6.0"`. Header check via
  authenticated curl confirms `X-Cache: MISS → HIT` on consecutive
  `/v1/databases/core_nt` calls.
