---
title: Service Bus terminal identity projection
description: Surface both OpenAPI and ElasticBLAST runtime identities on synced Service Bus job rows without changing queue or security contracts.
tags: [blast, ui]
---

# Service Bus terminal identity projection

## Motivation

A deployed [Azure Service Bus](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-messaging-overview) validation completed all ten BLAST shards and returned ten parsed result files, but the dashboard detail response still rendered `openapi_job_id` and `elastic_blast_job_id` as null. The sibling list already reported both the short OpenAPI request ID and canonical `job-<32 hex>` ElasticBLAST runtime ID. The durable row backfilled the runtime column, but the local-row projection only inspected payload fields and did not copy the indexed runtime column into the response.

## User-facing change

Synced OpenAPI and Service Bus jobs now expose the short OpenAPI request ID in `openapi_job_id` and the canonical Kubernetes runtime identity in `elastic_blast_job_id`. Both list and detail projections use durable identity columns when payloads are omitted. A Service Bus row whose runtime ID has not arrived yet may use its sibling-generated 12-hex row key; a correlation-key send placeholder does not match that format and continues to expose both fields as null until the real row supersedes it. Unsafe payload IDs containing path separators or control characters are ignored.

## API and infrastructure summary

- The fields are additive response metadata; no existing response field is renamed or removed.
- Service Bus body serialization, MessageId behavior, request/completion schemas, queue/topic names, authentication, token handling, RBAC, Storage networking, and result paths are unchanged.
- The canonical results prefix remains under `infrastructure.results_prefix`; this validation confirmed `2026/08/26/7f7d3a3fc2aa/` and ten parsed result files.
- `completed_at` is not part of the current `BlastJobSummary` contract. Terminal timing remains represented by the terminal transition history and sibling runtime statistics.

## Validation

- Live request correlation `sb-cachefix-20260826T021658Z-3822` produced OpenAPI job `7f7d3a3fc2aa` and ElasticBLAST runtime `job-04983be56bff464b86eb5d266c7c4bcc`.
- Kubernetes reported 10/10 BLAST Jobs and the finalizer succeeded, with zero failed Jobs.
- `GET /api/blast/jobs/7f7d3a3fc2aa/results` returned HTTP 200 with ten files and ten parsed results.
- `uv run pytest -q api/tests/test_local_to_blast_job.py` - `64 passed`.
- `cd web && npm test -- --run` - `978 passed`; `npm run build` - passed.
- `scripts/dev/local-run.sh smoke` - `27/27 passed`.