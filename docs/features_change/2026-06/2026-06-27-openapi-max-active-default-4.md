---
title: elb-openapi MAX_ACTIVE_SUBMISSIONS default 4 (manifest revision 5)
description: Permanent fix for the live kubectl env patch that bumped MAX_ACTIVE_SUBMISSIONS 3 to 4 during the 2026-06-27 SB Tier-B tuning; raises the manifest default so the next deploy preserves the change.
tags: [release, blast, operate]
---

# elb-openapi `MAX_ACTIVE_SUBMISSIONS` default → 4 (manifest revision 5)

## Motivation

The 2026-06-27 Service Bus throughput tuning ran a live
`kubectl set env deploy/elb-openapi ELB_OPENAPI_MAX_ACTIVE_SUBMISSIONS=4` to
match `SERVICEBUS_DRAIN_CONCURRENCY=4` on the worker. A live env patch is
wiped on the next `kubectl apply` (re-deploy / new revision), so the throughput
gain measured in the warmed N=10 burst (E2E p95 = 7.2 min, SLO ≤ 10 min)
would silently regress to the legacy ~2 sub/min ceiling on the next deploy.

The sibling `elastic-blast-azure` repo does **not** ship an installed
deployment manifest for `elb-openapi`; the dashboard's `api/tasks/openapi/`
package builds the manifest in-process and applies it via `kubectl apply`,
so the permanent home for this default lives in the dashboard repo.

## User-facing change

| File | Change |
|---|---|
| [api/tasks/openapi/manifests.py](../../../api/tasks/openapi/manifests.py) | `build_manifests(max_active_submissions: int = 3)` → `= 4` |
| [api/tasks/openapi/constants.py](../../../api/tasks/openapi/constants.py) | `OPENAPI_MANIFEST_REVISION = 4` → `= 5` (history line documents the bump + live memory model) |
| [api/tests/test_openapi_task.py](../../../api/tests/test_openapi_task.py) | Default assertion `"3"` → `"4"` |

## Memory model (live-validated 2026-06-18)

`openapi_mem (MiB) ≈ 268 + 70 × MAX_ACTIVE`. At `MAX_ACTIVE=4` the peak is
~548 MiB on a 2 Gi memory limit (~73% headroom). Raising further past 4 is
not useful because the BLAST shard-pod fan-out per E16 node already caps
useful run-parallelism at 3 distinct jobs (`ELB_OPENAPI_NUM_CPUS=7` with
shard-request 5 ⇒ `floor(15.74/5)=3` per node), so additional admit-cap
does not translate to sustained throughput, only to more memory pressure.

## Validation evidence

- `uv run pytest -q api/tests/test_openapi_task.py api/tests/test_control_plane_env.py` → 28 passed.
- Live verification on the customer dev cluster (`elb-cluster-01`, sub `d0747f40`):
  the kubectl-patched pod ran `MAX_ACTIVE=4` for both the cold-start N=10 and
  the warmed N=10 burst; warmed E2E p95 was 7.2 min (SLO PASS) with peak
  memory under the 2 Gi limit (no OOM, no restart loop).

## Operator note — revision bump

`get_openapi_deployment_status` compares the live `elb-dashboard/manifest-revision`
annotation against `OPENAPI_MANIFEST_REVISION` (now `5`). A previously-deployed
elb-openapi (revision `4` annotation) will report `manifest_outdated` in the
API Reference panel until the operator re-runs the "Deploy elb-openapi" task.
This is the dashboard's standard signalling for any manifest change that only
takes effect on redeploy; no other action is required.

## Out of scope

- No code change in `elastic-blast-azure`. The sibling reads
  `ELB_OPENAPI_MAX_ACTIVE_SUBMISSIONS` from the pod env at process startup,
  and that env is sourced from the dashboard-built manifest.
- No change to BLAST per-job runtime or shard fan-out; the throughput gain
  is entirely from the OpenAPI dispatch queue being able to admit 4 in-flight
  submits instead of 3.
