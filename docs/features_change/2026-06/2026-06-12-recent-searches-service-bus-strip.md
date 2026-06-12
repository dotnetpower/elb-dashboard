# Recent searches: Service Bus inbound strip + submission-source filter

## Motivation

With the optional Service Bus integration live, operators need to see the
inbound request queue at a glance and filter Recent searches by how a job was
submitted (dashboard UI / external OpenAPI / Service Bus queue). The heavy
queue management (config, DLQ policy, purge) stays in Settings → Service Bus;
this change adds only lightweight, read-only affordances to the jobs page.

## User-facing change

- **Service Bus inbound strip**: a read-only status line at the top of Recent
  searches, shown only when the integration is effective-enabled. Displays the
  live request-queue and dead-letter counts (degrades to "counts unavailable"
  when the managed identity lacks the Manage claim) and a "Manage" button that
  opens the existing Settings → Service Bus section. Hidden entirely when the
  integration is off, so the default experience is unchanged.
- **Submission-source filter chips**: All sources / UI / API / Queue, alongside
  the existing status filters, persisted in the `?source=` query param. The
  job row "User" column now shows `queue` for Service-Bus-originated jobs.

## API / IaC diff summary

- No backend or API change. Frontend only.
- New `web/src/pages/BlastJobs/jobSource.ts` — `jobSubmissionSource` helper used
  by BOTH the JobRow User column and the source filter so they cannot disagree.
- New `web/src/pages/BlastJobs/ServiceBusInboundStrip.tsx` — polls the existing
  `GET /api/settings/service-bus` (require_caller) every 20s, `retry:false` so a
  fetch error just hides the strip.
- `useBlastJobsState` gains a `SourceKind` filter (`?source=`), `setSource`, and
  `sourceCounts`; `JobsFilterBar` renders the chips; `BlastJobs` wires them and
  renders the strip under the header.

## Validation evidence

- `cd web && npm run build` — type-checks and builds.
- `npm test -- --run src/pages/BlastJobs/jobSource.test.ts` — 5 passed.
- `eslint` clean on all changed files.
- Consumer search: no consumer of the `useBlastJobsState` shape exists outside
  `web/src/pages/BlastJobs/`.
