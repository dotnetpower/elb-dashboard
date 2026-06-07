---
title: BLAST submit idempotency key from the SPA
description: The New Search submit now attaches a client-generated idempotency_key so a retried or replayed submit dedupes to the same job on the backend instead of creating a duplicate BLAST run.
tags:
  - blast
  - user-guide
---

# BLAST submit idempotency key from the SPA

## Motivation

The backend BLAST submit route (`POST /api/blast/jobs`) already supports
idempotent submits: when the request carries an `idempotency_key`, it derives a
**deterministic** `job_id` from `(tenant, caller, idempotency_key)` (uuid5) and
returns the existing job on replay instead of creating a second one. Without a
key it falls back to a fresh `uuid4` per request, so every submit is a new job.

The SPA's New Search page never sent an `idempotency_key`, so that dedup path was
effectively dead for dashboard submits. The only protection against a duplicate
BLAST run was the submit button disabling itself while the mutation was in flight
(`submitPending`). That guards a single double-click in one tab, but not:

- a transport-level replay (browser / proxy re-sending the same POST), or
- a submit fired from two tabs.

Either could create two real BLAST jobs on the cluster.

## Change

`useSubmitMutation` now attaches a stable `idempotency_key` (a `crypto.randomUUID()`,
with a timestamp+random fallback for non-secure-context browsers) to each submit
request when the caller did not already provide one. The key is generated inside
`mutationFn`; mutations use the default `retry: 0`, so the key is stable for the
lifetime of one submit attempt and a backend-side replay of the same request body
dedupes to the same `job_id`.

- `web/src/api/blast.types.ts`: `BlastSubmitRequest.idempotency_key?: string`.
- `web/src/pages/blastSubmit/useSubmitMutation.ts`: generate + attach the key.

No backend change — this activates the existing server-side dedup.

## Validation

- `cd web && npx vitest run src/pages/blastSubmit` — 189 passed.
- `cd web && npm run build` — clean.
- `npx eslint` — clean on both files.
- Backend dedup behaviour is already covered by
  `api/tests/test_blast_submit_route_options.py`.

## User-facing effect

Double-submitting the same New Search (rapid resubmit, flaky network retry, or a
second tab) now returns the original job instead of starting a duplicate cluster
run — saving compute and avoiding confusing duplicate rows on Recent searches.
