---
title: OpenAPI dispatcher reclaims dead-thread submit slots (#62)
description: The elb-openapi watchdog now releases a MAX_ACTIVE dispatch slot held by a submit whose thread died on a cluster stop/start, instead of waiting 2h.
tags:
  - blast
  - operate
---

# OpenAPI dispatcher reclaims dead-thread submit slots (#62)

## Motivation

When the AKS cluster was **stopped while a BLAST submit was in flight**, the
elb-openapi pod restarted and recovered the job in the `submitting` state, but
the submit could never complete — the in-process submit thread died with the
cluster. The job stayed `submitting` indefinitely, permanently occupying one of
the `ELB_OPENAPI_MAX_ACTIVE_SUBMISSIONS` (=3 in production) dispatch slots. A few
such zombies **deadlocked the dispatcher**: every newly queued job stayed
`queued` forever and Service Bus queue throughput dropped to **zero** even though
the queue was intact and the cluster was healthy again.

The pre-existing watchdog only failed a stuck `submitting` job after
`SUBMIT_STUCK_SECONDS` (default **7200s = 2h**), far too long to break a
post-restart deadlock.

## User-facing change

After a cluster stop-during-submit, the dispatcher now recovers **automatically
within one watchdog tick** (`ELB_OPENAPI_WATCHDOG_INTERVAL_SECONDS`, default 60s)
instead of stalling for up to 2 hours. Service Bus queue throughput resumes on
its own with no operator intervention (previously an operator had to manually
cancel every wedged job).

## API / IaC diff summary

Sibling repo `dotnetpower/elastic-blast-azure` (`docker-openapi/app/main.py`):

- New `_reclaim_dead_thread_job(job_id, refreshed)` helper. `_watchdog_once` now,
  for a `dispatching`/`submitting` job whose submit thread is **dead**
  (`not _has_alive_thread`) and past a short `RECLAIM_GRACE_SECONDS` (default 45s,
  avoids racing a just-claimed job whose thread has not started yet):
  - **requeues** to `queued` when the submit created no BLAST k8s work
    (`k8s_summary.total == 0 and submit_failed == 0`), bounded by
    `ELB_OPENAPI_SUBMIT_MAX_RETRIES` (default 3) so a job that keeps losing its
    thread is **failed** instead of re-sticking the dispatcher forever;
  - **leaves untouched** a job that already created k8s work (re-submitting would
    duplicate Jobs) — the normal status refresh carries it to running/terminal.
- A submit thread that is **alive** (a legitimately cold-staging submit waiting
  for nodes) is **never touched**, so the reclaim can never cancel healthy work.
- New env knobs: `ELB_OPENAPI_RECLAIM_GRACE_SECONDS` (45), `ELB_OPENAPI_SUBMIT_MAX_RETRIES` (3).
- **Startup reconcile shares the same bound.** `_reconcile_recovered_jobs` (the
  startup-only recovery pass) now delegates to the same `_reclaim_dead_thread_job`
  helper instead of requeueing unconditionally — see the live-validation note
  below for why this was essential.

Dashboard repo: `api/services/image_tags.py` pin `elb-openapi` **4.26 → 4.27**.

## Build / rollout note

`elb-openapi:4.27` was built directly from the **local sibling context**
(`az acr build --registry acrelbdashboard3abp67bppe --image elb-openapi:4.27
~/dev/elastic-blast-azure/docker-openapi`) and pushed to the moonchoi ACR
(final digest `sha256:c2399f43…`, after the live-validation rebuild below). The
historical `scripts/dev/patch-openapi-build-context.py`
step was **not** used: the sibling master has natively absorbed every app- and
Dockerfile-level patch it used to inject (the `eta.py` overlay is now a tracked
sibling file), so the patch script's `patch_app` anchors no longer match and it
is effectively retired for this image. Per the charter rollout order, the image
was built and pushed **before** moving the pin here.

## Validation evidence

- Sibling unit tests: `docker-openapi/tests/test_watchdog_reclaim.py` — **9
  green** (reclaim→queued, fail-after-max-retries, never-touch-alive-thread,
  grace-skips-just-dispatched, leave-job-with-k8s-work, two helper-contract,
  plus the two reconcile-bound tests added after live validation). Full
  `docker-openapi` suite: **93 passed** (1 unrelated `test_passthrough_fields`
  failure pre-dates this change — verified by stashing the fix).
- **Live validation (2026-06-21, moonchoi `elb-cluster-01`).** Deployed 4.27 to
  a fresh `elb` namespace (ACR pull verified), then loaded synthetic dead-thread
  `submitting` zombie jobs (the exact post-restart stuck state #62 describes) via
  job ConfigMaps + a pod restart:
  - attempt=1 zombie → log `watchdog reclaimed dead-thread job … -> queued
    (attempt 1/3)`, ConfigMap status flipped `submitting` → `queued` within one
    tick: **slot released**.
  - attempt=3 (budget-spent) zombie → log `watchdog failed dead-thread job …
    after 3 attempts (slot released)`, status `failed`/`stuck_cancelled` and
    **stayed failed**.
- **Live validation caught a real bug.** The *first* live run showed the
  budget-spent zombie end up back at `queued/recovered` despite the
  `watchdog failed …` log — the startup-only `_reconcile_recovered_jobs` was
  requeueing it **unconditionally**, resurrecting a job the watchdog had just
  failed and re-wedging the dispatcher across the restarts #62 is about. Fixed by
  routing reconcile through the same bounded `_reclaim_dead_thread_job` (sibling
  commit `96e8fb89`); the rebuilt 4.27 (`sha256:c2399f43…`) re-validated clean
  (attempt=3 zombie stays `failed`). Test resources + the cluster were torn down
  after validation.
