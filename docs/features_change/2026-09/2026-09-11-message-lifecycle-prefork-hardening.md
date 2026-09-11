---
title: Harden terminal message lifecycle rendering
description: Make terminal trace branches exclusive and reset execution-admission locks in Celery prefork children.
tags:
  - blast
  - ui
  - architecture
---

# Harden terminal message lifecycle rendering

## Motivation

A live completed Service Bus job rendered both `Succeeded` with its timestamp and an unreached
`Failed` row marked `pending`. The backend trace had correctly selected `succeeded` as its one
terminal stage, but the frontend filled every intermediate canonical row before terminal delivery,
including the mutually exclusive failure branch.

A second review found that the execution-admission decision and marker caches replaced inherited
Azure clients after a [Celery](https://docs.celeryq.dev/) prefork, but did not replace their own
copied `threading.Lock` objects. A parent lock copied while held can remain permanently locked in a
child process.

## User-facing change

- A completed message lifecycle shows exactly one terminal branch: `Succeeded`, `Failed`, or
  `Dead-lettered`.
- The backend-provided `terminal_stage` is authoritative when contradictory legacy history exists.
  Older traces without that field select the earliest valid terminal timestamp, then one
  deterministic canonical branch if every terminal timestamp is malformed.
- The lifecycle card is an accessible named region and its ordered stage list has an explicit
  accessible name.
- Long stage labels wrap without creating horizontal overflow at the 320 px supported viewport.

## Runtime change

- Every Celery prefork child clears the process-local execution-admission decision and active-marker
  caches and replaces both copied locks without acquiring a parent-owned lock.
- The reset runs from the existing `worker_process_init` pool-reset sequence. It does not close
  parent transports or change admission, queue, API, auth, RBAC, Storage, or network contracts.

## Hardening review

Fourteen review rounds were applied until no reproducible Medium-or-higher finding remained:

1. Live timing arithmetic: verified $177 = 0 + 9 + 168$ seconds and
   $168 = 0 + 167 + 1$ seconds for the canary job.
2. Terminal state machine: found and removed the simultaneous success plus pending-failure display.
3. Contradictory history: made `terminal_stage` authoritative and added valid-timestamp plus
  deterministic malformed-timestamp fallbacks.
4. Failure/dead-letter paths: asserted each terminal alternative excludes the other two.
5. Accessibility: connected the section heading and labelled the ordered lifecycle list.
6. Responsive layout: measured zero document/card overflow at 320 px and constrained long labels.
7. Prefork concurrency: found copied admission locks and replaced them during child initialization.
8. Locked-parent simulation: reset succeeds without acquiring inherited locked objects.
9. Marker lifecycle: rechecked enqueue ordering, terminal/deferred cleanup, stale bounds, overflow,
   malformed rows, and exact JobState lookup; all remain fail closed.
10. Drain deadlines: rechecked caller deadlines, deny-cache behavior, readiness, submit reserve,
    claim release, and ordered broker settlement.
11. Timing provenance: confirmed `created_at` is immutable, `updated_at` is excluded, and missing or
    contradictory evidence remains incomplete.
12. SSE recovery: classified long-tab HTTP/2 resets as Low because heartbeat, bounded re-ticketing,
    and polling fallback preserved the rendered state with no API 5xx.
13. Security/backward compatibility: no bearer, SAS, subscription, RBAC, public-network, response
    schema, or generated-type boundary changed.
14. Consumer/fixture/rollback audit: verified all new symbols and trace helpers, tests, module
    headers, and API/frontend image rollback boundaries.

Rejected findings included stale deny-cache entries as a correctness issue (deny-only cache adds at
most two seconds and cannot open admission), parallel settlement as duplicate execution (durable
claim plus sibling idempotency protects redelivery), and marker overflow exceptions as unhandled
(the admission boundary converts them to a controlled fail-closed decision).

## Validation

- Targeted message-trace model: 21 passed.
- Targeted execution-admission and Celery reset: 54 passed.
- Broader timing/admission/warmup sweep before the final fixes: 283 passed.
- Final backend hardening sweep: 219 passed.
- Hermetic full backend suite: 5,937 passed, 4 skipped because optional XML parity evidence was
  not configured; slow/subprocess suite: 160 passed.
- Full frontend suite after the final malformed-timestamp fallback: 1,041 passed across 118 files;
  the focused 21-test suite, ESLint, and production build passed.
- Ruff, the production mypy debt ratchet, OpenAPI contract/generated types, docs frontmatter, and
  strict MkDocs build passed.
- The updated Playwright scenario is discovered. Local browser launch is unavailable because the
  host Chromium runtime lacks `libnspr4.so`; the same assertions will be run against the deployed
  page with the integrated browser.
- Live baseline before rollout: the canary timing breakdown was complete, 834 automatic drains over
  two hours had zero failures (p95 954 ms, max 5.590 s), and no soft-time-limit exception recurred.

Live image, browser, and post-rollout telemetry evidence will be appended after the frontend and
API/worker/beat revisions converge.
