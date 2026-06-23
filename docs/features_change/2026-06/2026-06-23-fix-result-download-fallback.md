# Fix broken result-download fallback (removed SAS `download_url` path)

## Motivation

While validating the Service Bus outfmt-7 queue path (20 parallel jobs), the
BLAST result **download** code revealed a latent broken fallback. The result
download button (`useBlastResultActions.handleDownload`) had two branches:

* `file.file_id` present → `blastApi.downloadResultFile` → `GET /results/{file_id}`
  which **streams the bytes** through the api sidecar. This is the working path
  (verified live: 200 on every job).
* `else` → `blastApi.downloadResult` → `GET /results/download?blob_name=…` then
  `window.open(resp.download_url)`.

The `else` branch was stale: the `/results/download` endpoint was changed to
stream bytes through the sidecar (charter §9 — no SAS URL is ever handed to the
browser), but the frontend still treated the response as JSON `{ download_url }`
and called `window.open(undefined)`. It additionally passed `file.name` (a
basename) as `blob_name`, which the per-job blob guard rejects. The branch could
never succeed.

It was unreachable in practice — every result file from the listing carries a
`file_id` (local blobs get a deterministic base64 encoding via
`encode_blob_file_id`; external OpenAPI / Service Bus jobs keep their
sibling-generated `result-NNN` id) — but it was dead, broken code on the
download path.

## User-facing change

* The result download button now always uses the streaming `file_id` path. A
  malformed listing entry without a `file_id` surfaces a clear error toast
  instead of silently opening `undefined`.
* No change for the normal case: downloads already worked via the `file_id`
  path; this removes the broken fallback and its misleading API method.

## API / IaC diff summary

* `web/src/hooks/useBlastResultActions.ts`: `handleDownload` drops the
  `download_url` / `window.open` fallback; guards a missing `file_id` with a
  toast and otherwise streams via `downloadResultFile`.
* `web/src/api/blast.ts`: removed the unused `downloadResult` method (the only
  caller was the deleted fallback) and its stale `{ download_url: string }`
  return type.
* Backend unchanged: the `GET /jobs/{job_id}/results/download` route (and its
  path-traversal / cross-job security guards + contract test) is left intact for
  direct API consumers; it is simply no longer called by the SPA.

## Validation evidence

* `npm run build` (web) — clean.
* `npx eslint web/src/hooks/useBlastResultActions.ts web/src/api/blast.ts` — clean.
* `npx vitest run src/pages/blastResultsModel.test.ts src/hooks` — 70 passed.
* Live: result downloads via `GET /results/{file_id}` returned 200 on 6/6
  spot-checked Service Bus outfmt-7 jobs; the removed branch was never the path
  exercised by the UI.
