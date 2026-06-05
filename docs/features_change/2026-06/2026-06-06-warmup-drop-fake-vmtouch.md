# Warmup Job: drop the fake vmtouch step (staging-only semantics)

## Motivation

Operator inspection of a live warmup pod on `elb-cluster-02` revealed that the
`/scripts/blast-vmtouch-aks.sh` step in the BLAST DB warmup Job was effectively
a noop:

```
vmtouch memory limit: 98G
2026-06-05 21:10:04 RUNTIME cache-blastdbs-to-ram 1.000000 seconds
2026-06-05T21:10:04Z DONE shard=05 size=36G
```

A 36 GiB shard cannot be read into RAM in 1 second on any SSD. The actual
mechanic was:

1. `azcopy cp` downloaded ~36 GiB to the node's `/workspace/blast` (hostPath
   ephemeral SSD). The OS write-back path placed those pages in the page cache
   as a side effect — this is the `+40 GiB cache` line operators see in the
   dashboard's per-node cache strip.
2. The follow-up `vmtouch -t -m<AVAIL_MEM>` immediately found every page
   already cached and exited in 1 s without doing real work.
3. The warmup pod then exited. Because no process holds an mmap on the staged
   files, the kernel is free to reclaim those reclaimable pages under any
   future memory pressure — so the "warmup" produced no durable RAM residency
   guarantee at all.

The script was contributing nothing useful while spending pod time and
emitting log lines (`vmtouch memory limit`, `cache-blastdbs-to-ram`) that
misled operators into believing the warmup had pinned the DB in RAM.

## User-facing change

* The BLAST DB warmup Job no longer executes `/scripts/blast-vmtouch-aks.sh`.
  The pod entrypoint emits a new `STAGING_COMPLETE shard=<idx>` log line
  immediately before the existing `DONE shard=<idx> size=…` completion marker.
* The dashboard's per-database warmup status now reaches the terminal
  `completed` phase via the existing `"done shard="` matcher in
  `_phase_from_warmup_log` without ever transitioning through
  `touching_memory`. The `touching_memory` phase remains in the dashboard
  type system as backward-compat coverage for in-flight pods that were
  scheduled before this change rolled out.
* No change to the warmup Job's actual side effects on the node disk:
  `/workspace/blast/<db>/` files, `.download-complete` marker, and
  `.download-source-version` are unchanged.
* `blast-vmtouch-aks.sh` is still shipped in the `elb-warmup-scripts`
  ConfigMap so that the dashboard's equivalence-experiment shell scripts
  (`scripts/dev/eq13-core-nt-f3l-widepool.sh`,
  `scripts/dev/eq14-core-nt-webxml-sharded.sh`) that exec it directly keep
  working unchanged.

## API/IaC diff summary

* `api/services/warmup/scripts.py`:
  * `warmup_shell_command()` — remove `/scripts/blast-vmtouch-aks.sh` invocation,
    replace with a `log "STAGING_COMPLETE shard=<idx>"` marker.
  * Module docstring updated to record the new warmup-Job contract and the
    rationale for not running vmtouch in the warmup pod.
* `api/tests/test_warmup_jobs.py`:
  * `test_warmup_job_for_each_node_pins_to_node` — assert the warmup container
    `args` no longer contains `blast-vmtouch-aks.sh` and now contains the
    `STAGING_COMPLETE shard=` marker.
  * New `test_new_staging_complete_log_resolves_to_completed_phase` regression
    test verifies that a warmup pod log without any vmtouch text still maps to
    the `completed` phase via the existing `"done shard="` matcher.

No infra/Bicep change. No frontend code change (the existing defensive case
for `touching_memory` in `web/src/components/warmupSection/helpers.ts`,
`web/src/components/ClusterItem/DatabaseChipStrip.tsx`, and
`web/src/api/monitoring.ts` is intentionally retained so the dashboard keeps
rendering correctly for any in-flight pre-deploy pods during a rolling change).

## Validation evidence

* `uv run pytest -q api/tests/test_warmup_jobs.py` — 26/26 passed.
* `uv run pytest -q api/tests -k 'warmup or staging'` — 121/121 passed.
* `uv run pytest -q api/tests` — 2920 passed, 1 flaky unrelated test
  (`test_smoke.py::test_readiness_storage_probe_is_single_flight_on_cold_cache`)
  that passed on focused re-run.
* `uv run ruff check api` — clean.
* `git diff --stat api/` —
  `api/services/warmup/scripts.py` +8 -2,
  `api/tests/test_warmup_jobs.py` +58 -1.

## Follow-ups (not in this change)

The investigation surfaced two larger improvements that the dashboard could
adopt to provide real first-query latency reduction. They are intentionally
deferred to separate PRs because each touches the BLAST search pod path:

1. **Override `/scripts/blast-run-aks.sh` via `terminal/patch_elastic_blast.py`**
   to add a `vmtouch -m<shard_size>G <db files>` step immediately before
   `blastn` runs. The BLAST process then holds an active mmap on those pages,
   making the cache durable for the lifetime of that search and at much
   lower eviction priority during it.

2. **NodeAffinity for BLAST search Jobs** matching the shard ↔ node placement
   that warmup already pins (`shard N → node ordinal N`). The current
   `blast-batch-job-local-ssd-aks.yaml.template` from elastic-blast has no
   `nodeAffinity`, so a search pod can land on a node that did not stage its
   shard and pay full fault-in cost from cold SSD.

Both can be implemented as additions to `terminal/patch_elastic_blast.py`
(the same in-place patch surface that already rewrites
`init-db-shard-aks.sh` and adds `workload=blast` tolerations to other AKS
templates).
