# DB readiness honors `copy_status.phase` end-to-end

## Motivation

Production scenario (2026-05-27): the operator triggered `prepare-db core_nt`
on the deployed dashboard. `prepare-db` writes the `core_nt-metadata.json`
blob the moment it starts so it can update progress, which means
`/api/blast/databases` immediately surfaces a `core_nt` entry whose
`copy_status.phase` is `copying`. Multiple surfaces treated "the DB shows up
in the listing" as "the DB is ready":

* **Warmup card** showed `Storage DB ready` and an enabled **Warm** button.
* **New Search** listed `core_nt` as a selectable database in *Standard
  databases*, counted it in the category tab badge, and Submit was reachable.
* **Backend warmup task** (`api.tasks.storage.warmup_database`) only gated on
  `file_count > 0`, so a click against an in-flight DB would queue auto-shard
  + node warmup against incomplete volumes (cryptic per-pod failures minutes
  later).

The hardening that introduced `copy_status.phase` (see
`2026-05-22-blast-db-download-hardening.md`) already exposed the authoritative
signal but only `BlastDbRow` / `useBlastDb` consumed it. Other call sites kept
their pre-hardening heuristic and drifted.

## User-facing change

* **Warmup card**
  * The Storage pill now reads `Downloading · 30/800 files`,
    `Partial copy · …`, `Copy init failed`, `Updating to BLAST_DB-2026-05-20`,
    etc. when `copy_status.phase` is not `completed`. Tone is
    loading/blocked/ok accordingly (new `StatusPill` `loading`/`blocked`
    tones).
  * The detail line surfaces the same readiness reason.
  * **Warm** is disabled while the DB is mid-copy / mid-update; `canWarm`
    requires the strict readiness verdict.
  * In-flight DBs stay visible in the panel so users see progress instead of
    the row disappearing.
* **New Search → Choose Search Set**
  * Category tabs count only ready DBs; tab title shows `N ready · M
    preparing` when a copy is in-flight.
  * The DB row stays visible for in-flight copies but is dimmed, the radio is
    `disabled`, and the status column shows the readiness label
    (`Downloading 30/800`, `Partial copy 750/800`, `Updating to …`).
  * Auto-select on category change skips in-flight DBs (the previous behaviour
    silently picked the first DB and left Submit blocked with no message).
  * Suggested chips (`!form.db`) only suggest ready DBs.
  * A new warning banner appears when the selected DB exists but is not ready
    yet (mirrors the existing "not in storage" banner).
* **Submit**
  * `deriveSubmitValidation` exposes `dbNotReady` / `dbNotReadyReason`;
    Submit is disabled with the human reason in the `missing[]` list and the
    Database step indicator turns off.

## API / IaC diff summary

* `api/services/blast/task_config.py`
  * **New** `validate_blast_database_ready(*, storage_account, database)`:
    wraps `validate_blast_database_available` then reads
    `blast-db/{db}-metadata.json` (single capped blob GET, 1 MiB max).
    Raises `BlastDatabaseAvailabilityError(code="database_not_ready")` when
    `copy_status.phase != "completed"` and `code="database_updating"` when
    `update_in_progress` is set. Falls back to availability semantics for
    legacy DBs (no metadata.json) and for transient Storage errors.
  * **New** per-process 5s readiness cache + `reset_blast_database_readiness_cache()`
    for tests. Same TTL as the existing submit-gates cache so we never serve a
    stale verdict longer than 5s after prepare-db completes.
* `api/services/blast/submit_gates.py`
  * `_gate_blast_database` now calls `validate_blast_database_ready`. New
    error codes (`database_not_ready`, `database_updating`) carry friendly
    action hints (`Wait for download`, `Wait for update`).
* `api/routes/blast/preflight.py`
  * The `database` check row now uses the ready-aware validator. The check
    title differentiates `BLAST Database Preparing` / `BLAST Database
    Updating` from generic missing/found, and adds `error_code` for SPA-side
    mapping.
* `api/tasks/storage/warmup.py`
  * `warmup_database` rejects with `failed` + structured message when the
    selected DB's `copy_status.phase != "completed"` or `update_in_progress`.
    Defense in depth against scripted callers, beat scheduling, or stale UI
    that bypass the SPA gates.
* `api/services/storage/database_list.py`
  * `/api/blast/databases` response gains derived `ready: bool` +
    `not_ready_reason: str | null` fields on each row. Mirrors the SPA's
    `getBlastDbReadiness` so any consumer (BLAST orchestrator, OpenAPI,
    audit tools) can read one boolean instead of re-deriving the contract.

## Frontend diff summary

* **New** `web/src/utils/blastDbReady.ts` + `.test.ts` — single source of
  truth (`getBlastDbReadiness`, `isBlastDbReady`, `blastDbReadinessLabel`,
  `blastDbReadinessTone`, `blastDbBlockedReason`). 10 unit tests covering all
  five `copy_status` phases, update_in_progress, legacy file_count fallback,
  unknown phase forward-compat, undefined/null inputs.
* `web/src/components/cards/storage/useBlastDb.ts` — local `isDbReady` now
  delegates to the shared util (kills duplication / drift risk).
* `web/src/components/warmupSection/helpers.ts` — `buildWarmupRows` uses
  shared util; new `storageReady` / `storageTone` row fields; in-flight DBs
  stay visible; `canWarm` requires strict readiness.
* `web/src/components/WarmupSection.tsx` — `StatusPill` accepts new
  `loading` / `blocked` tones; storage pill driven by `row.storageTone`
  instead of hard-coded label match.
* `web/src/pages/blastSubmit/DatabaseSection.tsx` — readiness-aware category
  counts, disabled-row rendering for in-flight DBs, readiness-aware
  suggested chips; `firstDatabasePathForCategory` skips not-ready DBs.
* `web/src/pages/blastSubmit/submitValidation.ts` + `types.ts` +
  `BlastSubmit.tsx` — new `dbNotReady` / `dbNotReadyReason` plumbed through.
* `web/src/pages/blastSubmit/DatabaseSection.test.ts` — fixture update +
  three new cases (skips in-flight, only-in-flight returns empty, skips
  update_in_progress).
* `web/src/components/warmupSection/helpers.test.ts` — five new cases
  (copying, partial, updating, legacy ready, completed ready).
* `web/src/pages/blastSubmit/submitValidation.test.ts` — fixture update +
  one new case (in-flight DB blocks submit).

## Performance notes

* `validate_blast_database_ready` adds one metadata-blob GET per submit
  attempt, sized at 1 MiB max (metadata.json blobs are <16 KiB in practice).
  The result is cached per-process for 5s, so a UI retry burst from a single
  click sends at most one extra GET per (account, db) tuple.
* Frontend predicate is pure / O(1) per DB. `buildWarmupRows` and category
  counters keep their existing O(n) shape (n ≈ 10 DBs).
* No extra network calls in the SPA — readiness is derived from the
  existing `/api/blast/databases` payload.

## Validation evidence

```text
# Backend
$ uv run pytest -q api/tests/test_blast_database_readiness.py \
                    api/tests/test_warmup_database_readiness.py \
                    api/tests/test_blast_database_availability.py \
                    api/tests/test_blast_submit_gates.py \
                    api/tests/test_response_contracts.py \
                    api/tests/test_storage_data.py \
                    api/tests/test_auto_warmup.py
79 passed in 6.93s

$ uv run pytest -q api/tests
1617 passed; 1 unrelated flake (test_terminal_exec.py timeout on `az --version`)
that passes when re-run in isolation.

$ uv run ruff check api/services/blast/task_config.py \
                    api/services/blast/submit_gates.py \
                    api/services/blast_task_config.py \
                    api/routes/blast/preflight.py \
                    api/tasks/storage/warmup.py \
                    api/services/storage/database_list.py \
                    api/tests/test_blast_database_readiness.py \
                    api/tests/test_warmup_database_readiness.py
All checks passed!

# Frontend
$ cd web && npm test -- --run
Test Files  49 passed (49)
     Tests  357 passed (357)

$ npm run build
✓ built in 7.99s

$ npx eslint <touched files>
clean
```

## Deployment notes

* Both `api` and `frontend` sidecar images need a rebuild because the
  preflight gate, submit-gate path, and warmup task all changed. The
  `quick-deploy.sh frontend api` two-target rebuild is sufficient — no Bicep
  / Container App template change.
* Backwards compatible: legacy DBs prepared before the hardening (no
  metadata.json or no `copy_status` key) still validate via the existing
  availability + `file_count > 0` heuristic.
