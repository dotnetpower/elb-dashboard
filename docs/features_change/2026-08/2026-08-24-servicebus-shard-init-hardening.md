---
title: Service Bus shard initialization hardening
description: Make node-local ElasticBLAST database staging transactional, remove unsafe warmed-cache reuse, preserve canonical runtime identity, and recover terminal logs and artifacts after partial failures.
tags: [blast, operate, architecture]
---

# Service Bus shard initialization hardening

## Motivation

A request consumed from [Azure Service Bus](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-messaging-overview)
failed with `RuntimeError: Shard init jobs failed: init-ssd-d8faab8f-3`.
The original init-container stderr was no longer available, so the exact command
that failed cannot be reconstructed. The surviving Job name proves that the
failure occurred while [Azure Kubernetes Service
(AKS)](https://learn.microsoft.com/azure/aks/what-is-aks) was staging a sharded
ElasticBLAST database onto node-local SSD.

The review found several independent paths that could produce or obscure the
same symptom:

- a historical warmup Job and `.download-complete` marker could be treated as
  cache proof even when taxonomy filter indexes (`.nos` / `.not`) were absent;
- concurrent submissions could validate, remove, and download files in one
  node-local directory without one lock covering the complete transaction;
- a failed database source-version lookup could leave a stale completion marker
  trusted;
- dashboard and OpenAPI submits could bypass submit-time cache validation using
  a warmed-cache hint; and
- external jobs did not durably preserve a canonical ElasticBLAST runtime ID,
  so the dashboard could miss the failed pod logs or finalize artifacts against
  the wrong identity generation.

## User-facing change

- Every sharded submit now runs the hardened init path. A completed warmup Job
  is no longer accepted as proof that every target node still has a valid local
  cache.
- Warmup and submit-time staging use one bounded `flock` in the shared node
  `hostPath`. The lock covers marker validation, partial-download cleanup,
  repair, taxonomy checks, `blastdbcmd -info`, source-generation verification,
  and the final atomic marker commit.
- A cache with missing nucleotide volumes, missing taxonomy filter indexes,
  partial AzCopy files, a mismatched source generation, or a failed
  `blastdbcmd` probe is invalidated and repaired before BLAST starts.
- External and Service Bus jobs preserve only canonical lowercase
  `job-<32 hex>` runtime IDs. Kubernetes log discovery trusts exact
  `elb-job-id` labels or `BLAST_ELB_JOB_ID` values. The short eight-hex fallback
  is restricted to exact `init-ssd-<suffix>-<ordinal>` incident names.
- Terminal pod-log persistence retries partial target/chunk failures instead of
  marking an incomplete capture ready. Artifact finalization waits for runtime
  identity, rebuilds when the identity generation changes, and uses a bounded
  reconciliation budget. Exhaustion and retry-publication failures remain
  visible as durable `pod_logs` artifact state plus a job-history event.
- Replay-safe transient `kubectl` calls have both a six-attempt ceiling and a
  per-call wall-clock deadline. `ELB_KUBECTL_TRANSIENT_DEADLINE_SECONDS`
  defaults to 180 seconds and is clamped to 1-600 seconds; each subprocess
  timeout is reduced to the remaining deadline.
- OpenAPI token updates retry one resource-version conflict with a fresh
  Deployment snapshot. Runtime-ID patching and template identity assertions
  fail the image build when the pinned sibling source drifts from the required
  contract.

## Code and API diff

- `api/services/warmup/scripts.py` and `terminal/patch_elastic_blast.py` now
  implement the same taxonomy, source-generation, integrity, lock, and atomic
  marker contract.
- `api/services/blast/config.py`, `api/tasks/blast/submit_task.py`, and
  `api/tasks/openapi/manifests.py` remove the unsafe warmed-cache skip path.
- `api/services/state/job_state.py` and `api/services/state/repository.py` add an
  optional canonical runtime-ID column and conditional ETag backfill that
  preserves a concurrent winner.
- `api/routes/blast/external_webhook.py` and
  `api/services/blast/external_jobs.py` persist identity independently from
  lifecycle updates, refuse conflicting identities, and keep terminal artifact
  capture recoverable.
- `api/services/job_logs/k8s.py` and `api/services/job_logs/persist.py` isolate
  log targets by exact identity and report partial capture as incomplete.
- `api/services/job_artifacts.py`, `api/tasks/blast/state.py`, and
  `api/tasks/blast_artifacts.py` track the artifact runtime generation and a
  maximum of five beat-driven recovery attempts. The terminal scan uses sorted,
  lightweight Table summaries so an arbitrary first page cannot starve newer
  jobs. Pod-log retries write `capture_pending`, `capture_exhausted`, or
  `retry_enqueue_failed` into an independent artifact row; terminal failures
  also append `pod_logs_capture_failed` to owner-scoped job history.
- `api/services/openapi/token.py` retries one Kubernetes JSON Patch conflict.
- `scripts/dev/patch-openapi-build-context.py` pins ElasticBLAST `744d79b`,
  removes unlabeled Kubernetes fallbacks, enforces canonical runtime IDs, and
  validates source/system/venv template policy independently from idempotency
  markers.
- `api/celery_app.py` and the artifact enqueue path avoid unused result-backend
  waits and bound broker connection/publication failure so terminal job updates
  cannot hang behind unavailable Redis.

All new Table fields are optional and default safely for existing rows. No
public HTTP response field was removed or renamed. No RBAC assignment, browser
SAS path, Storage public-network setting, or SSE authentication contract changed.

## Twenty-round design critique

1. **Incident boundary:** preserved `init-ssd-d8faab8f-3` as the correlation key
   without claiming unavailable stderr evidence.
2. **Taxonomy contract:** required `.nos` and `.not` in both download and reuse
   validation paths.
3. **Concurrency:** placed one bounded lock on the node `hostPath` and reused its
   inherited descriptor in the child staging script.
4. **Transactional marker:** removed partial files first, verified source and DB
   integrity, then committed source and completion markers in that order.
5. **Cache proof:** removed dashboard and OpenAPI warmed-cache skipping; Job
   history no longer substitutes for disk validation.
6. **Producer identity:** constrained OpenAPI discovery and terminal webhook
   payloads to canonical runtime IDs.
7. **Persistence race:** used conditional ETag merges with bounded retries and an
   authoritative final read.
8. **Cross-job isolation:** made exact label/env identity authoritative and
   limited suffix-only recovery to exact init-SSD failure names.
9. **Consumer identity:** migrated marker lookup, projection, and webhook
   consumers away from permissive `startswith("job-")` checks.
10. **Boundary security:** kept shared-token verification constant-time,
    malformed payload handling bounded, and lifecycle writes forward-only.
11. **Artifact generation:** tied ready state to the runtime identity used to
    build it and recovered after a failed invalidation write.
12. **Liveness:** bounded artifact and pod-log retries, sorted terminal scans,
    and retried one token resource-version race.
13. **Build drift:** verified identity labels in init, batch, and finalizer
    templates and fixed empty-block removal plus patcher re-run idempotency.
14. **Semantic patch validation:** added postcondition validators so retained
  marker comments cannot mask a missing safety operation.
15. **Celery liveness:** removed the unused finalizer result, bounded broker
  connection/publication behavior, and retained sentinel-backed recovery.
16. **Canonical selectors:** rejected non-canonical runtime IDs in all ordinary
  selectors and parsers while keeping the exact init-SSD incident fallback.
17. **Generation budget:** reset an exhausted empty-identity sentinel to attempt
  one when a newly discovered canonical runtime identity starts a new
  generation, without duplicating an already active unknown-identity task.
18. **Pod-log observability:** made capture exhaustion and delayed-retry enqueue
  failure durable and owner-visible without turning best-effort log loss into
  a failed BLAST artifact bundle.
19. **Retry deadline:** added a per-call wall-clock budget, reduced each
  subprocess timeout to the remaining budget, matched the pinned upstream
  `SafeExecError` constructor, and made the wrapper self-contained.
20. **Independent severity gate:** re-checked the complete diff across
  contracts, bounded liveness, concurrency, partial failure, security,
  observability, and compatibility. Only Low residual operational risks
  remained.

## Validation

Local-safe validation completed after the twentieth critique round:

- `uv run pytest -q api/tests` — 5,103 passed, 4 skipped. Three skips require an
  optional parity candidate directory; one requires the absent sibling source
  checkout. Six pre-existing duplicate OpenAPI operation-ID warnings remained.
- `uv run pytest api/tests -m 'slow or subprocess'` — 83 passed.
- Artifact generation and pod-log partial-failure suite — 39 passed after the
  final state/history changes.
- Terminal transient retry/deadline selection — 7 passed with a test stub that
  deliberately provides neither `logging` nor `UNKNOWN_ERROR` globals.
- `uv run ruff check api terminal/patch_elastic_blast.py
  scripts/dev/patch-openapi-build-context.py scripts/dev/smoke_api.py` — clean.
- `uv run python scripts/docs/check_frontmatter.py` — 61 navigated pages valid.
- `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict` — succeeded.
- `scripts/dev/local-run.sh smoke` — 27/27 API and SPA probes passed.
- `git diff --check` — clean.
- Earlier in the same implementation session, the real OpenAPI build context
  was patched twice consecutively and generated `app/main.py` compiled. Its
  output contained no permissive runtime-ID consumer or unlabeled job/pod
  fallback.

No live BLAST submit, image rollout, or Azure resource mutation was performed.
The selected scope remained `local-safe` because no explicit `live-submit` or
`full-azure` opt-in was provided. The terminal/OpenAPI images must be rebuilt
and rolled out together before the hardened image-installed scripts affect the
deployed execution plane.

## Rollback

Roll back the API, worker, terminal, and OpenAPI images together so the runtime
ID, artifact-generation, and template contracts remain aligned. Existing
`elastic_blast_job_id`, `runtime_identity`, and `reconcile_attempts` Table
properties are additive and are ignored by older code. Do not restore
`exp-skip-warmed-ssd-init`; rollback cache reuse still requires the older image's
own node-local validation path.
