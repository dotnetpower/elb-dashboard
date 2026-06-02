# Warmup denominator: node-local warmup Jobs are authoritative over setup Jobs

## Motivation

On `elb-cluster-02` the DB Warmup panel showed `core_nt` as
`AKS cache copying · 10/20` and `Copying files to node disk · 10/20 nodes
ready · 10 active` even though the cluster has exactly **10** nodes and the
`warmup_database` task created exactly 10 node-local warmup Jobs
(`node_count: 10, num_shards: 10, jobs_created: 10, nodes_ready: 10`).

The inflated denominator also blocked BLAST submit: the readiness gate raised
`node warmup for core_nt is Loading (10/20 nodes ready)` and refused to queue
the job even though warmup had genuinely completed 10/10.

## Root cause

`k8s_warmup_status()` merges two job sources for the same DB:

- node-local warmup Jobs (`app=db-warmup`, **one per Ready node** = the correct
  denominator), and
- ElasticBLAST submit-side `init-ssd-*` setup Jobs (`app=setup`), whose count is
  ElasticBLAST's **internal shard count** — ~20 for the 288 GiB `core_nt` on a
  10-node cluster.

`_merge_database_statuses()` combined the two with `max()` on
`total_jobs` / `nodes_ready` / `nodes_failed` / `nodes_active`, so the larger
setup-Job count (20) won and corrupted both the dashboard card and the submit
readiness gate.

## User-facing change

- The DB Warmup panel now reports the node count as the denominator
  (`10/10`), matching the actual cluster size.
- BLAST submit is no longer held at `warmup_not_ready` when node-local warmup
  has completed for every node.

## Code change

`api/services/k8s/warmup_status.py` — `_merge_database_statuses()`: when an
incoming warmup-source entry (`sources` contains `warmup`) overrides a
setup-only existing entry, its count fields **and** status are taken verbatim
instead of `max`-merged. When both entries are the same source kind the
previous `max` behaviour is preserved. `sources` are still unioned, so a DB
seen via both job kinds keeps `["setup", "warmup"]`.

## Validation

- `uv run pytest -q api/tests/test_k8s_warmup_status_parallel.py` — 7 passed,
  including the new `test_warmup_status_warmup_jobs_are_authoritative_denominator`
  (20 setup Jobs, 10 active + 10 done; 10 succeeded warmup Jobs → merged entry
  asserts `total_jobs == 10`, `nodes_ready == 10`, `status == "Ready"`).
- `uv run pytest -q` across the warmup + submit-gate suites
  (`test_auto_warmup`, `test_warmup_jobs`, `test_warmup_database_readiness`,
  `test_warmup_planner`, `test_warmup_route`,
  `test_k8s_release_stale_warmup_jobs`, `test_blast_submit_gates`,
  `test_blast_submit_capacity_gate`, `test_blast_databases_warmup_plan`) —
  114 passed.
- `uv run ruff check api/services/k8s/warmup_status.py
  api/tests/test_k8s_warmup_status_parallel.py` — clean.
