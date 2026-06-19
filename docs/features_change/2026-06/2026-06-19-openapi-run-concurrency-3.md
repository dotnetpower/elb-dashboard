---
title: elb-openapi run-concurrency 2 → 3 (admit cap + num-cpus)
description: Default the OpenAPI dispatcher admit cap to 3 and pin elastic-blast num-cpus to 7 so three BLAST jobs co-schedule per E16 node, matching the sibling service's intended BLAST_MAX_RUN_CONCURRENCY of 3.
tags:
  - blast
  - operate
---

# elb-openapi run-concurrency 2 → 3

## Motivation

The sibling OpenAPI service's run-concurrency design target is **3**
(`BLAST_MAX_RUN_CONCURRENCY` default = 3 in `submit_coordination.py`). But the
dashboard's generated manifest set the dispatcher admit cap
`ELB_OPENAPI_MAX_ACTIVE_SUBMISSIONS = 2` (derived from
`floor(15.74 / 6) = 2` shard pods per E16 node at the default `num-cpus=8`
→ request 6). With the admit cap at 2, **only 2 jobs ever entered the active
pipeline**, so the intended 3-way concurrency could never be reached — and a
warm `core_nt` burst was observed peaking at **1–2** concurrent running jobs.

## User-facing change

The dashboard now deploys `elb-openapi` to run **3 BLAST jobs concurrently**:

- `ELB_OPENAPI_MAX_ACTIVE_SUBMISSIONS` default `2 → 3` (admit cap aligned with
  the sibling's `BLAST_MAX_RUN_CONCURRENCY = 3`).
- New env `ELB_OPENAPI_NUM_CPUS = 7` → elastic-blast shard pod CPU
  `request = num-cpus - 2 = 5`, so `3 × 5 = 15 < 15.74` allocatable and the 3rd
  job's pods schedule instead of staying `Pending`. The per-pod CPU drops `8 → 7`,
  trading a little single-job speed for 3-way parallelism.
- `OPENAPI_MANIFEST_REVISION 3 → 4` so the dashboard prompts a redeploy.

## Trade-off / SKU note

This is tuned for the **Standard_E16s_v5** blast pool (~15.74 allocatable CPU).
On a different node SKU, retune `num-cpus` so
`floor(allocatable / (num-cpus - 2))` equals the desired concurrency. Raising
the admit cap without the matching CPU drop would only leave the extra job's
pods `Pending`.

## Validation evidence

- Live, on `elb-cluster-01` with `core_nt` warm: patched the deployment to
  `MAX_ACTIVE_SUBMISSIONS=3` + `NUM_CPUS=7` and ran a `core_nt` burst →
  **3 distinct `elb-job-id` running concurrently, 0 `Pending`**, shard
  `cpu request = 5` (confirmed on the live Job spec). Before the change the same
  warm `core_nt` burst peaked at 1–2.
- `uv run pytest -q api/tests/test_openapi_task.py api/tests/test_openapi_deployment.py`
  → 21 passed (assertions updated to `MAX_ACTIVE_SUBMISSIONS=3` +
  `NUM_CPUS=7`). `ruff` clean.

The live deployment was left at the validated 3-config for the maintainer's
session; the durable change ships when this commit is deployed through the
normal `Deploy elb-openapi` flow.
