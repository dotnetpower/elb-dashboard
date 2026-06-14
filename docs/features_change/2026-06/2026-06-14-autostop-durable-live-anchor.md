---
title: Durable live-activity anchor for AKS auto-stop
description: Stop the idle "Stops in" countdown from lurching on refresh and the cluster from stopping earlier than shown by persisting the live K8s BLAST activity high-water mark.
tags:
  - operate
  - blast
---

# Durable live-activity anchor for AKS auto-stop

## Motivation

Users reported that the cluster card's **"Stops in"** countdown changed
unpredictably on refresh and, worse, the AKS cluster sometimes **stopped
suddenly earlier than the displayed deadline**.

Root cause: the idle deadline is recomputed from scratch on every evaluation as
`deadline = max(jobstate rows, last_started_at, live K8s probe) + idle_window`.
For OpenAPI-submitted BLAST runs — which never write a dashboard `jobstate` row
— the **only** anchor for recent activity is the live Kubernetes `app=blast`
probe (`live_latest_activity`). That probe is best-effort and **regresses**: it
returns nothing when the cluster API server briefly blinks, or once a finished
run's Job/Pods are garbage-collected. The instant the probe stopped seeing the
run, the anchor fell back to the much-older `created_at` value, so:

* the SPA "Stops in" number jumped backward between polls (the frontend
  `stabilizeDeadline` only smooths jitter ≤ 45 s, not a multi-minute regression);
  and
* the next beat tick computed `seconds_left <= 0` and **stopped the cluster
  early**, out of sync with the countdown the user had just seen.

## User-facing change

* The "Stops in" countdown is now anchored on a **durable, monotonic**
  record of the last observed live BLAST activity, so it stays stable across
  refreshes.
* The cluster stops at (or just after, bounded by the 5-min beat cadence) the
  displayed deadline — never abruptly earlier because the live probe blinked or
  the run's pods were garbage-collected.
* No visible UI change; the existing countdown / Extend controls behave the
  same, just consistently.

## API / IaC diff summary

Backend-internal only — no wire-shape, route, or Bicep change. The
`/api/aks/autostop/status` and `/api/aks/autostop` response shapes are
unchanged (the new field is not exposed to the browser).

* `api/services/auto_stop.py`
  * New `AutoStopPreference.last_live_activity_at` field (round-trips through
    `to_dict` / `from_dict`, both Table and file backends).
  * New `mark_auto_stop_live_activity(...)` helper — advance-only, idempotent,
    CAS-guarded, best-effort. A no-op when the observation is not newer than the
    stored anchor, so the high-frequency status route does zero Table writes in
    steady state.
* `api/services/auto_stop_evaluator.py` — folds `last_live_activity_at` into
  the idle anchor exactly like `last_started_at` (monotonic max; never moves the
  anchor into the future, so it cannot defer a stop indefinitely).
* `api/tasks/azure/idle_autostop.py` — `_live_blast_signal` persists the
  probe's high-water mark (beat + act paths).
* `api/routes/aks/autostop.py` — `_compute_status` persists the probe's
  high-water mark (advance-only, best-effort); the PUT handler carries
  `last_live_activity_at` forward so toggling the setting does not wipe it.

## Validation evidence

* `uv run pytest -q api/tests/test_auto_stop.py api/tests/test_auto_stop_evaluator.py api/tests/test_auto_stop_task.py api/tests/test_auto_stop_live.py api/tests/test_preference_etag.py api/tests/test_aks_autostop_route.py` → 112 passed.
* New tests:
  * `test_last_live_activity_at_round_trips_through_dict`
  * `test_mark_auto_stop_live_activity_advances_and_persists`
  * `test_mark_auto_stop_live_activity_is_monotonic` (older observation is a
    no-op — proves the anchor cannot regress)
  * `test_mark_auto_stop_live_activity_noop_when_no_pref`
  * `test_persisted_live_activity_resets_idle_clock_without_a_live_probe`
    (probe `None` this tick, persisted anchor still holds the deadline)
  * `test_stale_persisted_live_activity_does_not_prevent_stop` (anchor only ever
    moves forward — a stale value still lets the cluster stop)
* Full backend suite: `uv run pytest -q api/tests` → 3568 passed, 3 skipped.
* Lint: `uv run ruff check` on all changed files → clean.
