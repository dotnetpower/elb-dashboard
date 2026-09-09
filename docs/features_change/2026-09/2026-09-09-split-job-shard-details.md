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

## API change

`GET /api/blast/jobs/{job_id}/shards` is an additive, owner-scoped, read-only
route. It projects existing child `jobstate` rows; no new table, artifact,
Celery task, Kubernetes call, or Azure Service Bus interaction is introduced.
Malformed child rows owned by another caller are excluded defensively.

## Validation

- `uv run pytest -q api/tests/test_blast_shard_details.py` - 3 passed.
- `npm --prefix web test -- --run src/pages/blastResults/ShardDetailsCard.test.ts` - 2 passed.
- `npm --prefix web run build` - passed.
- Protected Service Bus source hashes remained identical to the pre-expansion
  baseline.