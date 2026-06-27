---
title: Service Bus drain throughput tuning (Tier A1+A2+A3 + B1 sibling note)
description: Activate parallel drain, resident long-poll consumer, 5s beat fallback, and date-tiered results layout to absorb 500-2000 SB requests/day with E2E p95 < 10 min.
tags: [release, blast, operate]
---

# Service Bus throughput tuning — Tier A enabled + Tier B note

## Motivation

The B2B request channel is the project's main data plane: external services
publish BLAST requests (often outfmt 7 with multi-token fields) to the
`elastic-blast-requests` queue, the control plane orchestrates ElasticBLAST,
and completion events are published to `elastic-blast-completions` (with a
`download_url` pointing back at the dashboard auth gateway).

Live-measured cap on 2026-06-18 was ~5.4 msg/min: the drain ran serially every
10 s and the sibling `elb-openapi` accepted `ELB_OPENAPI_MAX_ACTIVE_SUBMISSIONS=2`
at 512Mi. With a target of 500–2000 requests/day **with bursts** and an E2E
SLO of p95 ≤ 10 min, that cap is too tight — a 500-request burst takes ~90 min
to drain.

## User-facing change

Toggles already shipped as default-OFF (charter §12a Rule 4) are flipped ON for
this deployment; no new feature surfaces.

| Sidecar | Key | Old | New | Why |
|---|---|---|---|---|
| api | `STORAGE_DATE_LAYOUT_ENABLED` | `false` | `true` | Results go under `results/YYYY/MM/DD/<job>/` so unbounded retention stays browsable + day-partition cheap to operate. |
| worker | `SERVICEBUS_RESIDENT_CONSUMER` | `false` | `true` | Long-poll consumer (~1 s) replaces the 10 s beat-only pace; the beat drain remains as a fallback reconciler. |
| worker | `SERVICEBUS_DRAIN_CONCURRENCY` | `1` (env default) | `4` | One drain tick runs up to four `submit_job_v1` calls in a bounded thread pool. The atomic `claim_bridge` gate makes >1 safe against duplicate submits. |
| worker | `STORAGE_DATE_LAYOUT_ENABLED` | `false` | `true` | Drain handler stamps the `results/YYYY/MM/DD/` prefix once before submit. |
| beat | `CELERY_BEAT_SERVICEBUS_DRAIN_SECONDS` | `10` (env default) | `5` | Fallback drain tick halved so a restart of the resident loop loses at most 5 s. |
| beat | `STORAGE_DATE_LAYOUT_ENABLED` | `false` | `true` | Reconcilers resolve the same dated prefix as api/worker. |

## API/IaC diff summary

- `infra/control-plane-env.json`: 2 new keys (`SERVICEBUS_DRAIN_CONCURRENCY`,
  `CELERY_BEAT_SERVICEBUS_DRAIN_SECONDS`) + 4 default flips
  (`STORAGE_DATE_LAYOUT_ENABLED` on api/worker/beat, `SERVICEBUS_RESIDENT_CONSUMER`
  on worker). Per the shared-keys guard test, every key shared by two sidecars
  carries the same value across sidecars.
- `infra/modules/containerAppControl.bicep`: two new env entries
  (`SERVICEBUS_DRAIN_CONCURRENCY` on worker, `CELERY_BEAT_SERVICEBUS_DRAIN_SECONDS`
  on beat) wired via `controlPlaneEnv.<sidecar>.<KEY>` so
  `test_control_plane_env::test_bicep_references_every_guard_key` stays green.
- No code changes — the modules consuming these env vars
  (`api/tasks/servicebus/tasks.py`, `api/services/blast/resident_consumer.py`,
  `api/celery_app.py`) already read them at module load.

## Tier B sibling note (NOT in this PR)

Throughput is also gated by the sibling `elb-openapi` deployment running on the
AKS cluster: `resources.limits.memory: 512Mi` + `ELB_OPENAPI_MAX_ACTIVE_SUBMISSIONS: 2`.
Live memory model (2026-06-18): `openapi_mem ≈ 268 + 70 × MAX_ACTIVE` MiB. Safe
combo for this deployment is `memory: 768Mi` + `MAX_ACTIVE: 4` (peak 535 MiB,
43% headroom). Applied as a live `kubectl set env / kubectl patch` against the
customer cluster; permanent home is the sibling repo's openapi manifest. Replicas
stay at 1 per maintainer direction.

## Validation evidence

- `uv run pytest -q api/tests/test_control_plane_env.py api/tests/test_servicebus_tasks.py api/tests/test_resident_consumer.py api/tests/test_service_bus_pref.py` → 91 passed.
- `uv run ruff check api/services/service_bus.py api/tasks/servicebus/ api/services/blast/resident_consumer.py` → clean.
- Live burst test (customer dev env, post-deploy): see follow-up note with the
  measured E2E p95 and DLQ count after the load run.
