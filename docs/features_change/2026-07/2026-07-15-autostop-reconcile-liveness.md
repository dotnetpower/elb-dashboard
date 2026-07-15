---
title: Restore auto-stop reconcile liveness
description: Prevent long auto-warmup and unbounded Kubernetes history reads from starving AKS idle auto-stop evaluations.
tags:
  - operate
  - blast
  - architecture
---

# Restore auto-stop reconcile liveness

## Motivation

Auto-stop was enabled for `elb-cluster-01`, and the HTTP evaluator correctly
returned `keep / active_jobs:1`. The actual scheduled evaluator was not running:
the single reconcile worker had spent more than 30 minutes in one auto-warmup
reconcile while the Redis reconcile queue accumulated 1,947 messages.

The status route also took 64–88 seconds. Its live safety probe listed every
historical `app=blast` Job: 76,571 objects and approximately 993 MB per cache
miss, even though auto-stop only needs nonterminal work.

## User-facing change

- Auto-stop status and scheduled decisions use a filtered live-workload probe
  that covers BLAST, DB warmup, and prepare-db Kubernetes Jobs.
- A genuinely running multi-hour `prepare_db_aks` operation keeps the cluster
  alive for its full supported execution envelope instead of aging out after
  the two-hour warmup threshold.
- A wedged full-list auto-warmup reconcile can no longer occupy the periodic
  worker beyond its 110-second overlap-lock budget, and stale beat messages
  expire instead of replaying obsolete decisions.

## API and infrastructure diff summary

- The Kubernetes probe uses `status.successful=0` for Jobs and excludes
  terminal Pod phases. Live measurement reduced the BLAST Job response from
  76,571 objects / 993 MB / 36–88 seconds to 206 objects / 2.6 MB / 0.45
  seconds before terminal-condition filtering.
- The existing `probe_live_blast_activity()` symbol remains as a compatibility
  alias for the broadened cluster-workload probe.
- Auto-stop row staleness now uses six hours for long prepare-db/shard/oracle
  operations and two hours for warmup/ordinary rows.
- No route shape, RBAC assignment, network setting, or Azure resource changed.

## Validation evidence

- Focused evaluator, live-probe, auto-warmup, route, driver, and Celery schedule
  tests: `129 passed`; coverage includes filtered status shapes, type-aware
  stale thresholds, time limits, and expiry.
- Full backend suite: `4814 passed, 4 skipped`; Ruff lint passed.
- The optimized live workload probe completed against the existing AKS cluster
  in 1.23 seconds and returned zero active Kubernetes workloads, versus the
  previous 64–88-second status computation.
- The live preference remained enabled with a 60-minute idle window. The
  evaluator correctly kept the cluster Running while one active job was
  present; later the Service Bus card showed eight pending requests, which is
  also an additive keep-running signal.
- Kubernetes field-selector support was verified directly against
  `elb-cluster-01`. Unsupported/non-200 responses fail back to durable state;
  terminal Job detection requires success, completion time, or an explicit
  `Complete=True` / `Failed=True` condition so a failed Pod retry is never
  mistaken for a terminal Job.
- Design self-review found no unresolved Critical or High issue across
  beat/act parity, bounded liveness, idempotency, fan-out failure, security, or
  backward compatibility.
