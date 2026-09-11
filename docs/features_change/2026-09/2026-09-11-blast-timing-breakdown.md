---
title: Separate BLAST queue and processing time
description: Replace mutable job-update duration with immutable queue, submit, processing, result-ready, and status-delivery timing.
tags:
  - blast
  - ui
  - architecture
---

# Separate BLAST queue and processing time

## Motivation

The Run details page labeled a mutable JobState `created_at -> updated_at` span as `Workflow` and
`Duration`. Metadata backfills after result creation could extend that value, while the upstream
[Service Bus](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-messaging-overview)
queue dwell was excluded. One inspected job therefore showed 4m 58s even though its measured
execution-plane processing was 2m 18s and its actual enqueue-to-result wait was about 4m 59s.

The same trace also recorded `completion_published` for a non-terminal `running` notification. Since
trace derivation intentionally keeps the first event for idempotent replay, the displayed end-to-end
metric stopped at the running notification instead of terminal result delivery.

## User-facing change

- Run details separates `Time to result`, `Queue wait`, `Submit`, `Processing`, and `Status delivery`.
- `Time to result` includes broker wait when recorded, but `Processing` never includes queue time.
- Legacy Service Bus rows with incomplete broker evidence show `Execution queue` instead of claiming
  a complete queue or total result time.
- The Jobs list uses immutable processing seconds for terminal rows; later metadata writes no longer
  increase their displayed duration.
- Message lifecycle distinguishes non-terminal `Running delivered` from terminal
  `Terminal status delivered` for both success and failure.
- Recent Jobs shows p50/p95 time-to-result, queue, and processing from complete loaded samples, plus
  a queue-pressure indicator. Incomplete queue samples are excluded from queue percentiles, and
  pressure is classified only after five paired complete queue/processing samples.
- New OpenAPI runs expose orchestration, Kubernetes setup, BLAST container, export, and finalizer
  spans. These are wall-clock spans and may overlap; they are diagnostic components, not values to
  sum blindly.

## API and runtime changes

- Job detail responses add an optional `timing` object. Existing scalar fields remain for backward
  compatibility.
- Service Bus drain rows persist enqueue, receive, and submit boundaries.
- Terminal transition publication persists result-ready and subscriber-delivery boundaries.
- The sibling OpenAPI terminal snapshot performs one bounded Kubernetes pod read to capture
  container spans; active polling keeps its existing Job-only query and does not gain pod-list cost.
- Running transition publication writes `transition_published`; only terminal success/failure writes
  `completion_published`. Legacy history rows whose completion payload says `running` are corrected
  during derivation.

## Hardening review

Fourteen review rounds were run until no reproducible Medium-or-higher finding remained:

1. Contract/state-machine: pinned terminal-only generated timing collection and normalized
  cancelled sibling states onto the existing failed terminal vocabulary.
2. Arithmetic invariants: exposed unattributed execution time and a `breakdown_complete` flag;
  contradictory negative accounting stays incomplete.
3. Terminal ordering: contradictory legacy histories select the first terminal by timestamp and
  out-of-order metrics remain null.
4. Idempotency/concurrency: made bridge markers monotonic and terminal-sticky with Table ETag CAS;
  stale running writers cannot revive a completed bridge.
5. Partial persistence: removed mutable `updated_at` terminal fallbacks and proved trace/backfill
  failures do not undo an already durable completion publication.
6. Runtime bounds: limited terminal pod reads to 15 seconds and 4,096 pods, kept active polls on the
  Job-only path, and logged collection degradation.
7. Backward compatibility: kept every new timing field optional and validated nested webhook phase
  timestamps while preserving the authenticated always-202 webhook acknowledgement contract.
8. Frontend semantics/accessibility: removed every mutable terminal-duration fallback, corrected
  failure delivery labels, added a copy-button label, and constrained the narrow details grid.
9. Sample integrity: active elapsed time can never become result-ready time, and queue-pressure
  classification requires five paired complete samples.
10. Security: confirmed timing payloads expose no token, SAS URL, or subscription identifier and
   remain behind the existing webhook bearer and global request-size boundaries.
11. Deployment/rollback: reserved a new immutable OpenAPI tag and retained `4.60` as the rollback
   image; the pin moves only after the new image build succeeds.
12. Mechanical review: searched every timing/trace consumer and removed mutable duration fallbacks
   from Jobs rows, Cluster Bento duration, and 24-hour runtime averages.
13. Independent adversarial review: added bounded CAS conflict backoff and an additive trace schema
   version; rejected non-reproducible findings against intentionally incomplete broker evidence.
14. Final independent review: reported `No Medium-or-higher findings`; only Low observability and
   future schema-migration audit gaps remained.

## Validation

- Focused timing/trace/bridge/webhook/patcher sweep: 298 passed.
- Full backend suite: 5,916 passed, 4 skipped (optional parity evidence was not configured).
- Full frontend suite: 1,036 passed across 118 files; ESLint and the production build passed.
- Ruff, the production mypy debt ratchet, OpenAPI contract/generated types, docs frontmatter, and
  strict MkDocs build passed.
- The patched sibling context accepted two consecutive applications with the same hash and the
  generated `main.py` compiled.
- Independent final review found no Medium-or-higher issue after 14 rounds.

Live image, rollout, and passive validation evidence will be appended after the immutable image is
built and the affected services converge. Synthetic billable jobs are not required for rollout; an
existing or naturally arriving request may be used as a passive timing canary.
