---
title: "BLAST DB: clear stale \"Update available\" after a successful update"
description: >
  Fix the BLAST Databases card showing an "Update" button on a database that
  was just updated, and prevent false-positive update badges once all
  databases are current.
tags:
  - blast
  - ui
---

# BLAST DB: clear stale "Update available" after a successful update

## Motivation

After a successful DB update (e.g. `16S_ribosomal_RNA` promoted by the AKS
fan-out path), the BLAST Databases card still showed an orange **Update**
button on that database and kept counting it in the "N updates available"
badge. The database looked un-updated even though the backend had correctly
promoted the new generation.

### Root cause (two bugs)

1. **Stale `check-updates` query.** The SPA's per-DB update list comes from
   `GET /api/blast/databases/check-updates`, cached under the
   `blast-db-latest-version` React Query key with `staleTime: 300_000` and
   **no `refetchInterval` and no invalidation when a copy/update completes**.
   So after a promote, the SPA kept serving the pre-update list — which still
   contained the database the user had just refreshed — for minutes.

2. **Rotate-sensitive legacy fallback.** When the server returned an *empty*
   `updates_available` list, the SPA could not tell "evaluated, nothing stale"
   from "not evaluated (no storage scope)". It fell back to the coarse
   `source_version !== latest_version` heuristic, which re-flags **every**
   database whenever NCBI rotates its `latest-dir` tag — even when the
   underlying database content is unchanged. This produced false "Update
   available" badges once all real updates had been applied.

Ground truth from the deployed `check-updates` (per-DB signature comparison):
`16S_ribosomal_RNA` was correctly **absent** from the list after its update,
while `18S_fungal_sequences`, `ITS_RefSeq_Fungi`, and `core_nt` had genuinely
different composite signatures (real content change, not just a `latest-dir`
rotation).

## User-facing change

- The **Update** button and the "N updates available" badge clear immediately
  after a database update finishes, instead of lingering for minutes.
- Databases whose NCBI `latest-dir` merely rotated (no content change) no
  longer show a false "Update available" once all genuine updates are applied.
- No change to which databases genuinely need an update — the authoritative
  per-DB NCBI signature comparison is unchanged.

## API / IaC diff summary

- `api/routes/blast/databases.py` — `check-updates` response gains an additive
  boolean `updates_available_evaluated` (default `false`; `true` only after the
  per-DB signature comparison actually runs). No IaC change.
- `web/src/api/blast.ts` — `checkUpdates` response type adds the optional
  `updates_available_evaluated` and the already-returned
  `composite_signature` / `stored_composite_signature` item fields.
- `web/src/components/cards/storage/useBlastDb.ts` —
  - the "updates available" count gates the legacy fallback on
    `updates_available_evaluated` instead of "server list is empty";
  - a new effect re-runs the `check-updates` query on the
    `serverCopyActive` falling edge (any copy/update completion) so the stale
    badge clears at once;
  - exposes `updatesEvaluated`.
- `web/src/components/cards/storage/blastDbUpdates.ts` — new pure
  `dbHasUpdate` helper centralising the per-row decision.
- `web/src/components/cards/storage/BlastDbModal.tsx` — per-row Update button
  uses `dbHasUpdate` (gates the legacy fallback on `updatesEvaluated`).

## Validation evidence

- Backend: `uv run pytest -q api/tests/test_blast_databases_check_updates.py`
  → 6 passed (new `updates_available_evaluated` assertions: `True` when the
  per-DB comparison ran, `False` for the no-storage-scope legacy shape).
  Related sweep `test_blast_databases_preview.py` + `test_prepare_db_aks_task.py`
  → 24 passed.
- Frontend: new `blastDbUpdates.test.ts` (7 tests) covers the regression
  (evaluated + absent from map ⇒ no Update even when `latest-dir` rotated) and
  the legacy fallback. Storage-card suite → 51 passed. `npm run build` clean.
- Ground truth: deployed `check-updates` with storage scope returns
  `16S_ribosomal_RNA` absent and the other three present, matching the new
  SPA behaviour.
