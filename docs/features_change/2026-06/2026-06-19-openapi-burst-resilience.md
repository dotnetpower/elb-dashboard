---
title: elb-openapi burst resilience (memory + liveness hardening)
description: Raise the elb-openapi Deployment memory/cpu limits and relax its liveness probe so a high-concurrency core_nt submit burst no longer OOMKills the single-replica submit/dispatch pod and drives it into a restart loop.
tags:
  - blast
  - operate
---

# elb-openapi burst resilience

## Motivation

A load test of ~50 concurrent `core_nt` BLAST submits against `elb-cluster-01`
via the internal `elb-openapi` `/v1/jobs` endpoint
([issue #54](https://github.com/dotnetpower/elb-dashboard/issues/54)) surfaced a
submit/dispatch single point of failure: under the burst the `elb-openapi` pod
hit `exitCode 137` (OOMKilled) at its `memory: 512Mi` limit, then entered a
liveness death loop — `/healthz` missed its strict `timeoutSeconds: 5` deadline
`failureThreshold: 3` times in a row, so Kubernetes restarted an otherwise
recoverable pod. The pod is intentionally a **single replica** (it owns the
in-memory job queue and enforces `ELB_OPENAPI_MAX_ACTIVE_SUBMISSIONS` against a
process-local view), so killing it took the whole submit path down and every
client submit became a `ReadTimeout`. The queue mechanism itself was correct,
but `elb-openapi` died before the queue could drain, so 0 BLAST pods ran.

## User-facing change

The dashboard-generated `elb-openapi` Deployment manifest now ships harder
limits and a more forgiving liveness probe, so a transient submit burst no
longer crashes the submit/dispatch path. Operators must redeploy `elb-openapi`
(the **Deploy elb-openapi** action / API Reference page) for the new manifest to
take effect — the bumped manifest revision makes the dashboard report the live
Deployment as `manifest_outdated` until it is redeployed.

## API / IaC diff summary

`api/tasks/openapi/manifests.py` — `build_manifests` Deployment container:

* `resources.limits.memory`: `512Mi` → `2Gi` (headroom over the observed OOM
  ceiling).
* `resources.limits.cpu`: `500m` → `1` (keeps `/healthz` responsive under the
  burst so liveness does not time out while the pod is merely busy).
* `resources.requests`: `cpu 100m / memory 256Mi` → `cpu 250m / memory 512Mi`.
* `livenessProbe.timeoutSeconds`: `5` → `10`.
* `livenessProbe.failureThreshold`: `3` → `6` (≈3 min of sustained
  unresponsiveness before a restart — a genuine wedge, not a transient spike).
* `readinessProbe` unchanged (stays strict so a transient spike still pulls the
  pod out of the Service rotation quickly).

`api/tasks/openapi/constants.py` — `OPENAPI_MANIFEST_REVISION`: `2` → `3` so the
dashboard surfaces a pre-change live Deployment as outdated and prompts a
redeploy.

The **single replica** invariant is deliberately preserved: a second replica
would multiply the effective run-concurrency ceiling and strand queued jobs on
whichever replica the LoadBalancer routed them to, so the issue's "consider 2
replicas" suggestion is intentionally **not** adopted. One authoritative queue
owner remains the contract.

## Out of scope (cluster-side, sibling repo)

The issue's other two findings are not fixable from this repo's manifest:

* **Node ephemeral-storage exhaustion** from concurrent per-pod `core_nt`
  staging to node SSD lives in `elastic-blast-azure` (the BLAST shard pods, not
  the `elb-openapi` pod). Bounding concurrent DB staging or using a shared RWX
  DB volume / larger ephemeral-storage nodes is tracked there.
* **Submit throughput ≈ 1 job / ~9s** is a property of the sibling OpenAPI
  service's serialized submit path, not the Deployment manifest.

## Validation evidence

* `uv run pytest -q api/tests/test_openapi_task.py` — 18 passed. The
  `test_build_manifests_single_queue_owner` test now also asserts the hardened
  values (`limits.memory == "2Gi"`, `limits.cpu == "1"`,
  `livenessProbe.timeoutSeconds == 10`, `livenessProbe.failureThreshold == 6`,
  `readinessProbe.failureThreshold == 3`) so a later edit cannot silently
  regress them.
* `uv run pytest -q api/tests/test_openapi_deployment.py` — the
  `manifest_outdated` flagging tests stay green; they read
  `OPENAPI_MANIFEST_REVISION` dynamically, so the bump to 3 is picked up
  automatically.
* `uv run ruff check api/tasks/openapi/manifests.py api/tasks/openapi/constants.py`
  — clean.

The live re-run of `scripts/e2e/concurrency/run.sh burst 50` (acceptance
criteria 1, 3, 4) is gated on the maintainer starting `elb-cluster-01` (it was
`Stopped` at the time of this change) and redeploying `elb-openapi` with the new
manifest. It is captured as the remaining follow-up on issue #54.
