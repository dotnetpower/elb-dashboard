# Async BLAST DB download with progress tracking

**Date**: 2026-05-12
**Scope**: `api/function_app.py`, `web/src/components/cards/StorageCard.tsx`, `web/src/api/endpoints.ts`

## Motivation

`prepare_blast_db` synchronously iterated `start_copy_from_url` over every
NCBI source blob. For large databases (e.g. `core_nt`, ~600 files) the
HTTP request exceeded the Static Web App proxy's 4-minute backend timeout,
producing a "Backend call failure" toast. The card also flipped to
"Ready" the moment the API returned, hiding the fact that the actual
copy was still ongoing for many minutes.

## User-facing change

- Clicking "Get" on an NCBI database now returns immediately. The card
  shows an in-progress section: spinner, `Copying X / Y files`, elapsed
  seconds, estimated minutes (from the catalog), and an animated
  progress bar.
- The action button shows the live percentage (`12%`) while copying
  instead of being missing or showing the brief startup elapsed timer.
- The card border keeps shimmering (existing parent shimmer) for the
  whole copy duration, not just for the brief initiating call.
- The success result banner only appears once the actual file count
  reaches at least 90% of the expected count (i.e. the copy actually
  finished), not when the API call returns.

## API / IaC diff summary

- `api/function_app.py` `prepare_blast_db` route:
  - S3 listing and per-file `start_copy_from_url` calls moved into a
    background `threading.Thread`. The HTTP handler returns
    `{ok: true, files_total, async: true, source_version, ...}`
    as soon as the source blob list is known.
  - Inside the worker, `ThreadPoolExecutor(max_workers=20)` fires the
    `start_copy_from_url` calls in parallel. Metadata blob write and
    public-network re-disable also run inside the worker so the route
    returns within seconds even for `core_nt`.
- `web/src/api/endpoints.ts` `prepareBlastDb` response type extended
  with `files_total?: number` and `async?: boolean`.
- `web/src/components/cards/StorageCard.tsx`:
  - New `inProgress: Map<string, {expectedFiles, startTime, sourceVersion?}>`
    state captured from the API response.
  - 10 s polling effect (`dbQuery.refetch`) while `inProgress.size > 0`.
  - Completion-detection effect: when the metadata blob's
    `file_count >= expectedFiles * 0.9`, move the entry from
    `inProgress` to `locallyDownloaded` and surface the success banner.
  - `onDownloadingChange` notifies the parent for both the brief
    initiating phase and the long copying phase, so the card border
    keeps shimmering throughout.

## Validation evidence

- `python -c "import ast; ast.parse(open('function_app.py').read())"` → OK.
- `pytest -q api/tests/` → 13 passed.
- `npx tsc --noEmit` (web) → clean.
- `npx vite build --mode production` → succeeded (4.91s).
- API deployed via `WEBSITE_RUN_FROM_PACKAGE` user-delegation SAS
  (`funcapp-async.zip`), Function App restarted.
- SPA deployed via `azd deploy web --no-prompt` (57s).
