---
title: Service Bus drain — atomic single-writer claim (no duplicate BLAST runs)
description: Add a default-OFF SERVICEBUS_ATOMIC_CLAIM gate that reserves each correlation id with an atomic insert before submitting, so a parallel or multi-worker drain can never submit the same request twice. Pairs with the parallel-drain fan-out.
tags:
  - blast
---

# Service Bus drain — atomic single-writer claim

## Motivation

The Service Bus drain handler bridged each request to the sibling `/v1/jobs`
plane after a non-atomic `get_bridge() → upsert_bridge()` read-modify-write. With
at-least-once delivery, a correlation id delivered to two drain workers at once
(or to a parallel submit batch, or to `resident` + `beat` simultaneously) could
pass the `get_bridge() is None` check in both, so **both would submit — the same
BLAST job runs twice** (compute cost + a shard-merge that mixes two runs). This
is the gating race that kept the parallel-drain fan-out
(`SERVICEBUS_DRAIN_CONCURRENCY`) at default 1.

## User-facing change

None by default. Gated behind `SERVICEBUS_ATOMIC_CLAIM` (default-OFF, charter
§12a Rule 4). When off, behaviour is byte-for-byte the legacy "any existing
bridge row dedups" path.

## API / IaC diff summary

- `api/services/service_bus_tracking.py`
  - `BridgeRecord` gains `claimed_at` (when a correlation id was reserved before
    its submit confirmed).
  - `claim_bridge(correlation_id, request_id)` — atomic reservation:
    `create_entity` insert-if-absent on the Table backend (the 409 is the
    single-writer lock); on the file backend the `_FILE_LOCK` serialises it.
    Returns `True` only to the winner. A **confirmed** row (one carrying an
    `openapi_job_id`) is never re-claimable. A **stale** unconfirmed reservation
    (older than `SERVICEBUS_CLAIM_STALE_SECONDS`, default 180s, floored at 30s,
    fail-safe on a bad value) is stolen via optimistic concurrency
    (`ETag` + `IfNotModified`) so a worker that crashed between claim and submit
    cannot wedge a correlation id forever. A steal logs at INFO.
  - `release_bridge(correlation_id)` — rolls back an unconfirmed reservation so a
    redelivery can re-claim + resubmit. Conditional (`ETag`) delete: never
    deletes a row that was confirmed in the gap between read and delete.
- `api/tasks/servicebus/tasks.py`
  - New `SERVICEBUS_ATOMIC_CLAIM` default-OFF gate. When on, `_drain_handler`
    dedups early only on a **confirmed** row, then `claim_bridge` before submit;
    a lost claim ABANDONs (defers to the winner's single submit); a submit
    failure `release_bridge`s so a redelivery can retry.

## Safety notes

- Pair `SERVICEBUS_ATOMIC_CLAIM=true` with `SERVICEBUS_DRAIN_CONCURRENCY>1`:
  the atomic claim is what makes the parallel fan-out safe. Enabling parallel
  submit without the claim is the duplicate-run risk this change removes.
- `_CLAIM_STALE_SECONDS` (default 180s) must exceed the sibling submit timeout so
  a slow-but-alive submit is never stolen. A steal is expected to be rare; an
  elevated steal-log rate signals worker crashes or a too-small threshold.
- The file backend's lock is thread-only — correct for production (Azure Table,
  ETag-based), but a local multi-process dev setup can still race; production is
  unaffected.

## Validation

- `uv run pytest -q api/tests/test_service_bus_tracking.py api/tests/test_servicebus_tasks.py`
  — 8 new claim/release unit tests (first-wins, confirmed-never-reclaimable,
  release-allows-reclaim, release-never-deletes-confirmed, stale-stealable,
  fresh-not-stolen, env fail-safe + floor) and 5 new gate tests
  (contended→abandon, submit-failure→release, gate-off legacy dedup, gate-on
  confirmed dedup, gate-on unconfirmed→claim).
- `uv run pytest -q api/tests/test_persona_matrix.py` — green (§12a Rule 2).
- `uv run ruff check api` — clean. 128 passing across the SB + persona suites.
