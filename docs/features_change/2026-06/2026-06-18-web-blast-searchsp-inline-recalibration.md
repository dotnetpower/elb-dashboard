---
title: Inline Web BLAST search-space drift recalibration
description: New Search and OpenAPI submits auto-adapt the verified Web BLAST search space to a drifted core_nt snapshot instead of blocking or degrading.
tags:
  - blast
  - user-guide
---

# Inline Web BLAST search-space drift recalibration

## Motivation

The verified Web BLAST search space for `core_nt` was a single pinned constant
calibrated against the 2026-05-09 snapshot (`db-len = 1,041,443,571,674`,
`db-num = 125,619,662`, `searchsp = 32,156,241,807,668`). When the live database
was re-downloaded and its statistics drifted, the submit gate could no longer
match the pinned value and **blocked browser New Search** (and degraded the
Service Bus bridge), even though the run was otherwise valid. Recovering parity
previously implied a manual recalibration with a full-database BLAST run.

A spike confirmed the search space is a **deterministic function** of the live DB
statistics for the fixed 64 nt calibration query:

```
searchsp = (query_len - L) * (db_len - db_num * L)   with L = 33
```

`L = 33` reproduces the pinned value EXACTLY from the calibrated stats and is
insensitive to modest snapshot drift (it tracks `log(db_len)`). So the verified
search space can be **recomputed inline from the live stats** — no NCBI
round-trip, no full-database run, no background job, no cluster dependency.

## User-facing change

* **New Search now stays precise after a `core_nt` snapshot drift.** The
  dashboard recomputes `web_blast_searchsp` from the live `total_letters` /
  `total_sequences` it already reads, the browser forwards both stats, and the
  submit gate recomputes the same value and accepts it as
  `web_blast_compatible_sharded`.
* **Graceful degrade is preserved where it is still correct.** A caller that
  replays the stale pinned value against drifted stats (or sends drifted
  `db_total_letters` without `db_total_sequences`, so the value cannot be
  recomputed) still degrades to approximate sharding with a warning instead of
  blocking.
* **Bad overrides still block.** A `db_effective_search_space` that matches
  neither the recomputed nor the pinned value is rejected as before.

## API / IaC diff summary

* `api/services/web_blast_searchsp.py` — added `compute_web_blast_searchsp()`
  (pure formula) and `calibrated_searchsp_for_stats()` (recompute-or-pinned).
* `api/services/storage/database_list.py` — `web_blast_searchsp` is recomputed
  from live stats when available (new `web_blast_searchsp_source` field:
  `recomputed_live_snapshot` / `pinned_calibration`).
* `api/services/blast/submit_payload.py` — `resolve_sharding_plan` recomputes the
  verified value from forwarded `db_total_letters` / `db_total_sequences`,
  accepts a matching value as precise, degrades on a genuine drift, and blocks a
  bad override. Removed the now-superseded `_calibration_snapshot_error` equality
  gate. Added `db_total_sequences` to the submit option allowlist.
* `api/services/blast/compatibility.py` — the compatibility contract compares the
  configured search space against the live-recomputed value.
* `web/` — `db_total_sequences` is forwarded on both the submit and pre-flight
  payloads (`blast.types.ts`, `blast.ts`, `useSubmitMutation.ts`,
  `usePreFlight.ts`, `BlastSubmit.tsx`).

## Validation evidence

* `uv run pytest -q api/tests` — 3934 passed, 3 skipped.
* New tests: `api/tests/test_searchsp_recalibration.py` (recompute-accept,
  stale-pinned degrade, no-stats pinned fallback, bad-override block),
  `api/tests/test_web_blast_searchsp.py` (formula reproduces the pinned value
  EXACTLY), plus recompute cases in `test_blast_compatibility.py` and
  `test_storage_data.py`.
* `cd web && npx vitest run` — 900 passed; `npm run build` clean (strict TS).
