# Warmup azcopy: let azcopy auto-tune concurrency (drop the hard-coded 16)

## Motivation

Node-local warmup downloads (Azure Blob → node disk over the private endpoint)
were slow for large DBs. `core_nt` is ~64 GiB per node on a 5-node shard and a
real warmup pod copied it in **430 s = 158 MB/s**.

Root cause (confirmed by a live benchmark, not a theory): the warmup download
script pinned `AZCOPY_CONCURRENCY_VALUE=16`. azcopy's own default is `16 × vCPU`
(capped at 300) with dynamic CPU-based tuning, so the hard-coded `16` ran at
**1/16th** of azcopy's auto value on a 16-vCPU node.

> An earlier attempt in this area introduced an SKU-vCPU formula
> (`concurrency = 2 × vCPU`, `buffer = vCPU // 2`) built on the **incorrect**
> belief that `AZCOPY_BUFFER_GB` caps in-flight parallelism at `buffer × 4`
> chunks. azcopy's only block/buffer constraint is `max block size ≤ 0.75 ×
> AZCOPY_BUFFER_GB` (a 256 MiB block needs ~0.34 GiB), so the buffer was never
> the bottleneck and that formula has been removed.

## Live benchmark (the evidence)

Throwaway pod on `elb-cluster-02` (`Standard_E16s_v5`, same `ncbi/elb:1.4.0`
image, same `core_nt` blob URL + include-pattern, `--block-size-mb=256`):

| Setting | concurrency | throughput |
|---------|-------------|------------|
| old hard-coded `AZCOPY_CONCURRENCY_VALUE=16` | 16 | 158 MB/s |
| unset → azcopy auto | 256 (`16 × vCPU`) | **281.5 MB/s (1.78×)** |

azcopy log on the auto run: *"Number of CPUs: 16; Max concurrent network
operations: 256 (Based on number of CPUs)"* with dynamic CPU tuning enabled.

## User-facing change

Warmup downloads now leave `AZCOPY_CONCURRENCY_VALUE` / `AZCOPY_BUFFER_GB`
**unset by default**, so azcopy uses its own CPU-based auto-tuning — ~1.78×
faster on the live cluster with no per-SKU code. Operators can still pin the
values on the worker via `WARMUP_AZCOPY_CONCURRENCY` / `WARMUP_AZCOPY_BUFFER_GB`;
when set they are injected as Job env vars and azcopy honours them.

## API / behaviour diff summary

- [api/services/warmup/scripts.py](../../../api/services/warmup/scripts.py):
  removed the `export AZCOPY_CONCURRENCY_VALUE=${…:-16}` /
  `AZCOPY_BUFFER_GB=${…:-2}` default lines so an unset var → azcopy auto.
- [api/services/warmup/jobs.py](../../../api/services/warmup/jobs.py):
  deleted the SKU formula (`vcpus_for_machine_type`,
  `recommended_azcopy_concurrency`, `recommended_azcopy_buffer_gb`,
  `DEFAULT_AZCOPY_*`, `MAX_AZCOPY_*`). `build_warmup_job_plan` /
  `_build_job` now take `azcopy_concurrency: int | None = None` and
  `azcopy_buffer_gb: int | None = None`; the azcopy env vars are injected
  **only when an override is not None**. `_validate_common` skips the range
  check for `None`.
- [api/tasks/storage/warmup.py](../../../api/tasks/storage/warmup.py): drops the
  SKU computation; passes the `WARMUP_AZCOPY_*` env overrides straight through
  (`None` when unset).
- No IaC change. No new dependency. The separate prepare-db
  `DEFAULT_AZCOPY_CONCURRENCY` in `api/services/k8s/prepare_db_jobs.py` is
  unrelated and unchanged.

## Validation evidence

- Live benchmark above (158 → 281.5 MB/s, 1.78×) on cluster-02.
- Updated tests in [api/tests/test_warmup_jobs.py](../../../api/tests/test_warmup_jobs.py):
  `test_plan_omits_azcopy_env_by_default_for_auto_tuning`,
  `test_plan_injects_azcopy_env_only_when_overridden`,
  `test_plan_rejects_out_of_range_azcopy_override`; the default-env plan test now
  asserts `AZCOPY_*` is absent.
- `uv run pytest -q api/tests/test_warmup_jobs.py` and the full
  `uv run pytest -q api/tests` pass; `uv run ruff check api` clean.

## Deploy

The default (unset) values flow through the warmup pod script which is baked into
the warmup ConfigMap built by the worker at Job-creation time — an api/worker
redeploy picks it up. The `WARMUP_AZCOPY_*` overrides live on the worker env.
