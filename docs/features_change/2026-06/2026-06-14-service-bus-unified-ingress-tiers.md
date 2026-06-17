---
title: Service Bus unified ingress — optional submit enqueue, resident consumer, idempotent completion
description: Tier 2-5 of the Service Bus ingress unification — default-OFF submit-to-SB front door, resident low-latency consumer, optional idempotent completion events, and the external-consumer result contract.
tags:
  - blast
  - architecture
---

# 2026-06-14 — Service Bus unified ingress: Tiers 2-5 of #36

Builds on the Tier 1 consumer-as-writer change. All behavioural switches ship
**default-OFF** so the live submit contract only changes by explicit opt-in
(charter §12a Rule 4).

## Motivation

Tier 1 made the Service Bus consumer the single writer of job state. Tiers 2-5
complete the unified-ingress design from
[#36](https://github.com/dotnetpower/elb-dashboard/issues/36): let the dashboard
funnel its own submits through Service Bus, drain them with low latency, deliver
optional push events to external services idempotently, and document the
external-consumer contract.

## User-facing change

* **Tier 2 — optional submit-to-SB front door (`ENABLE_SB_SUBMIT_INGRESS`,
  default-OFF).** When enabled (and Service Bus is on), `POST
  /api/v1/elastic-blast/submit` enqueues the request to Service Bus instead of
  calling `/v1/jobs` directly, returning the dashboard correlation id
  immediately. A publish failure falls back to the direct path (break-glass), so
  a Service Bus blip never drops a submit. When the flag is off the historical
  direct path is unchanged.
* **Tier 3 — optional resident consumer (`SERVICEBUS_RESIDENT_CONSUMER`,
  default-OFF).** When enabled, a resident long-polling consumer on the worker
  drains the request queue within ~1 s instead of waiting the 30 s beat. The beat
  drain task stays registered as the fallback reconcile, so the resident loop is
  an accelerator, never a single point of failure. The loop is bounded,
  interruptible (stops promptly), and backs off (capped) on a drain error instead
  of hot-looping.
* **Tier 4 — idempotent optional completion events.** Every `blast.transition`
  event published to a configured completion topic now carries `event_id`
  (stable per `correlation_id`+`status`) and `attempt` (1 on first publish, ≥2
  on re-publish), so an external subscriber can dedupe an at-least-once
  redelivery. `result_ref` continues to carry pointers only (never result bytes;
  charter §9).
* **Tier 5 — external-consumer result contract** documented in
  [architecture/service-bus-integration.md](../../architecture/service-bus-integration.md):
  pull (poll by `external_correlation_id`) plus optional push (subscribe a
  configured completion topic), dedupe-on-`event_id`, pointers-not-bytes, and
  the status poll as the canonical fallback so a missed event is never a lost
  result.

## Design notes (self-critique)

* Both runtime switches require the gate env **AND** `service_bus_enabled()` —
  a half-configured deployment never drops a submit into a void.
* The enqueue helper **raises** on a publish failure (does not swallow) so the
  route can fall back to the direct submit; a swallowed failure would lose the job.
* The resident loop never raises out of its body, exits promptly on stop, and
  backs off on error — a stuck/erroring consumer cannot hot-spin or wedge.
* `event_id` is deterministic so dedupe needs no shared state across the
  subscriber and the dashboard.

## API / IaC diff summary

No API surface change. New default-OFF env flags (documented in the architecture
page): `ENABLE_SB_SUBMIT_INGRESS` (api), `SERVICEBUS_RESIDENT_CONSUMER` (worker).
Internal:

* `api/services/blast/submit_ingress.py` (new) — gate + enqueue helper.
* `api/services/blast/resident_consumer.py` (new) — resident drain loop lifecycle.
* `api/tasks/servicebus/tasks.py` — `_event_id` / `_transition_event` idempotency
  builder applied to all three publish points.
* `api/routes/elastic_blast.py` — submit route enqueues when gated on, falls back
  to direct on failure.
* `api/celery_signals.py` — worker start/shutdown manage the resident consumer.

## Validation evidence

* `uv run pytest -q api/tests` → **3599 passed, 3 skipped**.
* New tests: `test_submit_ingress.py` (6), `test_resident_consumer.py` (7), plus
  Tier 4 cases in `test_servicebus_tasks.py` (`_event_id` deterministic,
  transition event idempotency keys).
* `uv run ruff check` clean; `mkdocs build --strict` succeeds; frontmatter guard passes.
