---
title: elb-openapi num_cpus knob and the real core_nt concurrency ceiling
date: 2026-06-03
tags:
  - blast
  - openapi
  - concurrency
  - aks
---

# elb-openapi `num_cpus` knob and the real `core_nt` concurrency ceiling

## Motivation

The standing performance goal is "max throughput while preserving NCBI parity,
without changing `num_nodes`". A prior phase proved the live `core_nt` ceiling
is **2 concurrent jobs** on the 10-node `blastpool`, bounded by the per-shard
CPU **request** (`6` CPU/shard → `floor(15740m / 6000m) = 2` pods/node), and
that NCBI parity is guaranteed independently by the auto-injected full-DB
`-searchsp` (`32,156,241,807,668`).

A wrong "page-cache memory wall" model (memory scaling with job count) was
corrected: page cache is keyed per shard **file** and shared across processes,
so a node's footprint tracks the number of *distinct* shards it holds (≤ 10),
not the job count. That meant the 2-job ceiling is movable and the real limit
is CPU saturation — which had to be **measured**, not asserted.

## User-facing change

No user-facing behaviour change. Two code deliverables, both safe:

1. **Env-gated `ELB_OPENAPI_NUM_CPUS` knob** (default-OFF) added to the openapi
   build patch. When set ≥ 1 it writes `[cluster] num-cpus` into the generated
   `elastic-blast` config; the per-shard CPU request becomes `num-cpus − 2`.
   Unset = current `elastic-blast` profile default (`num_cpus=8`) = unchanged.
2. **Empirical ceiling measurement** recorded in the capacity-gate research doc
   (§9.6): with `num_cpus=6` the per-shard request drops to `4`, giving
   `floor(15740m / 4000m) = 3` pods/node and **3 concurrent `core_nt` jobs**.

The knob is **code only** — `IMAGE_TAGS["elb-openapi"]` stays `4.18` and the
shared `elb-openapi` deployment was restored to `4.18` / `MAX_ACTIVE=2` / no
`NUM_CPUS` immediately after the test.

## API / IaC diff summary

- `scripts/dev/patch-openapi-build-context.py` (+23): new idempotent
  `_insert_once` block (marker `ELB_OPENAPI_NUM_CPUS`) placed after the
  `searchsp` injection. Reads `os.environ["ELB_OPENAPI_NUM_CPUS"]`, parses int,
  and on `≥ 1` sets `config["cluster"]["num-cpus"]`. Default-OFF.
- `docs/research/aks-capacity-gate.md`: §9.5 live-env note corrected
  (`MAX_ACTIVE=2` is persisted, not unset); new §9.6 with the `num_cpus=6`
  measurement table.
- No Bicep, no `IMAGE_TAGS`, no Container App template change.

## Validation evidence

- `uv run ruff check scripts/dev/patch-openapi-build-context.py` → All checks passed.
- Patched sibling `docker-openapi/app/main.py` compiles (`py_compile`); both the
  `searchsp` and `num-cpus` injections present (grep parity count = 2).
- `az acr build` → `elb-openapi:test-numcpus` (digest `sha256:d1f6299b…`, 2m54s).
- 4-job `core_nt` burst (`scripts/e2e/concurrency/harness.py --mode burst --n 4`):
  - pod-watcher timeline peak `running_jobs=3`, `running_pods=30`,
    `phases={Running: 30, Pending: 10}` — **3 concurrent jobs confirmed**.
  - wall time 145.6 s for 4 jobs (3 done at 126 s, 4th at 145.6 s) ≈ 1.6×
    faster than the 2-job packing.
  - all 4 jobs `succeeded`.
- Shared service **restored**: image `4.18`, `ELB_OPENAPI_MAX_ACTIVE_SUBMISSIONS=2`,
  `ELB_OPENAPI_NUM_CPUS` absent, replicas `2/2`, 0 residual blast pods. Sibling
  `docker-openapi/` left clean (0 dirty).
- `uv run pytest -q api/tests/test_openapi_task.py` → 13 passed.
- `check_frontmatter.py` (52 pages) + `mkdocs build --strict` → OK.
