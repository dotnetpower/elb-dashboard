---
title: NCBI Direct live rollout hardening
description: Fix real NCBI taxdb manifest and embedded-taxonomy archive contracts before enabling and validating Direct database generations in the deployed environment.
tags: [blast, operate, architecture]
---

# NCBI Direct live rollout hardening

## Motivation

The first live-data preflight for the opt-in [NCBI Direct](https://ftp.ncbi.nlm.nih.gov/blast/db/v5/) path found two contracts that synthetic fixtures did not represent. The official `taxdb` metadata reports zero searchable letters and sequences because it is a lookup bundle, and ordinary database archives can embed `taxdb.btd`, `taxdb.bti`, and `taxonomy4blast.sqlite3`. The original validation rejected both forms before any generation could be promoted.

## User-facing change

- Direct updates can include the official standalone taxonomy bundle without rejecting its intentional zero search counts.
- Taxonomy files embedded in ordinary database archives are validated against an exact allowlist and skipped; the separately pinned `taxdb` archive uploads one authoritative copy, avoiding parallel overwrite and marker-duplication races.
- Searchable databases retain positive letter and sequence count validation.
- If a Container App revision replaces the worker and ephemeral Redis while an AKS Direct Job continues, the five-minute orphan reconciler now waits through the original-worker grace period and then revalidates and promotes a fully staged generation from durable Storage metadata. Missing or partial generations remain unpromoted and preserve the previous active database.

## API, worker, and infrastructure summary

- `api.services.ncbi_direct` applies non-negative count validation only to `taxdb`; archive sizes and every searchable database count remain strictly positive.
- The Direct Indexed Job extraction guard accepts only target database members or the three exact shared-taxonomy names. Embedded copies are omitted unless the archive itself is `taxdb`.
- Dispatch persists the release counts, archive count, transfer hash, JobState id, and generation identity needed after broker loss. Promotion checkpoints revalidate the owner under Blob ETags before verification, shard publication, and atomic activation. The Redis transfer lock can be reclaimed after revision loss only when absent or already held by the same persisted owner.
- The default-OFF gate now shares the control-plane environment source used by Bicep and exact-container quick deploys. A deployment-specific `azd env` override therefore survives both deployment paths without changing the repository default.
- API quick deploys now build the matching `elb-prepare-db` tag and backfill `PREPARE_DB_AKS_AZCOPY_IMAGE` on the exact API container. This closes drift in older deployments where Direct dispatch was enabled in code but the required AKS transfer image and runtime reference were absent.
- Live rollout exposed that Container Apps ARM GET no longer returns an ETag in either its body or response headers. Exact-container deploys now compare two canonical raw-ARM template fingerprints immediately before PATCH and retain `If-Match: *` as an existence precondition, rather than failing every deployment or silently removing the concurrency check.
- Container Apps normalizes decimal memory quantities such as `2.0Gi` to `2Gi`. The patch helper now compares normalized byte values, preventing a successful revision from being reported as a post-verification failure or repeatedly rolled.
- The first cleanup of a promoted Direct test generation exposed real ADLS Gen2 directory markers. Database deletion now reads the account's HNS capability and uses the existing leaf-guarded DFS recursive delete automatically; the general DFS feature gate remains unchanged for unrelated result/query paths.
- Live cancellation exposed the intentional metadata owner takeover as an unexpected Celery failure. The fenced worker now terminates as `cancelled` (or `superseded` after a rapid resubmit) without rewriting the cancellation commit. A cancelled first-time Direct generation is projected as non-ready even when no active DB exists, keeping its Delete action visible instead of leaking hidden partial staging.
- Cancellation also releases the Redis transfer lock before the polling worker wakes. Progress now checks the durable Blob owner before and after reclaiming/refreshing that ephemeral lock, and the generic failure branch rechecks ownership before writing. This makes cancel, rapid resubmit, and Redis revision loss converge without false failure telemetry.
- A live full-revision restart proved that a Python shutdown `finally` previously deleted the independent AKS Job after one of two archives, leaving recovery with an honest `markers incomplete (1/2)` partial generation. Interrupted workers now preserve the deterministic Job/ConfigMap; only completed, failed, or explicitly cancelled tasks clean them directly. Successful orphan recovery performs the deferred idempotent cleanup after atomic promotion.
- A first-time generation has no active DB record to anchor the catalogue row. Durable Direct `queued`/`downloading`/`verifying`/`sharding`/`promoting` metadata is now projected as non-ready `copying`, so a page reload or revision replacement keeps progress and Cancel visible until promotion instead of temporarily hiding the database.
- Final residue audit found that a 200 response to Background Job deletion did not mean its pods had stopped; an immediate database Delete could be followed by a terminating pod recreating generation files. User cancellation now uses Foreground propagation and waits up to 60 seconds for Job 404 before deleting the ConfigMap and committing terminal metadata. Timeout remains retryable and does not claim cancellation completed.
- No API payload, Storage network setting, RBAC assignment, SAS path, dependency, or Azure resource changed.
- A deployed API/worker image refresh is required because the defect exists in both dispatch manifest construction and the Kubernetes script emitted by the worker. Host-mode validation cannot exercise AKS managed identity, NCBI egress, private Storage upload, or generation promotion.

## Validation evidence

- Focused Direct, lock, promotion, orphan-recovery, deployment-env tests: `74 passed`.
- Full backend suite: `5513 passed, 4 skipped`; `uv run ruff check api` passed. Strict mypy passed for all eight changed production modules.
- Frontend parity (no frontend source changed): `109` test files / `985` tests, ESLint, and production build passed.
- Documentation frontmatter and strict MkDocs build passed; Bicep compiled successfully.
- Real read-only NCBI manifest smoke pinned `16S_ribosomal_RNA` release `2026-08-25` (72,164,324-byte archive) plus `taxdb` release `2026-08-26` (64,988,170-byte archive), including the intentional zero taxonomy search counts and one combined transfer SHA-256.
- Subscription-scope `az deployment sub what-if` could not run because the signed-in operator lacks `Microsoft.Resources/deployments/whatIf/action`; the strict RBAC-removal preflight halted rather than silently skipping. The rollout uses the existing RG-scoped ACR and exact-container patch path and does not change role assignments.
- Pending live evidence: deployed small-database completion, cancellation, revision-restart recovery, metadata promotion, private-Storage posture, and App Insights/log review.