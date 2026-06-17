# New Search submit no longer hard-blocks on a drifted core_nt calibration

## Motivation

Submitting from **New Search** failed with the toast:

> Submission failed: caller-supplied db_effective_search_space does not match the
> calibrated database snapshot

Root cause: the Web BLAST parity feature pins a calibrated effective search space
to a **fixed** core_nt snapshot (2026-05-09:
`calibrated_db_len=1,041,443,571,674`, `calibrated_db_num=125,619,662` in
[api/services/web_blast_searchsp.py](../../../api/services/web_blast_searchsp.py)).
The frontend sends the live DB's `db_total_letters`
([web/src/pages/BlastSubmit.tsx](../../../web/src/pages/BlastSubmit.tsx)
`selectedDbInfo.total_letters`). NCBI updates core_nt frequently, so once the
operator re-downloads a newer snapshot the live `total_letters` no longer equals
the pinned calibration, and `_calibration_snapshot_error` in
[api/services/blast/submit_payload.py](../../../api/services/blast/submit_payload.py)
fired.

The Service Bus bridge already degraded gracefully on this
(`allow_servicebus_downgrade`), but the **browser** path hard-blocked
(`submission_source != "servicebus"`), so New Search was unusable whenever
core_nt drifted from the calibration.

## User-facing change

A **snapshot drift** — the live calibrated database's stats no longer matching
the pinned Web BLAST calibration — now degrades gracefully on **every** submit
surface (browser New Search included), exactly as the Service Bus bridge already
did:

- the calibrated `db_effective_search_space` is dropped (BLAST computes its own
  effective search space for that run),
- precise sharding falls back to approximate when the output format can be
  merged across shards,
- a warning is recorded on the compatibility contract
  (`"verified Web BLAST search-space calibration does not match this database
  snapshot; using BLAST's own effective search space for this run (Web BLAST
  e-value parity not applied)"`) instead of a 4xx block.

**Deliberately unchanged (still blocks):** an explicit *wrong* `searchsp`
override on a calibrated DB, and `precise` sharding on an *uncalibrated*
database. Those are caller errors, not DB drift, so they keep their existing
block (the Service Bus bridge still downgrades them as before).

## API / IaC diff summary

Backend-only.

- `api/services/blast/submit_payload.py` — `resolve_sharding_plan` tracks a
  `snapshot_drift` flag and degrades when `allow_servicebus_downgrade or
  snapshot_drift`; the downgrade block now drops the searchsp, falls back to
  approximate only when the outfmt is mergeable (keeps blocking otherwise), and
  surfaces a clear warning.
- `api/tasks/servicebus/tasks.py` — unchanged behaviour (still passes
  `allow_servicebus_downgrade=True`).

## Validation evidence

- `uv run pytest -q api/tests` → 3914 passed, 3 skipped (includes the two
  previously-asserted blocks staying green:
  `test_external_blast_submit_rejects_bad_searchsp_override`,
  `test_blast_jobs_submit_blocks_false_precise_with_unverified_database`).
- New test
  `api/tests/test_blast_submit_route_options.py::test_browser_submit_degrades_on_calibration_snapshot_mismatch`
  — a browser submit with a drifted `db_total_letters` no longer blocks, drops
  the searchsp, falls back to approximate, and carries the warning.
- `uv run ruff check` → clean.

## Follow-up

The warning is on the compatibility contract; surfacing it as a visible New
Search banner/toast is a small frontend follow-up. Recalibrating
`WEB_BLAST_SEARCHSP_DEFAULTS["core_nt"]` to the current snapshot restores NCBI
e-value parity (separate maintenance task).
