---
title: Warmup no longer rejects prepared databases as "unknown database"
description: Remove the hardcoded BLAST_DATABASES gate so any prepared NCBI database (e.g. 18S_fungal_sequences, ITS_RefSeq_Fungi) can be warmed.
tags:
  - blast
  - operate
---

# Warmup no longer rejects prepared databases as "unknown database"

## Motivation

DB warmup failed for databases that were fully downloaded and visible in the
dashboard. Live reproduction on `elb-cluster-01` (moonchoi production): four
databases were present in workload Storage — `16S_ribosomal_RNA`,
`18S_fungal_sequences`, `ITS_RefSeq_Fungi`, `core_nt` — but only the first and
last could be warmed. Triggering warmup for `18S_fungal_sequences` returned:

```json
{ "status": "failed", "error": "unknown database: 18S_fungal_sequences" }
```

No Kubernetes warmup Job was ever created.

### Root cause

`api/tasks/storage/warmup.py` gated on a hardcoded `BLAST_DATABASES` dict
(`api/tasks/storage/helpers.py`) that lists only ~10 well-known NCBI databases.
NCBI publishes many more, so any prepared database outside that static list was
rejected up front with a misleading `unknown database` error — even though the
database was fully staged in workload Storage.

The gate was both **redundant** and **wrong**: the authoritative validation is
the workload Storage catalog check immediately below it
(`list_databases` + `file_count > 0` + `copy_status.phase == "completed"`),
which already rejects un-prepared / mid-copy / mid-update databases with clear
errors. The hardcoded `db_info` value was never read after the gate.

## User-facing change

* Warming a prepared database that is not in the static `BLAST_DATABASES` list
  (e.g. `18S_fungal_sequences`, `ITS_RefSeq_Fungi`, and any future NCBI
  database) now proceeds instead of failing immediately with
  `unknown database`.
* Truly un-prepared databases still fail, now with the accurate
  `database '<name>' is not prepared in workload storage` message from the
  Storage catalog check.

## API / IaC diff summary

* `api/tasks/storage/warmup.py` — removed the `BLAST_DATABASES.get()` gate and
  its now-unused import. Added a comment pointing future editors at the
  authoritative Storage-catalog validation.
* `api/tasks/storage/helpers.py` — `BLAST_DATABASES` kept (descriptive metadata,
  still re-exported) but annotated as **not** a validation source.
* No route, schema, or infrastructure change. The fix is baked into the
  `elb-api` worker image and takes effect on the next worker deploy.

## Validation

* `uv run pytest -q api/tests/test_warmup_database_readiness.py` — 5 passed,
  including two new regression tests:
  * `test_warmup_database_allows_db_outside_hardcoded_catalog` — a non-listed
    prepared DB reaches the Storage gate (fails on `phase=copying`, not
    `unknown database`).
  * `test_warmup_database_unprepared_db_reports_storage_not_catalog` — a DB
    absent from the Storage catalog reports `not prepared in workload storage`,
    never `unknown database`.
* Wider sweep: `test_warmup_route.py`, `test_auto_warmup.py`,
  `test_warmup_jobs.py`, `test_warmup_planner.py`,
  `test_warmup_database_readiness.py` — 109 passed.
* `test_tasks_facade_contract.py` — 54 passed (`BLAST_DATABASES` re-export
  contract intact).
* `uv run ruff check` clean on all touched files.

> Live note: the fix is a worker-image code change. A `quick-deploy.sh api`
> (worker) deploy is required to make it effective on the running cluster.
