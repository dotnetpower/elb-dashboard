---
title: Service Bus consumer creates the jobstate row at drain time + full message-flow trace
description: The Service Bus drain handler now persists the durable jobstate row immediately (consumer = writer) and records an end-to-end message lifecycle trace surfaced on the job detail.
tags:
  - blast
  - architecture
---

# 2026-06-14 — Consumer-as-writer + message lifecycle trace (Tier 1 of #36)

First deliverable of the Service-Bus ingress unification design ([#36](https://github.com/dotnetpower/elb-dashboard/issues/36)).

## Motivation

The Service Bus consumer (`_drain_handler`) submitted to the OpenAPI plane and
wrote only a **bridge record** — it never created the `jobstate` row that the
Message Flow card and Recent searches read. That row was created much later by
the periodic `_sync_external_jobs_to_table` (~70 s discovery cache), so a job we
*ourselves* just submitted as the consumer was invisible to the dashboard until a
later poll. That late row creation was the structural root of the "appears too
late" latency and the reason webhook + polling + cache invalidation all existed
as separate patches.

Separately, the message lifecycle (enqueue → received → submit → run → result →
delivered) was scattered across the SB queue counts, the bridge record, the
jobstate row, and the OpenAPI status — there was no single place to answer "where
is this message right now and how long did each hop take".

## User-facing change

* **Consumer is the writer.** When the drain handler successfully bridges a
  Service Bus message to the execution plane, it now persists the durable
  `jobstate` row immediately (reusing the proven `_sync_external_jobs_to_table`
  so the row shape / self-heal rules stay identical and the later poll is a
  no-op). The job appears on the Message Flow card / Recent searches at drain
  time instead of after the ~70 s discovery poll.
* **Full message lifecycle trace.** The consumer records ordered lifecycle
  stages (`enqueued → received → row_created → routed → submitted`) and the
  status watcher records `running → succeeded|failed → completion_published`,
  each with its real timestamp. `GET /api/blast/jobs/{job_id}?history=1` now
  returns a derived `message_trace` with the ordered stages and the
  `queue_dwell_ms` / `submit_latency_ms` / `e2e_ms` metrics, so the dashboard can
  show where a message is and how long each hop took (answers "is Service Bus
  enqueuing late, or are we processing late?").

## Design notes (self-critique)

* Trace stages reuse the existing `jobhistory` table (one `mf.<stage>` event per
  stage) — no new schema, no new column.
* Recording is **best-effort**: a trace/row-creation failure is logged and
  swallowed so it can never abandon an already-accepted submit (abandoning would
  cause a duplicate submit on Service Bus redelivery).
* `derive_trace` keeps the **first** occurrence of each stage so an
  at-least-once redelivery cannot rewrite an earlier timestamp; metric math
  tolerates missing/out-of-order stages and never raises.
* The row is keyed by the OpenAPI `job_id` (matching the later sync + webhook
  paths) so no duplicate row is created.

## Scope / what is NOT in this change

This is Tier 1 of [#36](https://github.com/dotnetpower/elb-dashboard/issues/36).
The remaining tiers are tracked there and gated:

* Tier 2 — dashboard API submit enqueues to Service Bus instead of calling
  `/v1/jobs` directly (default-OFF flag).
* Tier 3 — resident long-polling consumer (≤1 s) replacing the 30 s beat
  (infra change, needs deploy + approval).
* Tier 4 — formal result-return push/pull contract + `event_id`/`attempt`
  idempotency + external consumer guide.
* Tier 5 — reduce discovery polling + webhook to safety-net only.

## API / IaC diff summary

No API surface or IaC change. Internal only:

* `api/services/blast/message_trace.py` (new) — `record_stage` /
  `derive_trace` / `MESSAGE_TRACE_STAGES`.
* `api/tasks/servicebus/tasks.py` — `_drain_handler` persists the jobstate row +
  records enqueued/received/row_created/routed/submitted via
  `_persist_drain_row_and_trace`; `publish_transitions` records
  running/terminal/completion_published via `_record_transition_trace`.
* `api/routes/blast/jobs.py` — `GET /jobs/{id}?history=1` attaches
  `message_trace` derived from the returned history rows.

## Validation evidence

* `uv run pytest -q api/tests` → **3584 passed, 3 skipped**.
* New tests:
  * `api/tests/test_message_trace.py` (9) — record/derive, dedup, metrics, best-effort.
  * `api/tests/test_servicebus_tasks.py` — `test_drain_persists_jobstate_row_and_trace`,
    `test_publish_transitions_records_trace`.
  * `api/tests/test_blast_job_message_trace_route.py` (2) — detail exposes/omits trace.
* `uv run ruff check` clean on all changed paths.
