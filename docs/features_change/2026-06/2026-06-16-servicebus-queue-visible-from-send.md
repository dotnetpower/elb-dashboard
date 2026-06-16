---
title: Service Bus requests show as queued in job list + Message Flow from send
description: A BLAST request enqueued via Service Bus now writes a queued placeholder jobstate row at send time, so it appears in Recent searches and the Message Flow card the instant it lands on the queue — not only after the ~30s drain tick.
tags:
  - blast
  - operate
  - ui
---

# Queue-visible-from-send for Service Bus BLAST requests

## Motivation

A BLAST request enqueued onto the Service Bus request queue was invisible until
the next `drain_and_resubmit` beat tick (~30 s): only then did the consumer
(consumer=writer) create the durable jobstate row, so Recent searches and the
Message Flow card showed nothing for up to half a minute after the operator
sent it. The request should be visible — as `queued` — the instant it lands on
the queue.

## User-facing change

* **Recent searches / job list** — a Service Bus send immediately shows the job
  with status `queued`.
* **Message Flow card** — the job appears as a `queued` broker box the instant
  it is enqueued (the card already draws `queued`/`pending` jobstate rows; the
  placeholder simply joins that active set), with the `enqueued` lifecycle
  stage recorded.

Once the consumer drains the message (~30 s, or immediately via "Run consumer
now"), the real OpenAPI-keyed row takes over and the placeholder is superseded —
the list shows a single row throughout.

## How it works

A correlation-id-keyed **placeholder** jobstate row is written at send time and
reconciled away by the drain path:

* **send** (`POST /settings/service-bus/send`) → `create_queued_placeholder`
  writes a `status=queued` row keyed by the `external_correlation_id`, tagged
  `payload.placeholder=True`, with the `enqueued` message-flow stage. Best-effort
  — a placeholder failure never fails the already-enqueued send. A dry-run does
  not create one.
* **drain success** → the real OpenAPI-`job_id`-keyed row is created (unchanged),
  then `supersede_placeholder` soft-deletes the placeholder (`status=deleted`,
  which the `status ne 'deleted'` list filter hides). Distinct rows by key, so
  the real row carries the job forward.
* **drain dead-letter** (permanent 4xx / un-buildable payload) →
  `fail_placeholder` terminalises the placeholder so it does not linger as
  `queued` after the message is DLQ'd.
* **drain abandon** (transient) → the placeholder stays `queued` (correct — the
  message is still being retried).

## API / IaC diff summary

### Backend (`api/`)

* `api/services/blast/servicebus_placeholder.py` (new) — `create_queued_placeholder`
  / `supersede_placeholder` / `fail_placeholder`. All best-effort; supersede/fail
  only touch rows tagged `payload.placeholder=True` so a real job is never
  clobbered.
* `api/routes/settings/service_bus.py` — `send` creates the placeholder after the
  audit write (skipped on dry-run).
* `api/tasks/servicebus/tasks.py` — `_drain_handler` supersedes the placeholder on
  success and fails it on a permanent rejection / malformed message (correlation
  id recovered from the raw body for the un-buildable case).
* `api/tasks/blast/reconcile_task.py` — the time-based `worker_lost` guard now
  also exempts placeholder rows (`payload.placeholder=True`), mirroring the
  external-origin exemption: a worker that is down for >10 min cannot drain the
  still-queued message, so the placeholder is legitimately `queued` and must not
  flip to a false `failed` (self-critique hardening).

## Validation evidence

* New `api/tests/test_servicebus_placeholder.py` (9): create writes a queued row +
  `enqueued` stage; idempotent on duplicate send; blank correlation id no-ops;
  supersede soft-deletes only placeholder rows; fail terminalises only
  placeholder rows; missing-row no-ops.
* `api/tests/test_servicebus_tasks.py` (+3): drain supersedes the placeholder on
  success, fails it on a permanent 4xx, and fails it on a malformed message.
* `api/tests/test_settings_service_bus.py` (+2): a real send creates the
  placeholder; a dry-run does not.
* `api/tests/test_blast_tasks.py` (+1): reconcile does not mark a quiet
  placeholder `worker_lost`.
* Full backend: `uv run pytest -q api/tests` → 3873 passed, 3 skipped.
* `uv run ruff check api` clean.

## Design notes (self-critique hardening)

* **Contract**: placeholder is a normal `queued` dashboard row — every consumer
  (job list, Message Flow `_ACTIVE_STATUSES`) already handles `queued`; no SPA
  change needed.
* **Liveness**: superseded on drain success, failed on dead-letter, exempted from
  the reconcile `worker_lost` path while queued. The one residual is a transient
  message that reaches max-delivery and is broker-auto-moved to the DLQ — the
  `_drain_handler` never sees that move, so its placeholder lingers as `queued`
  until the operator promotes/deletes the DLQ message (rare; documented, not
  over-engineered with a TTL).
* **Idempotency**: `repo.create` swallows `ResourceExistsError`, so a duplicate
  send (at-least-once / double-click) reuses the one placeholder; supersede/fail
  are idempotent and placeholder-tagged.
* **Partial failure**: every placeholder write is best-effort — it never blocks a
  send or abandons a drained message.
* **Security**: only sanitised `program`/`db` + the caller's own oid are stored;
  no raw query FASTA, no SAS token.
