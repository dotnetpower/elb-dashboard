---
title: Service Bus Message Flow dashboard card
description: A read-only dashboard strip + modal that visualizes the optional Service Bus integration as Producers (active-job submitters) → Broker (in-flight BLAST jobs sized by query length) → Consumers (AKS clusters), with click-to-inspect job JSON.
tags:
  - ui
  - blast
---

# Service Bus Message Flow dashboard card

## Motivation

The optional Service Bus integration had only a one-line inbound counter on the
Recent searches page. There was no at-a-glance view of *who* is driving BLAST
work, *what* is in flight, and *where* it runs. This adds a calm, honest flow
diagram to the dashboard.

## User-facing change

- New **Message Flow** strip on the dashboard (between the Cluster plane and
  Resource plane sections). It renders **only when the Service Bus integration
  is effective-enabled** — the default dashboard is unchanged.
- The strip shows producer color dots (one stable color per submitter alias),
  active-job count, and target-cluster count. When the integration is on but
  nothing is running it shows a single **"no active messages"** line instead of
  an empty diagram.
- An **expand** control opens a modal with three lanes:
  - **Producers** — active-job submitters by alias (shown as-is), colored per
    submitter, busiest first.
  - **Broker** — one box per in-flight `JobState` row (status `queued`/`running`),
    box width proportional to `log10(query sequence letters)`, left band tinted
    with the submitter's color. **Clicking a box shows the real job JSON** fetched
    from `/api/monitor/jobs/{job_id}`.
  - **Consumers** — grouped by AKS cluster name with running/queued counts.
  - Footer badge with live Service Bus queue / scheduled / DLQ counts (degrades
    to "counts unavailable (reason)" when the managed identity lacks the Manage
    claim).

### Honesty / degrade behavior

The broker lane reflects **active job state**, not raw queue messages — the
request queue drains in under a second so its depth is almost always zero
(see [Service Bus integration](../../architecture/service-bus-integration.md)).
When the integration is off the card hides itself; when counts are unavailable
the footer says so; nothing is animated to fake activity.

## API / IaC diff summary

- New `GET /api/monitor/message-flow` (read-only, `require_caller`, never 500 —
  degrades to `{"enabled": false}` via `_graceful`). The enabled snapshot is
  served through the shared monitor cache (`cached_snapshot`, TTL ~30s) so the
  per-poll Table scan + Service Bus management call run at most once per window
  regardless of open tab count. The cache key is **isolated per caller** (or a
  single `shared` bucket when `BLAST_JOBS_SHARED_VISIBILITY=true`) so one
  caller's private active-job list is never served to another from cache.
- New service `api/services/message_flow.py` builds the snapshot from the
  jobstate repo + Service Bus config (no new Azure SDK surface, no SAS tokens).
- Snapshot fields: `scope` (`own`/`shared`), `active_total`, `active_shown`,
  `broker_truncated`, `read_truncated` so the SPA labels a truncated view
  honestly instead of implying it sees every active job.
- The JSON-detail view reuses the existing `/api/monitor/jobs/{job_id}`
  endpoint; the modal **strips raw `owner_oid` / `tenant_id` GUIDs** before
  rendering (charter §12 — sanitise UI output). No infra/Bicep changes.

## Robustness review (self-critique, severity-ordered)

Fixed before completion:

- **Critical** — no caching meant a full Table scan + Service Bus management
  call on every 20 s poll per tab; added `cached_snapshot` with a per-caller
  cache key (load collapse + privacy isolation).
- **High** — payload-heavy reads and un-TTL'd Service Bus counts are now
  bounded by the same 30 s cache window.
- **High** — modal JSON no longer echoes raw `owner_oid` / `tenant_id`.
- **Medium** — added `scope` / `broker_truncated` / `read_truncated` so the
  producer-lane semantics and any >cap truncation are shown, not hidden; added
  an enabled-path route test; box `<button>` now carries an `aria-label`.

Verified non-defects: read-only GET (no idempotency/concurrency write races),
`require_caller` enforced, `_graceful` never 500s, SB-off hides the card (no
fake animation), box-click ownership check consistent across shared-visibility
and cluster-shared rows.

## Validation evidence

- `uv run pytest -q api/tests/test_message_flow.py` — 8 passed.
- `uv run pytest -q api/tests` (full suite) — 3334 passed, 3 skipped.
- `uv run ruff check` — clean on all new backend files.
- `cd web && npx vitest run src/components/cards/MessageFlow` — 9 passed.
- `cd web && npm run build` — succeeds; `npx eslint` clean on new files.
