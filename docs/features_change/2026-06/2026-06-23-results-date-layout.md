---
title: Hierarchical date layout for results (results/YYYY/MM/DD/{job_id}) — flag-gated
description: Date-tiered results bucket for new BLAST submissions, gated OFF behind STORAGE_DATE_LAYOUT_ENABLED, with a state-aware resolver so dated and legacy-flat jobs coexist with no migration.
tags:
  - storage
  - architecture
---

# Hierarchical date layout for results — flag-gated

Epic #64, issue #67. Builds on the stored-prefix system-of-record from #66.

## Motivation

At thousands of jobs/day a flat `results/{job_id}/` namespace becomes
operationally unwieldy and blocks prefix-scoped lifecycle/retention. This change
lets new submissions write results under `results/YYYY/MM/DD/{job_id}/`, bounding
directory fan-out and enabling the date-bucket retention in #69. It is **gated OFF
by default** so nothing changes until an operator opts in (and validates on a
live cluster).

## User-facing change

None while the flag is OFF (the default). With `STORAGE_DATE_LAYOUT_ENABLED=true`,
new dashboard BLAST submissions store and use a date-tiered results prefix; the
Results page, downloads, analytics, oracles and elastic-blast all resolve through
the same stored value, so the change is transparent to the user.

## What landed

- `api/services/storage/job_prefix.py`:
  - `date_layout_enabled()` — the `STORAGE_DATE_LAYOUT_ENABLED` gate (default OFF).
  - `build_dated_results_prefix(job_id, now=)` — `YYYY/MM/DD/{job_id}/` (UTC),
    computed once at submit.
  - `resolve_results_prefix(job_id, state=, repo=)` — the linchpin. Honours an
    explicit state row, else (only when the flag is ON) does a single `jobstate`
    lookup, else the flat `{job_id}/` fallback. Degrades to flat on lookup failure
    (never raises into a listing/streaming path).
- `api/routes/blast/submit.py` — stamps `state.results_prefix = build_dated_results_prefix(job_id)`
  at submit when the flag is ON (once, so there is no midnight-boundary drift);
  OFF leaves `to_entity`'s `{job_id}/` default.
- `api/services/blast/task_config.py` `results_job_url` — derives the elastic-blast
  `--results` bucket from `resolve_results_prefix` so the URL always matches the
  stored prefix.
- Every results-area **read** now resolves through `resolve_results_prefix`:
  `result_analytics` (success-marker + `list_parseable_result_blobs`),
  `result_artifacts.build_result_manifest_payload`, the `routes/blast/results.py`
  listing, `runtime_failure`, and elastic-blast id discovery
  (`blast/job_state` + `tasks/blast/submit_runtime`).
- Every results-area **write** resolves too: `oracles.py` tie-order/db-order
  oracle blobs land in the resolved (dated) bucket alongside elastic-blast output.

## Consistency surface (verified)

- **Self-consistent within the results bucket**: results_url, oracle writes,
  elastic-blast `job-` discovery, and all result-listing reads derive from one
  source of truth (`JobState.results_prefix`).
- **Artifact cache** (`write_/read_json_artifact`) is keyed by `job_id` and
  write/read symmetric — independent of the results layout, unchanged.
- **External (`/v1/jobs`) jobs** keep the flat `results/{job_id}/` layout (sibling
  contract; they are not stamped dated) — correct and unchanged.
- **Config blob** stays `queries/{job_id}/elastic-blast.ini` (separate container).

## Known limitations (do NOT flip the flag ON until resolved)

- **Split jobs are not yet date-aware.** Split parents/children keep the flat
  layout (their path-key builders are flat), but the submit route stamps a dated
  prefix on every submission including split parents. Enabling the flag while
  split submissions occur would desync a split parent's dated Results read from
  its flat merge output. Date-tiering split needs the result-map AND path-key
  builders changed together — tracked as a #67 follow-up.
- **Queries/uploads dating** is deferred (low value, separate container; reads
  already resolve from the stored `payload.query_file`).
- Flipping the flag ON requires live-cluster end-to-end validation (#55-class).

## Validation evidence

- `uv run pytest api/tests/test_storage_job_prefix.py` → **33 passed** (flag
  gating, `build_dated_results_prefix`, `resolve_results_prefix` explicit-state /
  flag-off-skip-lookup / flag-on-dated / legacy-flat / lookup-failure-degrades,
  `results_job_url` flat-OFF and dated-ON).
- Flag-OFF no-op regression: oracle/manifest/config-sharding **95**, results
  routes + submit-accession **53**, elb-cfg + parity **29**, submit-task +
  route-options **136** (0 failures), all green.
- Flag-ON smoke: `STORAGE_DATE_LAYOUT_ENABLED=true pytest test_blast_results_routes`
  → **40 passed** (dated path does not crash).
- `uv run ruff check api` → clean.

## Self-critique (design pass)

- **Contract**: single source of truth (`results_prefix`); all results read+write
  resolve through it. ✓
- **Liveness/concurrency**: prefix stamped once at submit (idempotent replay
  returns the existing row); resolver does at most one Table read. ✓
- **Partial failure**: resolver degrades to flat on lookup failure (logged, never
  raises) → at worst a transient empty result for a dated job, self-heals. ✓
- **Backward-compat**: flag default OFF = byte-identical (300+ tests). ✓
- **High finding (mitigated)**: split + flag-ON incompatibility — documented in
  the flag docstring and above; flag stays OFF until split is wired. Not a blocker
  for landing a dormant mechanism.
- Verdict: no Critical; one documented-mitigated High; safe to land flag-OFF.
