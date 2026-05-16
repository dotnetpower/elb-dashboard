# 2026-05-15 — BLAST Jobs page no longer silently empty

## Motivation

The BLAST Jobs page (`/blast/jobs`) was rendering "No BLAST jobs yet · Submit
your first search" even though the user had run **8** BLAST jobs that
afternoon (3 completed, 5 failed) against the cluster. Two failures stacked
on top of each other:

1. **Local-only data source.** The page only reads from
   [api/routes/stubs.py](../../../api/routes/stubs.py)
   `GET /api/blast/jobs`, which queries `JobStateRepository` (Azure Table
   Storage). The dashboard's local dev environment has no
   `AZURE_TABLE_ENDPOINT` set, so the route returns
   `{"jobs": [], "degraded": true, "degraded_reason": "not_configured",
   "message": "Job state storage is not configured…"}`.
2. **The SPA dropped the `degraded` / `message` fields on the floor.** It
   only consumed `jobs[]`, so the user saw a perfectly normal "no rows yet"
   empty state with no hint that anything was wrong.

On top of that, the jobs the user actually cared about were never recorded
in the dashboard's local Table state at all — they were submitted directly
through the sibling **elb-openapi** execution-plane service
(`elbacr01.azurecr.io/elb-openapi:3.4`, exposed at `http://20.249.147.217`),
whose state lives in K8s ConfigMaps. There was no dashboard surface that
joined the two views.

## User-facing change

* The BLAST Jobs page now also pulls from a new dashboard endpoint
  `GET /api/v1/elastic-blast/jobs` that proxies the external openapi
  service's `/v1/jobs` listing. Externally-submitted jobs appear in the
  list (with a synthesised `job_title` of `<program> · <db basename>` and
  `phase` copied from the external `status`).
* Local Table rows still win on `job_id` collision so they keep richer
  metadata (owner UPN, infrastructure, phase history).
* When `/api/blast/jobs` reports `degraded: true` AND the merged list is
  empty, the empty-state card now shows an amber banner with the
  `degraded_reason` and `message` so the operator knows it is a config
  problem, not "you really have zero jobs".
* If the external openapi proxy itself errors, the SPA shows a small
  muted "External ElasticBLAST OpenAPI is unreachable: …" line in the
  empty state.

## API / IaC diff summary

* [api/services/external_blast.py](../../../api/services/external_blast.py) —
  new `list_jobs()` helper. Calls the external service's `/v1/jobs`
  (the legacy listing endpoint; the newer `/api/v1/elastic-blast/...`
  contract has submit/get/file but no list). Same auth + timeout +
  upstream-error handling as the other helpers in this module.
* [api/routes/elastic_blast.py](../../../api/routes/elastic_blast.py) —
  new `@router.get("/jobs")` (`/api/v1/elastic-blast/jobs`) gated by
  `require_caller`. Forwards to `external_blast.list_jobs()`.
* [web/src/pages/BlastJobs.tsx](../../../web/src/pages/BlastJobs.tsx) —
  added a second `useQuery(['blast-jobs-external'], …)` that calls the new
  proxy with `retry: false` so a missing `ELB_OPENAPI_BASE_URL` does not
  hammer the api with retries. Mapper `externalToSummary()` projects the
  external shape onto `BlastJobSummary`. `allJobs` now merges and sorts
  by `created_at desc`. `degradedNotice` derives from the `/api/blast/jobs`
  response and renders inside the empty-state card.
* No Bicep / IaC change. No new env var (the proxy reuses
  `ELB_OPENAPI_BASE_URL` and `ELB_OPENAPI_INTERNAL_TOKEN` that the existing
  `submit` / `get` paths already consume).

## Validation evidence

* `uv run pytest -q api/tests` → **137 passed in 10.93s** (no regression).
* `npx tsc --noEmit` in `web/` → clean (no TS errors).
* `curl -H 'Authorization: Bearer __dev_bypass__'
   http://127.0.0.1:8080/api/v1/elastic-blast/jobs`
  → HTTP 200, `count=8 jobs=8 statuses=['completed', 'failed']` —
  matches the upstream `http://20.249.147.217/v1/jobs` directly.
* Dashboard `/api/blast/jobs` still returns its existing
  `degraded_reason: "not_configured"` payload locally, which the SPA now
  surfaces instead of swallowing.

## Notes for the next iteration

* The external job shape does not carry `owner_upn`, so externally-submitted
  rows render without a user attribution. Once the openapi service starts
  recording the submitter (or the dashboard records every external submit
  into its own Table state), this will fill in automatically because local
  rows already win on `job_id` collision.
* `phase` is just a copy of the openapi `status` for external rows, so
  filter buckets (`running` / `completed` / `failed`) line up with the
  same status strings the local route uses.
* Delete still goes through `blastApi.deleteJob` which targets
  `/api/blast/jobs/{id}` — for purely-external jobs that route will
  currently 404. A follow-up should either route to
  `/api/v1/elastic-blast/jobs/{id}` for external rows or hide the Trash
  affordance on rows we know are external-only.
