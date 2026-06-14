---
title: Service Bus bridge + webhook stability hardening (critique pass)
description: A design-critique pass over the Service Bus ingress / completion-bridge / webhook paths fixed a terminal-state resurrection bug, added per-bridge partial-failure isolation to publish_transitions, and corrected two observability/contract defects.
tags:
  - blast
  - operate
---

# 2026-06-15 — Service Bus bridge + webhook stability hardening

## Motivation

A focused self-critique pass (contract/state-machine, liveness, idempotency,
partial-failure, observability) over the recently-shipped Service Bus
unified-ingress + completion-bridge + webhook code surfaced four defects. None
were caught by the existing focused tests because they live in the design seams
the mechanical review cannot see (out-of-order delivery, one-item-aborts-the-batch,
dead branches, swallowed error reasons).

## User-facing change

* **Webhook no longer resurrects a finished job (Medium).** The sibling
  `elb-openapi` pod fires lifecycle webhooks with a 3-retry exponential backoff,
  so a stale `running`/`submitted` notification can be delivered *after* the job
  already reached a terminal state. The receiver only guarded a `running` row
  against backward `submitted`/`queued` events — a **terminal** `completed` /
  `failed` / `cancelled` row could be flipped back to `running` by a late event,
  making a finished job re-appear as in-flight on the dashboard. Terminal rows
  are now immutable against non-terminal events (`reason: "terminal_locked"`); a
  genuine terminal→terminal correction is still accepted (the sibling stays
  authoritative for terminals).
* **One flaky bridge no longer stalls the whole completion tick (Low-Medium).**
  `publish_transitions` had no per-bridge exception isolation: a transient
  tracking-store write (`mark_published` / `mark_done`) raising aborted the
  entire tick and starved the remaining active bridges until the next beat run.
  The per-bridge body is now isolated (matching `drain_requests` /
  `reconcile_stale_jobs`); a failing bridge increments an `errors` counter and
  the tick continues. Any already-published event is deduped by `event_id` on
  the subscriber; the bridge marker advances on the next tick.

## API / internal diff summary

* `api/routes/blast/external_webhook.py` — `_apply_to_jobstate` adds a
  terminal-lock guard (`cur_status` terminal + incoming non-terminal → ignore).
  New response `reason: "terminal_locked"`.
* `api/tasks/servicebus/tasks.py` — extracted `_publish_one_bridge` helper
  (returns `(published_delta, finished_delta)`); `publish_transitions` wraps it
  per bridge and reports a new `errors` counter (additive — existing keyed
  consumers unaffected). Removed a dead `attempt = 2 if …` branch (it was
  unreachable; the equal-status case `continue`s earlier, so `attempt` was
  always 1) and corrected the `_transition_event` docstring to state that
  `event_id` is the authoritative dedup key.
* `api/services/blast/message_trace.py` — `record_stage` exception handler now
  logs the real exception instead of re-logging the stage name (the failure
  cause was being silently dropped).

## Validation evidence

* New regression tests:
  * `test_external_webhook.py::test_register_external_job_terminal_row_not_resurrected_by_late_event`
    (12 parametrized cases: 3 terminal × 4 late non-terminal statuses) +
    `…_terminal_correction_still_applies` (3 cases).
  * `test_servicebus_tasks.py::test_publish_transitions_isolates_one_failing_bridge`.
* `uv run ruff check api` — clean.
* `uv run pytest -q api/tests` — **3623 passed, 3 skipped** (was 3607; +16 new).
* No behaviour change for the `attempt` field (it was already always 1) or the
  `publish_transitions` return contract (the `errors` key is additive).
