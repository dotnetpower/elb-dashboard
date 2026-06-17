# Service Bus load verification — drain backlog + /v1/jobs rate limit

**Date:** 2026-06-17
**Area:** Service Bus integration (`api/tasks/servicebus`), OpenAPI rate limiter (`api/app/openapi_rate_limit.py`)

## Motivation

Confirm the queue-heavy and `/v1/jobs`-heavy paths stay correct and bounded
under load, and capture the throughput model + tuning knobs. No defect was
found — the gap was that the load behaviour was an unverified design assumption.
This change converts it into regression-guarded contracts plus one live burst.

## What was verified

### Queue backlog (drain path)

`api/tests/test_servicebus_load.py` drives the **real** `drain_requests`
settlement loop and the `drain_and_resubmit` task over many bounded ticks:

- **No loss, exactly one submit per correlation** — a 300-message backlog drains
  fully across `ceil(300 / SERVICEBUS_DRAIN_MAX_MESSAGES)` ticks; every tick
  respects the per-tick bound; 300 distinct correlations → 300 submits, zero
  duplicates.
- **At-least-once redelivery is deduped** — 120 messages covering 60 distinct
  correlations submit exactly 60 times (the drain handler short-circuits on an
  existing bridge row); all 120 are completed.
- **Permanent-rejection flood dead-letters without a retry storm** — 120 messages
  that the sibling 400s are each dead-lettered after a single submit attempt (no
  delivery-count burn / infinite abandon loop).
- **Transient failure is abandoned** — a 503 from the sibling abandons for retry
  rather than dead-lettering.

### `/v1/jobs` request volume (rate limiter)

- **No over-admit under concurrency** — 16 threads × 100 attempts against the
  sliding-window counter on one key admit **exactly** the budget (500), never
  more; the per-key lock makes check+record atomic.
- **Per-key isolation** — 20 distinct tokens each get their own budget; a flood
  on one key never starves another caller.

### Live burst (real deployment)

Enqueued a 25-message burst directly onto `elastic-blast-requests` with
`db="/loadtest"` (the sibling rejects a leading `/` with a submit-time 400, so
**no BLAST compute is scheduled** and the dashboard is not polluted — the send
route is bypassed so no placeholder rows are created). Result: the worker drained
all 25 over ~2 ticks, every message was correctly dead-lettered (4xx = permanent),
queue went `active 25 → 0` with no loss, and the worker stayed healthy. The DLQ
was purged afterwards (namespace returned to `active=0, dlq=0`).

## Throughput model (operator note)

- Drain runs every `CELERY_BEAT_SERVICEBUS_DRAIN_SECONDS` (default **30 s**),
  up to `SERVICEBUS_DRAIN_MAX_MESSAGES` (default **50**) per tick → ~100 msg/min
  ceiling, but the **effective** rate is bounded by the synchronous per-message
  `POST /v1/jobs` round-trip to the sibling, not the 50-message budget.
- This is by design: the sibling serializes BLAST execution
  (`ELB_OPENAPI_MAX_ACTIVE_SUBMISSIONS` + the cross-path Lease / `BLAST_MAX_RUN_CONCURRENCY`),
  so a faster drain would not increase end-to-end BLAST throughput — the queue is
  the durable buffer that absorbs bursts while the sibling executes at its safe
  concurrency.
- Tuning knobs if a deployment needs faster backlog ingestion (not execution):
  `CELERY_BEAT_SERVICEBUS_DRAIN_SECONDS` (lower = more frequent),
  `SERVICEBUS_DRAIN_MAX_MESSAGES` (higher = more per tick),
  `OPENAPI_RATE_LIMIT_REQUESTS_PER_WINDOW` / `OPENAPI_RATE_LIMIT_WINDOW_SECONDS`
  (default 2000 / 60 s) for the inbound `/api/v1/elastic-blast/*` surface.

## API / IaC diff summary

- New `api/tests/test_servicebus_load.py` (6 load/stress tests). No production
  code change — the design already satisfied the load contract.

## Validation

- `uv run pytest -q -n 0 api/tests/test_servicebus_load.py` — 6 passed.
- `uv run ruff check api/tests/test_servicebus_load.py` — clean.
- Live: 25-message burst drained to completion (active 25 → 0, all dead-lettered),
  DLQ purged to 0.
