---
title: Service Bus pipeline latency — drain/publish beat intervals to 10s
description: Lower the Service Bus drain/publish beat intervals from 30s to 10s so the queue→sharded-BLAST→completion-topic→download pipeline reports transitions promptly; control-plane overhead drops from ~43s to ~14s.
tags:
  - blast
  - operate
---

# Service Bus pipeline latency — drain/publish beat intervals to 10s

## Motivation

A full Service Bus BLAST round trip (enqueue request → sibling ElasticBLAST
sharded run → completion topic event with `download_url` → download) was measured
end-to-end against the live deployment. The first run took **199s**, dominated by
two 30s beat-poll quantization points in the control plane:

- `servicebus-drain-and-resubmit` (drains the request queue, submits to the
  sibling `/v1/jobs`) ran every 30s, so the request waited up to 30s before
  submission.
- `servicebus-publish-transitions` (polls sibling status, publishes
  queued/running/succeeded to the topic) ran every 30s, so each transition was
  detected up to 30s late.

## User-facing change

The two Service Bus beat intervals now default to **10s** (were 30s), tunable via
`CELERY_BEAT_SERVICEBUS_DRAIN_SECONDS` / `CELERY_BEAT_SERVICEBUS_PUBLISH_SECONDS`.
Subscribers on the completion topic see queued/running/succeeded transitions
roughly three times sooner. The optional Service Bus integration is idle-cheap
(one guard check per tick when no active bridge rows), so the higher tick rate
adds negligible cost when unused.

## API / IaC diff summary

- `api/celery_app.py` — `servicebus-drain-and-resubmit` and
  `servicebus-publish-transitions` default `schedule` lowered from `30` to `10`.
  No new env var, no Bicep change (the values were never set on the Container
  App; the code default governs). The live deployment was also tuned via an
  env-var-only Container App update (revision `ca-elb-dashboard--0000591`) so the
  change took effect without rebuilding the image.

## Validation evidence

Live E2E against `sub b052302c`, cluster `elb-cluster-01` (`rg-elb-cluster`),
`core_nt` warm on 5 shards. Request enqueued with `example/servicebus`-shaped
JSON; completion topic subscription `default` consumed; all result files
downloaded via their authenticated dashboard `download_url`.

| Phase | Before (30s) | After (10s) |
| --- | ---: | ---: |
| sent → queued | 42.1s | 16.1s |
| queued → running | 19.5s | 6.6s |
| running → succeeded | 120.5s | 113.9s |
| **results received + 5 files downloaded** | **~199s** | **136.6s** |

Both runs were `SUCCESS`: 5 `core_nt` shard result files (`batch_000-blastn-core_nt_shard_00..04.out.gz`,
164,842 B total) downloaded and verified as real gzipped BLAST output.

### Remaining latency is in the sibling execution plane (cross-repo)

The post-tuning control-plane overhead is now minimal (~14s drain+submit, <1s to
detect the sibling completion and publish). The dominant cost is the sibling
ElasticBLAST run itself. From the sibling job (`43c5ddd6a4bd`) and the AKS job
timeline (suffix `ad4a55f2`):

- sibling job created → BLAST pods created: **~93s** (`elastic-blast submit`
  config generation + k8s Job creation + query-batch import).
- BLAST shard execution: **~22s** (5 warm shards).
- finalizer tail after shards: **~7-20s** (serialized shard download + merge).

The ~93s `elastic-blast submit` / init overhead lives in
[`dotnetpower/elastic-blast-azure`](https://github.com/dotnetpower/elastic-blast-azure)
(the sibling execution plane), which is read-only from this repository. Reaching
a sub-120s (let alone sub-60s) round trip requires reducing that submit overhead
and/or parallelizing the finalizer in the sibling repo; the elb-dashboard control
plane has no further lever here.
