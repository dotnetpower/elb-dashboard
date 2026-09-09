---
title: Split-job shard details
description: Show owner-scoped per-child progress and failure details for split-query BLAST jobs without adding a new state store.
tags: [blast, ui]
---

# Split-job shard details

## Motivation

Split-query parents exposed aggregate child counts, but researchers could not
see which shard was active, slow, failed, or associated with a particular query
group. Diagnosing one failed child required inspecting raw state outside the
Results page.

## User-facing change

The **Run details** tab now shows a Shard details section for split-query parent
jobs. It includes terminal progress, completed/active/failed counts, and one row
per child with status, phase, duration, effective search space, and sanitized
error text. Polling runs every five seconds only while at least one child is
active and is disabled entirely for non-split jobs.

The response is capped at 1,000 rows. When more children exist, the API sets
`truncated=true` and the page states that the summary covers the displayed
rows. A completed job opened directly on **Run details** remains on that tab;
only a job observed transitioning from running to completed auto-opens
Descriptions.

## API change

`GET /api/blast/jobs/{job_id}/shards` is an additive, owner-scoped, read-only
route. It projects existing child `jobstate` rows; no new table, artifact,
Celery task, Kubernetes call, or Azure Service Bus interaction is introduced.
Malformed child rows owned by another caller are excluded defensively.

## Validation

- `uv run pytest -q api/tests/test_blast_shard_details.py` - 5 passed.
- `npm --prefix web test -- --run src/pages/blastResults/ShardDetailsCard.test.ts` - 2 passed.
- Results tab transition and shard card focused suite - 8 passed.
- `npm --prefix web run build` - passed.
- Desktop and mobile Playwright checks rendered the truncation warning and
  sanitized failure row; the completed-job `?tab=run` URL remained stable.
- Protected Service Bus source hashes remained identical to the pre-expansion
  baseline.