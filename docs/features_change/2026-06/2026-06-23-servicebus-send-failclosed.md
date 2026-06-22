---
title: Service Bus send — fail-closed backpressure on sustained counts outage
description: Add a default-OFF SERVICEBUS_SEND_FAILCLOSED gate so the Playground send refuses (503 + Retry-After) after a sustained run of queue-depth read failures, instead of silently dropping backpressure and letting an unbounded producer pile up BLAST cost.
tags:
  - blast
---

# Service Bus send — fail-closed backpressure

## Motivation

The Playground send capacity check (`_assert_send_capacity`) refuses a send with
429 when the request-queue backlog is at/over `SERVICEBUS_SEND_MAX_QUEUE_DEPTH`.
But when the queue depth could not be read (no Manage claim, a momentary
admin-plane outage) it failed **open** — the send proceeded. A *sustained* counts
outage therefore silently removed the ceiling entirely, letting an unbounded
producer (now reachable by a subscription Reader via the Playground) pile up
BLAST compute cost unchecked.

## User-facing change

None by default. Gated behind `SERVICEBUS_SEND_FAILCLOSED` (default-OFF, charter
§12a Rule 4). When off, a counts failure still fails open exactly as before.

## API / IaC diff summary

- `api/routes/settings/service_bus.py`
  - A module-global consecutive-failure streak (`threading.Lock`-guarded) tracks
    counts-read failures. `_assert_send_capacity` increments it on a failure and
    resets it on a successful read.
  - `SERVICEBUS_SEND_FAILCLOSED` (default-OFF) + `SERVICEBUS_SEND_FAILCLOSED_STREAK`
    (default 3, floored at 1, fail-safe). When the gate is on AND the streak
    reaches the threshold, the send fails **closed** with HTTP 503
    (`capacity_unknown`, `consecutive_failures`, `Retry-After: 30`) and logs a
    WARNING. Below the threshold — or with the gate off — it still fails open, so
    a single/brief blip never blocks a normal send.

## Safety notes

- **Only enable where the credential has the Service Bus `Manage` claim** (so the
  depth can actually be read). If counts are unreadable by design, every check
  fails and a fail-closed gate would refuse EVERY send — keep it OFF there.
- The streak is per-api-process (not shared across replicas); each process
  independently arms fail-closed. That is acceptable for a cost ceiling — it is a
  backpressure heuristic, not a distributed quorum.
- `Retry-After: 30` steers a producer fleet's backoff so a flaky admin plane is
  not hammered (thundering-herd avoidance).

## Validation

- `uv run pytest -q -n0 api/tests/test_settings_service_bus.py -k "fail_closed or fail_open or resets_failclosed"`
  — 3 new tests: fail-open by default on repeated counts failures, fail-closed
  after the consecutive-failure threshold (asserts 503 + `capacity_unknown` +
  `Retry-After`), and streak-reset-on-success (a later single blip does not 503).
- `uv run pytest -q -n0 api/tests/test_settings_service_bus.py -k "send"` — 17
  passing (the unrelated `test_observed_completions` slow test is excluded; it
  hangs only under serial `-n0`, independent of this change).
- `uv run pytest -q api/tests/test_persona_matrix.py` — green (§12a Rule 2).
- `uv run ruff check api` — clean.
