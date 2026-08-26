---
title: OpenAPI ElasticBLAST script reconciliation
description: Refresh stale AKS ElasticBLAST scripts by content so dashboard-warmed database shards are validated and reused without redundant downloads.
tags: [operate, blast]
---

# OpenAPI ElasticBLAST script reconciliation

## Motivation

The OpenAPI runtime previously treated an existing `elb-scripts` [Kubernetes ConfigMap](https://kubernetes.io/docs/concepts/configuration/configmap/) as current when a small set of script names existed. A live Service Bus execution showed that `elb-warmup-scripts` had staged all ten `core_nt` shards with DB-specific complete, source-version, manifest, and layout markers, while the name-compatible but older `elb-scripts` ConfigMap used a different cache contract. ElasticBLAST therefore downloaded a representative 39.85 GB shard again after warmup.

## User-facing change

Before each OpenAPI submit, the runtime now compares every installed ElasticBLAST shell script with the same-named ConfigMap entry. Any content drift reapplies the ConfigMap before ElasticBLAST creates initialization Jobs. Initialization is still fail-closed: the orchestration-level warmup skip remains disabled, and each node runs the hardened init script, which skips a transfer only after its DB-specific source, manifest, layout, payload, taxonomy, and `blastdbcmd` integrity checks pass.

## API and infrastructure summary

- The Service Bus request and completion schemas, queue/topic names, OpenAPI request payload, authentication headers, token lifecycle, and idempotency behavior are unchanged.
- The existing `elb-scripts` ConfigMap and Kubernetes permissions are reused; no role assignment, Bicep, Storage network rule, SAS path, or Azure resource is added.
- Concurrent reconciliation is idempotent because every caller applies the same image-bundled desired content. A lookup or apply failure does not fall back to a known-stale script.
- The runtime reads the ConfigMap back after apply and compares every desired script again. A concurrent or ambiguous update that does not converge fails the submit before any ElasticBLAST Job is created.
- Installed script data is capped at 900 KiB so an oversized image fails with a clear error before Kubernetes rejects the 1 MiB ConfigMap object.
- Logs identify drifted script file names only; script bodies and credentials are not logged.
- The runtime first moved from `elb-openapi:4.31` to `4.34` for the live cache proof, then to `4.35` after the post-apply verification hardening. ACR run `de5f` successfully pushed `4.35` digest `sha256:7bac4202e9e264c92a876e50ad4f2d1cde3fd3fcd9e6adfe286eb8fae581fbf4`. Existing older tags `4.32` and `4.33` were not overwritten, and `4.34` remains a rollback boundary.

## Validation

- Live read-only evidence: all ten warmup Jobs succeeded on the matching `ordinal=0..9` workload nodes, used `/workspace/blast`, and carried source version `2026-07-21-01-05-02`.
- Live read-only evidence: deployed `elb-warmup-scripts` and `elb-scripts` shard-init SHA-256 values differed, and the older `elb-scripts` entry lacked the DB-specific complete/source/layout marker declarations.
- Patched sibling commit `352a1f4ccf32dc8d76add5bcdb901530f0ad4c14` compiled successfully and produced the same context hash `8c70fe1d…d89087c` on two consecutive patch runs.
- ACR run `de5f` built `elb-openapi:4.35`; AKS Deployment generation 25 reached 1/1 Ready on the exact pushed digest with zero restarts and `/v1/ready` HTTP 200.
- The first 4.35 reconciliation found two remaining image-content drifts and converged. A second invocation emitted no drift log and preserved the exact ConfigMap object hash, proving the no-op postcondition.
- Live Service Bus request `sb-cachefix-20260826T021658Z-3822` drained once with DLQ zero. Its ten init Jobs completed in about 42 seconds; the retained shard log verified the 39,852,149,243-byte layout and emitted `DOWNLOAD_SKIP existing shard=00` with no download-start signal. All ten BLAST Jobs and the finalizer succeeded.
- `uv run pytest -q api/tests/test_patch_openapi_build_context.py` - `21 passed`.
- `uv run pytest -q api/tests/test_terminal_patch_elastic_blast.py api/tests/test_warmup_jobs.py` - `89 passed`.
- `uv run pytest -q api/tests` - `5312 passed, 4 skipped`.
- `uv run ruff check api` and `uv run ruff check scripts/dev/patch-openapi-build-context.py` - passed.
- `uv run python scripts/docs/check_frontmatter.py` and `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict` - passed.

## Ten-round hardening review

1. Preserved Service Bus serialization, initial/retry MessageId, target validation, and persona authorization contracts.
2. Replaced filename-only ConfigMap trust with exact desired-content comparison.
3. Re-verified warmup and init parity for DB-specific complete, source, manifest, layout, taxonomy, lock, and `blastdbcmd` gates.
4. Added a 900 KiB desired-data bound and post-apply read-back verification.
5. Validated idempotent same-image concurrency and fail-closed apply/read failures.
6. Projected OpenAPI and ElasticBLAST identities without contaminating queue placeholders.
7. Validated terminal transition uniqueness, durable outbox drain, and zero request/completion DLQ.
8. Ran full backend, frontend, lint, documentation, and local smoke regressions.
9. Removed process-global token precedence from cluster-aware proxy reads while preserving the legacy fallback for other consumers.
10. Re-ran consumer, fixture, diff, rollback, observability, and design-level self-critique; no Critical, High, or Medium finding remained.