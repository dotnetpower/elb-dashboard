# Async sharding + auto-shard on warmup + hardening

## Motivation

Three connected pain points in the AKS card's per-DB chip strip:

1. **`core_nt` sharding timed out.** The synchronous `POST /api/blast/databases/{db}/shard`
   route ran `ensure_shard_sets` inline. For `core_nt` (~83 volumes × 8 preset
   shard counts ≈ 150+ Azure SDK round-trips) the call exceeded the
   `_blob_service` per-call timeouts and surfaced as `Request timed out`
   on the chip — but only after a long wait, during which the api sidecar
   was blocked from serving any other request.
2. **Sharding had to be triggered manually before warmup could work.** The
   db-warmup daemonset references the per-shard layout files, so a DB
   that was downloaded but not yet sharded silently broke warmup.
3. **The Celery worker was running stale code** (an old `terminal_exec.run(env=…)`
   signature) so even a successful click on the modal's Warmup button
   ended in `status=failed`.

## User-facing change

* Clicking a "downloaded only" chip now returns immediately. The chip
  flips to `sharding…` while the daemon thread runs and resolves to
  `sharded · ×N` (or `shard failed · click to retry`) on its own — no
  page refresh needed and no api sidecar stall. A page reload while
  sharding is in flight still shows the in-progress state because it is
  persisted in the metadata blob.
* Re-clicking a chip that is already sharding (or opening a second tab)
  is a no-op — the per-`(account, db)` lock returns 409 and the SPA
  silently invalidates the listing instead of surfacing an error.
* The Warmup button inside the cluster detail modal now performs the
  sharding step automatically if the selected DB has not been sharded
  yet, so the user no longer needs to click two buttons in the right
  order.
* If sharding fails, the chip itself shows the error (truncated +
  sanitised) with a "click to retry" hint, so the warning text below
  the strip is no longer load-bearing.

## API / IaC diff summary

### Backend (`api/`)

* [api/routes/stubs.py](../../../api/routes/stubs.py) `blast_database_shard`:
  rewritten to a daemon-thread async pattern (mirrors
  [api/routes/storage.py](../../../api/routes/storage.py) `prepare_db`).
  Returns `202 {accepted, db_name, sharding_started_at, output}` and
  publishes progress through `{db}-metadata.json`.
  - Added a module-level `_SHARD_LOCK_REGISTRY` (per-`(account, db)`
    `threading.Lock`) plus `_SHARD_LOCK_REGISTRY_GUARD`. Re-clicking
    while a daemon is in flight returns 409.
  - Added `_SHARD_STALE_SECONDS = 30 * 60`: a leftover
    `sharding_in_progress=true` flag older than that is treated as a
    crashed previous daemon and the new request takes over.
  - All error strings are passed through
    [api/services/sanitise.py](../../../api/services/sanitise.py)
    `sanitise()` before being written into the metadata blob or
    returned to the client (capped at 300 chars).

* [api/services/storage_data.py](../../../api/services/storage_data.py)
  `list_databases`: surfaces three new fields per DB:
  - `sharding_in_progress: bool` (default `False`)
  - `sharding_started_at: str | None` (ISO timestamp)
  - `sharding_error: str | None` (sanitised, truncated)

* [api/tasks/storage.py](../../../api/tasks/storage.py) `warmup_database`:
  added an inline auto-shard step after the "is the DB present in
  storage?" check. If the DB is already sharded the task is a no-op
  (returns `sharding="skipped"`); otherwise it writes the
  `sharding_in_progress=true` marker, runs `ensure_shard_sets`
  synchronously (safe inside a Celery worker — no HTTP timeout), then
  writes the final `sharded=true` / `shard_sets=[…]` state. On
  failure it persists `sharding_error` so the SPA chip strip surfaces
  the same retry UI the manual path uses, and the task itself returns
  `status=failed` with `error="auto-shard failed: …"` so the orchestrator
  status in the modal reflects reality instead of pretending to succeed.

### Frontend (`web/`)

* [web/src/api/blast.ts](../../../web/src/api/blast.ts):
  `BlastDatabase` gains `sharding_in_progress?`, `sharding_started_at?`,
  `sharding_error?`. `shardDatabase` return type updated to the
  new 202 shape (`accepted, db_name, sharding_started_at, output`).
* [web/src/components/ClusterItem.tsx](../../../web/src/components/ClusterItem.tsx):
  - `dbListQuery` now uses a function-form `refetchInterval` that
    returns `5_000` whenever any DB row carries `sharding_in_progress`
    (and `false` otherwise). Cadence falls back to the existing
    `staleTime`-driven refresh once all daemons have settled.
  - `dbChips` carries `shardingInProgress` + `shardingError` taken
    straight from the server. Chip stage classification reads
    `db.shardingInProgress || shardingDb === db.name` — server-side
    state wins over the optimistic local mutation flag, which fixes
    the stale-spinner case after a page reload.
  - New chip stage `shard failed · click to retry` (warn variant) when
    `db.shardingError` is set; the chip remains `isShardable` so the
    next click retries.
  - `shardMutation.onError` recognises HTTP 409 / "already in
    progress" and silently invalidates the listing instead of
    surfacing it to the user.

## Hardening checklist

* [x] Per-`(account, db)` lock prevents concurrent shard daemons.
* [x] Stale-flag recovery for crashed daemons (30 min TTL).
* [x] All error strings pass through `sanitise()` before landing in
  the metadata blob or the HTTP response.
* [x] `sharding_in_progress=true` is written **before** the daemon is
  spawned, so a thread-spawn failure leaves no stale flag (we only set
  it if the metadata pre-write succeeds).
* [x] Inputs (`subscription_id`, `resource_group`, `account_name`,
  `db_name`) all hit the same regex validation as `/api/storage/prepare-db`.
* [x] Frontend collapses 409 to a silent refetch — no noisy banner
  when a second tab races a click.

## Validation evidence

* `uv run pytest -q api/tests` → **237 passed in 20.80s**
  (no new tests; the existing storage / sanitise / smoke suites
  exercise the imports.)
* `cd web && npx tsc --noEmit` → clean.
* `cd web && npm run build` → 5.93 s, 671.97 kB JS (no size delta).
* Smoke check after worker restart: `/api/health` returns
  `{"status":"ok",…}` and the modal Warmup flow no longer hits the
  legacy `terminal_exec.run() got an unexpected keyword argument 'env'`
  failure path (that error was stale-image baggage; restarting the
  worker container loaded the current source via the `/app/api:ro`
  bind-mount).

## Known follow-ups (out of scope for this change note)

* `warmup_database` still **only verifies + shards** the DB — it does
  not deploy the node-side daemonset that vmtouches the shard layout.
  The chip will go `downloaded only → sharding… → sharded`, but it
  will not reach `ready` until a separate daemonset path is
  implemented. Until then, the modal's "Warmup" button should be read
  as "make sure this DB is ready for `elastic-blast submit`", not as
  "warm node caches". Tracking issue to be filed.
