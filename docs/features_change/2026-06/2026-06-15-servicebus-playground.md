---
title: Service Bus Playground (preview) — send, drain, and observe BLAST requests
description: A preview Playground page sends BLAST request messages onto the Service Bus request queue under the managed identity, lets an operator force a real drain pass, and observes optional completion-topic events via a demo external consumer. Adds request-queue / optional-topic env overrides, a Reader-accessible send route, and a standalone external-subscriber sample.
tags:
  - blast
  - ui
  - operate
---

# 2026-06-15 — Service Bus Playground (preview)

## Motivation

Operators wanted a browser-only way to exercise the Service Bus → BLAST path the
way an external service uses it: put a request message on the queue, watch the
real consumer pick it up and run BLAST, and confirm optional completion events
fan out to subscribers when a completion topic is configured. Previously the
only producer was an out-of-band script and the optional completion topic had no
subscriber to demonstrate delivery.

## User-facing change

* **New preview page `Service Bus Playground`** (`/blast/playground`), gated behind
  Settings → Preview → "Service Bus Playground" (default OFF). Three panes:
  1. **Request** — compose a BLAST request (query FASTA, db, program, taxid,
     options) and **Send** it onto the request queue, or **Validate** (dry run)
     without enqueueing.
  2. **Sample code** — read-only Python (send onto the queue / optionally
     consume an optional completion topic) and a dashboard-API `curl`, kept in
     sync with the form, for an external service to copy.
  3. **Consumer** — queue depth, a **Run consumer now** button that triggers one
     real `drain_and_resubmit` pass, recent sends, and completion events the
     optional demo consumer observed.
* **Reader can send (intentional policy relaxation).** `POST /settings/service-bus/send`
  is `require_caller`-only and is listed in `persona_reader_allowlist.py`, so a
  subscription **Reader** may enqueue a request. This is a deliberate exception
  to "Reader is read-only": the enqueue runs under the shared managed identity
  (no SAS token ever reaches the browser) and triggers BLAST execution. The same
  applies to the Playground `drain_now` accelerator and the read-only
  `observed_completions` view.
* **External subscriber model (optional topic fan-out).** The dashboard remains
  the sole consumer of the request **queue** (so every message is tracked
  end-to-end via `message_trace` + the bridge + jobstate rows). When a
  completion **topic** is configured, completion events are published there; an
  external service subscribes on its **own** subscription and receives an
  independent copy — it never competes with the dashboard for messages and can
  never double-run a job. Without that optional topic, callers use the status /
  result APIs by correlation id or job id.

## API / IaC diff summary

New routes under `/api/settings/service-bus`:

| Route | Auth | Behaviour |
|-------|------|-----------|
| `POST /send` | `require_caller` (Reader OK) | Validates against the OpenAPI submit contract, enqueues under the MI, records a producer-side forensic audit row. `dry_run: true` validates without enqueueing (works even when the integration is off). 409 when the integration is off; 429 when the request-queue backlog is at the send ceiling. |
| `POST /drain` | `require_caller` (Reader OK) | Runs one real `drain_and_resubmit` pass synchronously (bounded). 409 when off. |
| `GET /observed-completions` | `require_caller` (Reader OK) | Recent optional completion-topic events the demo consumer observed (empty when the consumer or topic is off). |

### Send backpressure (cost ceiling)

Because a Reader can now enqueue, and every request runs BLAST (AKS compute =
real cost), `POST /send` refuses with **429** when the request queue's pending
backlog (active + scheduled) is at or over `SERVICEBUS_SEND_MAX_QUEUE_DEPTH`
(default **2000**). This is a best-effort ceiling, not a security control: a
counts/admin outage fails open so a working integration is never blocked by an
admin-plane hiccup. `dry_run` and the 409 disabled-gate are evaluated before the
ceiling, so validation always works and a disabled integration is reported as
such rather than as "queue full".

> The producer-side audit row written by `/send` lives in the `jobhistory`
> Table keyed by the `external_correlation_id` (the only id known at send time).
> It is **forensic data** — the `/api/audit/log` view keys on the durable
> jobstate `job_id` (created later by the consumer under the OpenAPI job id), so
> the send row is queryable in the Table for forensics but is **not** surfaced
> in the dashboard Audit screen.

New env / config:

* `SERVICEBUS_REQUEST_QUEUE` / `SERVICEBUS_RESPONSE_TOPIC` — deployment-level
  overrides for the request queue / optional completion topic entity names. **Unset =
  existing behaviour preserved** (the saved Settings value or its default wins);
  a malformed value is ignored (logged), never silently repointed (§12a Rule 4).
* `SERVICEBUS_EXTERNAL_CONSUMER` (default OFF) — when set, the **worker** sidecar
  starts one daemon loop that subscribes to the optional completion topic on a
  dedicated subscription (`SERVICEBUS_COMPLETION_SUBSCRIPTION`, default
  `playground-observer`) and records observations into shared Redis for the
  Playground. Purely observational — it never executes BLAST.
* `SERVICEBUS_SEND_MAX_QUEUE_DEPTH` (default **2000**) — the Playground send
  ceiling described above.

New modules:

* `api/services/service_bus_external_consumer.py` — the completion-subscription
  receive loop, the gated worker launcher, and a standalone `__main__` an
  external party copies (`python -m api.services.service_bus_external_consumer`,
  authenticating with `DefaultAzureCredential`; needs `azure-servicebus` +
  `azure-identity` and `Azure Service Bus Data Receiver`).
* `api/services/service_bus_completions.py` — a capped, best-effort Redis ring of
  observed completions shared across the api/worker sidecars.

No Bicep change: the request queue, optional completion topic, and the new
`playground-observer` subscription are BYO Service Bus entities (the integration
is namespace-attached, not deployed by this repo).

## Persona impact

* **Owner / Contributor** — unchanged (full access; gains the Playground page).
* **Reader** — **gains** `send`, `drain_now`, `observed_completions` on the
  Service Bus route (intentional, recorded above). No other Reader change.
* **dev_bypass** — unchanged.

## Validation evidence

* Backend: `uv run pytest -q api/tests` — **3661 passed, 3 skipped**. New focused
  suites: `test_settings_service_bus.py` (send/drain/observed),
  `test_service_bus_env_override.py`, `test_service_bus_completions.py`,
  `test_service_bus_external_consumer.py`; `test_persona_matrix.py` green with the
  new Reader allowlist entries.
* Lint: `uv run ruff check api` — clean.
* Frontend: `cd web && npm run build` — clean (`ServiceBusPlayground` chunk
  emitted); `usePreferences.fixtureContract.test.ts` green with the new preview
  pref wired into the e2e fixture.
