# BLAST DB hardening round 2 — concurrency, signatures, security, cancel UX

**Date**: 2026-05-22
**Scope**: backend + frontend

## Motivation

Round 1 ([2026-05-22-blast-db-download-hardening.md](./2026-05-22-blast-db-download-hardening.md))
closed the 20 most visible critique items for the DB download surface.
A widened critique surfaced 20 more issues across adjacent surfaces
(sharding, oracle, terminal WebSocket, NCBI catalogue resilience,
frontend reactive bugs). This change closes those.

## User-facing change

- **Cancel a stuck download.** Catalog rows now show a "Cancel" button
  during in-flight copies; clicking aborts every pending blob copy and
  flips the row to a clean state immediately (no 2 h stale-recovery wait).
- **Retry button works even while another DB is downloading.** Previously
  the per-row "Retry" was greyed by the global `downloadDisabled` lock
  for partial / init_failed rows; now retries are per-DB.
- **No more repeated partial-copy toasts.** The completion-detection
  effect dedupes by `(db, terminal phase)` so a 10 s poll loop doesn't
  re-toast the same error.
- **Multi-shard DBs now correctly detect updates.** Update detection
  uses a composite signature (hash of N evenly-spaced `.tar.gz.md5`
  ETags) instead of a single shard's ETag, so an NCBI rotation that
  touches only the last shard still surfaces as "Update available".
- **NCBI throttling is contained.** Repeated 403/5xx trips a circuit
  breaker for 2 min — users see a fast, actionable error instead of N
  seconds of timeout per call and NCBI sees zero traffic from us
  during cooldown.

## API / IaC diff summary

### New routes

- `POST /api/storage/prepare-db/{db_name}/cancel` — abort pending blob
  copies + write `copy_status.phase=cancelled` metadata.

### Hardened routes

- `POST /api/blast/databases/{db}/shard` — daemon now uses the
  ETag-aware `_update_metadata` helper for every write so a concurrent
  prepare-db / warmup writer cannot clobber the shard fields.
- `POST /api/blast/databases/{db}/oracle` — refuses (409) when the
  DB's `copy_status.phase` is `partial`, `init_failed`, or `copying`
  (was: only checked `update_in_progress`).
- `GET /api/blast/databases/check-updates` — comparison precedence is
  now `composite_signature` > `signature_etag` > `source_version vs
  snapshot`. Legacy DBs without ETag fields fall back transparently.
- `POST /api/storage/prepare-db` — records a DB-ops audit JobState
  (op=`prepare_db`) so the existing `/api/audit/log` surface picks it up.

### Hardened services

- `api/services/ncbi_catalogue.py`:
  - Composite signature samples up to `NCBI_SIGNATURE_SAMPLE_COUNT`
    (default 8) `.tar.gz.md5` ETags and SHA-256s them into a 16-char
    marker.
  - `httpx.Client` now has an explicit 30 s timeout.
  - `_BLAST_VOLUME_SUFFIXES` adds `.nos`, `.pos`, `.nxm`, `.pxm` so
    volume counts stay accurate for protein DBs.
- `api/routes/storage/common.py`:
  - Circuit breaker (`_NCBI_BREAKER_THRESHOLD` consecutive failures,
    `_NCBI_BREAKER_COOLDOWN` seconds) on `_resolve_latest_dir` +
    `_list_keys`. Refuses inbound calls while open.
- `api/routes/storage/prepare_db.py`:
  - Lock registry GC: caps at 256 entries, evicts free locks first.
  - Cooperative shutdown event (`_SHUTDOWN_EVENT`) interrupts the
    60 s poll sleep on SIGTERM.
  - ETag-aware metadata writes also write `composite_signature` on
    promotion.
- `api/services/db_ops_audit.py` (new): focused helper that creates a
  JobState + jobhistory entry for `prepare_db`, `shard`, `oracle`, and
  `prepare_db_cancel` actions.
- `api/routes/terminal_ws.py`: CSWSH defence — WebSocket upgrade now
  refuses Origin headers that are not same-origin or in
  `TERMINAL_WS_ALLOWED_ORIGINS`. `TERMINAL_WS_ALLOW_ANY_ORIGIN=true`
  escape hatch for local dev only.

### Frontend

- `web/src/components/cards/storage/useBlastDb.ts`:
  - Dedup ref so partial-copy toasts fire once per (db, phase).
  - New `handleCancel` action calls the cancel route + clears local
    state + drops the dedup entry so a retry produces a fresh toast.
- `web/src/components/cards/storage/useDbPreviews.ts`:
  - Memoised `byName` map (downstream `useMemo` no longer re-fires per
    render).
  - `skipNames` parameter — modal passes the set of already-Ready DBs
    so the per-modal-open HEAD volume drops.
- `web/src/components/cards/storage/BlastDbRow.tsx`:
  - `onCancel` prop wired; "Cancel" button shows during in-flight copy.
  - "Retry" Get button is per-DB (no longer gated by another DB's
    in-flight download).
- `web/src/api/monitoring.ts`: `cancelPrepareBlastDb` typed client.
- `web/src/api/blast.ts`: `BlastDatabase` gains `composite_signature`;
  `checkUpdates` response adds `composite_signature` per entry.

## Validation evidence

```
$ uv run pytest -q api/tests
1200 passed in 29.95s
```

New tests:
- `api/tests/test_ncbi_breaker_composite.py` — circuit breaker open/close
  + composite signature detects later-shard rotation.
- `api/tests/test_prepare_db_routes.py` — 409 on live daemon, cancel
  aborts pending copies, cancel refuses when already completed.
- `api/tests/test_terminal_ws_origin.py` — same-origin allowed, unknown
  origin rejected, explicit allowlist works, bypass flag permits all.
- `api/tests/test_blast_databases_check_updates.py` — legacy ETag-empty
  falls back to snapshot diff; composite signature takes precedence.

Lint clean on every changed file.

## Out of scope

- The composite signature still samples a fixed N (default 8) `.tar.gz.md5`
  ETags. A truly bulletproof signature would HEAD every md5, but that
  costs N HEADs per `preview_database` call. 8 covers every multi-shard
  layout NCBI publishes today.
- `_SHUTDOWN_EVENT` is wired into the poll loop but not yet `set()` from
  any lifespan / signal handler — that requires a separate change to
  `api/main.py` and adds risk to the unrelated request-handling path.
  Once set, stale-recovery + ETag idempotence already cover the
  partial-completion case.
- The frontend build currently fails on a pre-existing TS error in
  `web/src/pages/UpgradePage.tsx` (unrelated WIP — unused `confirmBreaking`
  state). Not addressed here.
