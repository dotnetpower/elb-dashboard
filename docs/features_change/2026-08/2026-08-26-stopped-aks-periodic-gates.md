---
title: Stopped AKS periodic task gates
description: Skip Kubernetes runtime GC and OpenAPI endpoint reconciliation when ARM proves the target AKS cluster is stopped or missing.
tags: [operate, blast]
---

# Stopped AKS periodic task gates

## Motivation

After an [AKS](https://learn.microsoft.com/azure/aks/what-is-aks) cluster auto-stopped, two periodic worker tasks still called its Kubernetes API endpoint. Runtime garbage collection and OpenAPI Service-IP reconciliation caught the resulting DNS failures, but Azure Monitor recorded one `ConnectionError` exception for each task every five-minute cycle.

## User-facing change

When ARM explicitly reports `cluster_stopped` or `cluster_not_found`, both periodic tasks now skip before opening a Kubernetes connection. Runtime GC reports an additive skipped count, and endpoint reconciliation leaves the stale durable IP to age out as before. When ARM health is unavailable or ambiguous, the gate degrades open and preserves the existing Kubernetes attempt.

## API and infrastructure summary

- No route, queue payload, Service Bus message, token, RBAC role, Storage rule, network setting, or Kubernetes object contract changed.
- Running clusters retain the same bounded GC and endpoint re-stamp behavior.
- Stopped/missing clusters produce informational skip results instead of exception telemetry.

## Validation

- `uv run pytest -q api/tests/test_k8s_runtime_gc.py api/tests/test_openapi_runtime_endpoint_reconcile.py` - `10 passed`.
- `CI=true uv run pytest -q api/tests` - `5313 passed, 4 skipped`.
- `uv run ruff check api` - passed.
- `uv run python scripts/docs/check_frontmatter.py` and `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict` - passed.