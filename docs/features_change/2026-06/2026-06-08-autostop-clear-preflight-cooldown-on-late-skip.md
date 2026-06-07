---
title: Auto-stop clears the fake preflight cooldown when the act task late-skips
description: When the idle evaluator preflight-stamps last_stop_at then the act task late-skips (e.g. the cluster was just restarted), the stamp is now rolled back so the dashboard does not show a phantom cooldown with no auto-stop countdown while the cluster is actually Running.
tags:
  - operate
  - architecture
---

# Auto-stop clears the fake preflight cooldown on late-skip (2026-06-08)

## Motivation

The idle auto-stop flow is two-phase: the beat task `evaluate_idle_clusters`
**decides**, and the per-cluster `auto_stop_aks` task **acts** (re-evaluates to
close the decide→act race, then calls `stop_aks`).

To guard against a slow beat tick double-enqueuing a stop, the beat driver
**preflight-stamps** `last_stop_at` (with a `enqueued:<reason>` marker) *before*
enqueueing the act task. If the act task then **late-skips** — most commonly
`provisioning:Starting` because the user (or an operator) restarted the cluster
during the queueing window — no cluster is actually stopped, but the preflight
`last_stop_at` stamp was never rolled back on that path. The
`auto_stop_aks` late-skip branch called `mark_auto_stop_event(stopped=False)`,
which only updated `last_skip_at` and left `last_stop_at` in place.

Consequence (reproduced live): the cluster is happily `Running`, but
`GET /api/aks/autostop/status` returns `verdict=keep, reason=cooldown,
next_stop_at="", seconds_until_stop=0` for the entire cooldown window — so the
dashboard shows a **phantom cooldown with no auto-stop countdown**, and the user
cannot see when the cluster will next be considered for stop.

This was caught while validating auto-stop end-to-end: a live trace showed
`auto_stop_aks late-skip cluster=elb-cluster-02 reason=provisioning:Starting`
right after a manual start, and the status endpoint then reported
`reason=cooldown` with no countdown.

(The evaluator's *enqueue-failure* path already rolled the stamp back; only the
act-task *late-skip* path was missing the rollback.)

## User-facing change

After the act task late-skips a stop, the dashboard auto-stop card shows a real
countdown again immediately (on the next evaluator tick) instead of a phantom
"cooldown" for up to the full cooldown window while the cluster is Running.

## API / IaC diff summary

- `api/services/auto_stop.py` — `mark_auto_stop_event` gains
  `clear_preflight_stop: bool = False`. When set (only with `stopped=False`) and
  the persisted `last_stop_reason` still starts with the `enqueued:` preflight
  marker, it blanks `last_stop_at` / `last_stop_reason`. A genuine recent stop
  (reason without the marker) is never erased.
- `api/tasks/azure/idle_autostop.py` — `auto_stop_aks` passes
  `clear_preflight_stop=True` on the late-skip path.
- No IaC change. Default behaviour unchanged for callers that do not pass the
  new flag.

## Validation evidence

- `uv run pytest -q api/tests/test_auto_stop.py api/tests/test_auto_stop_task.py` — 31 passed, including the new
  `test_mark_auto_stop_event_clears_preflight_stop_on_late_skip` (enqueued: stamp
  → late-skip → `last_stop_at` blanked) and
  `test_mark_auto_stop_event_clear_preflight_preserves_real_stop` (a real
  `idle:` stop is NOT erased).
- `uv run pytest -q api/tests -k "auto_stop or autostop or idle"` — 97 passed.
- `uv run ruff check` on the changed files — clean.
- Live evidence of correct auto-stop overall: trace
  `auto_stop_aks completed cluster=elb-cluster-02 reason=idle:240m` at
  2026-06-07T13:32 matched last activity 09:24:51 + idle_minutes 240 = 13:25,
  stopped on the next 5-min evaluator tick (13:29) — the idle clock, evaluator
  cadence, and stop call are all correct; this fix only removes the phantom
  cooldown on the late-skip edge.
