# Descriptions / Taxonomy fast-path — fix artifact cache misses

## Motivation
Opening the Recent searches → Descriptions tab took 5–15 seconds on the
first hit per session even though the backend already pre-bakes a
"default" alignments artifact on job completion. The two-line gate
that decides whether to serve the artifact was too strict: it only
matched callers who omitted `page_size`, but the SPA's default query
always sends `page_size: 100` (the same size the artifact is baked
at). Result: every Descriptions tab open went down the cold parse
path — list blobs → download → parse → annotate → sort → paginate —
even when a ready artifact existed.

The Taxonomy tab had the analogous problem after the
2026-05-22-taxonomy-ncbi-parity change made the SPA always send
`include_lineage=true`. The pre-baked taxonomy artifact contained no
lineage, so the gate refused to serve it.

## User-facing change
* **Descriptions tab opens in ~50–200 ms** instead of 5–15 s on every
  subsequent open of a job whose artifact has been built. The first
  open of a brand-new job still runs the cold path AND enqueues the
  backfill so the second open is fast.
* **Taxonomy tab Organism view** now serves from the artifact too,
  with `lineage` / `blast_name` already populated — no per-open
  eutils round-trip on the request thread.
* No filter / sort / page-size change affects the user — the gate
  only matches the exact "default" request the SPA sends on first
  open. Any user-applied filter still routes to the cold parser so
  results stay correct.

## API / IaC diff summary
* `api/routes/blast/result_helpers.py`
  * `default_alignments_request`: accept `page_size in (None, RESULTS_DEFAULT_PAGE_SIZE)`.
  * `default_taxonomy_request`: drop the `include_lineage=false` guard
    now that the artifact carries lineage.
* `api/services/blast_result_artifacts.py`
  * `build_default_taxonomy_payload`: enrich the rolled-up organisms
    with lineage / `blast_name` (top-20 organisms, eutils cached) so
    the artifact matches what the SPA's default Taxonomy query asks
    for. Lineage enrichment is wrapped in a best-effort try/except —
    a transient eutils failure does not block the artifact bake.
* No infra / Bicep changes.

## Validation
* `uv run pytest -q api/tests` → 977 passed.
* `uv run ruff check api/routes/blast/result_helpers.py
  api/services/blast_result_artifacts.py` → clean.
* Manual: a job whose `result_alignments` artifact exists now returns
  `source: "artifact"` / `artifact_state: "ready"` for the SPA's
  default query (verified by sending the exact query the SPA sends
  and inspecting the response).

## Follow-up — artifact schema versioning (same day)

The first pass shipped a hidden trap: once an analytics artifact was
written as `status: ready`, `artifact_build_should_enqueue` would
never re-trigger a rebuild, so any job whose artifact had been baked
by an older code version stayed stale forever. After Phase 1+2 went
out, the Taxonomy tab kept showing "Unclassified" for jobs whose
`result_taxonomy` artifact had been built before the stitle fallback
landed.

Fix:

* `api/services/job_artifacts.py`
  * New `_ANALYTICS_ARTIFACT_MIN_SCHEMA_VERSION` table — bump the
    entry for an artifact type whenever its builder's payload
    semantics change, and stamp the matching version in the builder.
  * `read_result_analytics_artifact` now reads the payload, checks
    `artifact_schema_version`, and on miss flips the state row to
    `status: failed` with `error_code: schema_stale` so the next
    request triggers a rebuild via `artifact_build_should_enqueue`.
* `api/services/blast_result_artifacts.py`
  * `build_default_alignments_payload` and
    `build_default_taxonomy_payload` now emit
    `"artifact_schema_version": 2`. The minimum table for both is
    set to 2, matching the Phase 2 rollup changes.
* `api/tests/test_job_artifacts.py`
  * Two new tests lock in the stale-detection contract
    (`test_read_result_analytics_artifact_treats_missing_schema_as_stale`,
    `test_read_result_analytics_artifact_returns_fresh_payload`).

Operational note: rolling out this change picks up automatically on
the next request per job — the SPA reads the artifact, the route
sees a stale state, the worker rebuilds, and the second request is
fast. Local dev environments must restart the Celery worker after
pulling so the new builder code is loaded; the stale flag would
otherwise rebuild via the old code and stay stuck.

