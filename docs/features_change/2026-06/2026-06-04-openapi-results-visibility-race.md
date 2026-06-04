---
title: OpenAPI completed→/results 404 race fix (4.19 → 4.20)
description: Gate the OpenAPI job completed state on actual result-blob listing so a status poll that just saw completed cannot 404 on /jobs/{id}/results.
tags:
  - blast
  - architecture
---

# 2026-06-04 — OpenAPI `completed`→`/results` 404 race fix (4.19 → 4.20)

## Motivation

A client polling `GET /v1/jobs/{job_id}/status` against the on-AKS
`elb-openapi` pod (sibling `docker-openapi/app/main.py`) would see `status`
flip to `completed`, immediately call `GET /v1/jobs/{job_id}/results`, and get
a **404** — yet retrying a few seconds later succeeded.

Root cause was an **asymmetric completion contract** in
`_refresh_job_status()`:

* The marker branch (evaluated first) returned `completed` as soon as
  `metadata/SUCCESS.txt` was visible, **without** verifying that the result
  artifacts were listable.
* The Kubernetes-summary branch only declared `completed` when
  `_list_result_files()` actually returned files.

The finalizer (`elb-finalizer-aks.sh`) uploads every artifact (shard
`batch_*.out.gz`, and in DB-partitioned runs `merged_results.out.gz`)
**before** writing `SUCCESS.txt`, so when the marker is visible the artifacts
are durably stored. But the `azcopy` List that `/results` relies on can briefly
lag behind the marker write (Azure Blob list-after-write visibility), so the
marker branch could announce `completed` during a window where `/results`
would still 404.

## User-facing change

No dashboard UI change. The fix lives in the sibling `elb-openapi` service.
After it ships, an OpenAPI job only reports `completed` (and the dashboard's
external job view only reports `success`) once the result listing the download
path uses is actually populated, eliminating the `completed`→`/results` 404
race. The dashboard's own `/api/blast/jobs/{id}/results` already degraded
gracefully on the transient 404, so this is a correctness/latency improvement,
not a crash fix on our side.

## API / IaC diff summary

* Sibling `elastic-blast-azure` (separate repo, committed + pushed by
  maintainer):
  * `feat(api): implement RESULTS_VISIBILITY_GRACE_SECONDS and enhance job
    status handling for completed markers` (`5c9c6e54`) — `_refresh_job_status`
    now gates the `SUCCESS.txt` marker on `_list_result_files`; holds at
    `running/finalizing` until the listing catches up; records
    `success_marker_seen_at` and trusts the durable marker after
    `RESULTS_VISIBILITY_GRACE_SECONDS` (default 120 s, env-tunable) so a
    listing that never catches up cannot wedge the job in a non-terminal
    state. `FAILURE.txt` stays immediately terminal. Adds 4 unit tests in
    `tests/openapi/test_queue.py`.
  * `chore(api): bump VERSION to 3.7.5` — `VERSION 3.7.4 → 3.7.5` in
    lock-step with the fix.
* Dashboard `api/services/image_tags.py` — `elb-openapi` pin `4.19 → 4.20`
  (4.20 == upstream 3.7.5), comment block updated with the mapping.

## Rollout order (charter)

Per `docs/features_change/2026-05/2026-05-29-openapi-critique-fixes.md`
"Rollout order" (and the 2026-05-30 P0 rollback that exists because the order
was inverted), the safe sequence is:

1. Sibling repo: `VERSION 3.7.4 → 3.7.5` committed + pushed (done).
2. Build + push the patched `elb-openapi:4.20` image to ACR **from the
   dashboard-patched local context**:
   ```bash
   python scripts/dev/patch-openapi-build-context.py ~/dev/elastic-blast-azure/docker-openapi
   az acr build -r <acr> -t elb-openapi:4.20 -f Dockerfile ~/dev/elastic-blast-azure/docker-openapi
   ```
3. **Only after** the image exists in ACR, move the pin in
   `api/services/image_tags.py` (`4.19 → 4.20`) and roll the deployment.

## Validation evidence

* Sibling tests: `tests/openapi/test_queue.py` — **22 passed** with
  `ELB_OPENAPI_ALLOW_UNAUTHENTICATED=1 CONTROL_PLANE_URL=http://localhost`
  (the 4 new marker tests: listing-not-ready holds `finalizing`; listing-ready
  completes; grace-elapsed trusts the marker; `FAILURE` is immediately
  terminal).
* The 8 unrelated failures seen without the auth env vars reproduce on a clean
  `git stash` of the fix, confirming they are pre-existing environment-config
  failures, not regressions from this change.
