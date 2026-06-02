# Warmup status merge preserves DB-generation marker

## Motivation

The dashboard warmup card showed a database (`core_nt`) as **Ready / warm**, yet
submitting a BLAST job against the same cluster failed at the submit gate with:

```
node warmup for core_nt has no DB generation marker
```

This happens whenever a prior BLAST submit has left `init-ssd-*` setup Jobs on
the cluster for the same DB. Those setup Jobs do **not** carry the
`elb.dashboard/source-version` annotation, while the node-local
`app=db-warmup` Jobs do.

## Root cause

`k8s_warmup_status` builds `result["databases"]` in two passes:

1. `_database_status_from_setup_jobs` seeds the list from `init-ssd-*` setup
   Jobs first — these have **no** `source_version`.
2. `database_status_from_warmup_jobs` (which carries `source_version` /
   `source_versions`) is merged in afterwards via `_merge_database_statuses`.

In the `warmup_authoritative` merge path the function copied counts, status,
shards, sources and timing keys onto the existing setup entry, but the
key-copy loop **omitted** `source_version` / `source_versions`. The merged
entry was therefore `status="Ready"` but marker-less. The BLAST submit gate
`ensure_node_warmup_ready_for_submit` then compared the storage blob's
`source_version` against an empty warm marker and raised
`WarmupNotReadyError(... "has no DB generation marker")`.

## User-facing change

A database that is genuinely warmed (warmup Jobs succeeded and annotated) now
passes the BLAST submit readiness gate even when leftover `init-ssd-*` setup
Jobs from a previous run are present. The dashboard card state and the submit
gate now agree.

## API / IaC diff summary

- `api/services/k8s/warmup_status.py` — `_merge_database_statuses` now also
  carries `source_version` and `source_versions` from the incoming (warmup)
  entry onto a pre-existing setup entry. Purely additive: setup entries never
  carry those keys, so they can never overwrite a real marker.
- No IaC, route, or schema change.

## Validation evidence

- New regression test
  `api/tests/test_k8s_warmup_status_parallel.py::test_warmup_status_merge_carries_source_version_over_setup_entry`
  reproduces the setup-first-then-warmup merge and asserts the merged
  `core_nt` entry keeps `source_version="2026-05-26-01-05-01"`.
- `uv run pytest -q api/tests/test_k8s_warmup_status_parallel.py` → 8 passed.
- `uv run pytest -q api/tests -k "warmup or blast_submit or task_config or merge"`
  → 213 passed.
- `uv run ruff check api/services/k8s/warmup_status.py api/tests/test_k8s_warmup_status_parallel.py`
  → clean.
- Live cluster confirmation (AKS `elb-cluster-02`): 10 `warm-core-nt-0*` Jobs
  all `succeeded=1` and annotated `elb.dashboard/source-version=2026-05-26-01-05-01`,
  alongside 20 `init-ssd-*` setup Jobs for `core_nt_shard_*` with no marker —
  exactly the merge collision this fix resolves.
