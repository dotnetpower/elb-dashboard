---
title: OpenAPI proxy token resynchronization
description: Recover one stale control-plane token response from the target AKS deployment without rotating credentials or changing proxy authorization.
tags: [operate, security]
---

# OpenAPI proxy token resynchronization

## Motivation

The API Reference proxy preferred the API sidecar's static token environment value over the per-cluster runtime cache. After the sibling token changed, a later [Azure Container Apps](https://learn.microsoft.com/azure/container-apps/overview) revision restart restored the older static value and every proxied request returned HTTP 401. The Service Bus worker already recovered this condition, but `/api/aks/openapi/proxy` did not.

## User-facing change

The proxy now prefers the explicit target cluster's token cache over the process-global static token, preventing two clusters from repeatedly replacing one another's first-request token. A per-cluster cache miss retains the existing static fallback. When the sibling returns HTTP 401, the proxy reads the existing token from the target [AKS](https://learn.microsoft.com/azure/aks/what-is-aks) deployment, synchronizes the server-side runtime cache, and retries the original request once. The recovery never generates or rotates a token. An unavailable live token preserves the original 401, and a second 401 is returned without another retry.

## API and infrastructure summary

- The proxy path, request body, response streaming, authentication, RBAC checks, path allowlist, and audit behavior are unchanged.
- The cluster-scoped resynchronization helper requires explicit subscription, resource group, and cluster coordinates; it does not depend on global endpoint metadata.
- No Bicep, role assignment, network rule, Service Bus message contract, or Storage setting changed.

## Validation

- Live reproduction: cluster-scoped `/v1/ready` returned HTTP 401 while the OpenAPI pod was healthy; synchronizing the deployment token changed the response to HTTP 200 without rotation.
- Live Service Bus request `sb-live-20260826T010346Z-07be` drained once, produced OpenAPI job `b64567f3e792`, completed 10/10 shards, published one terminal event, and produced a valid 31-file result archive.
- `uv run pytest -q api/tests/test_openapi_proxy_route.py api/tests/test_openapi_runtime_token_cache.py api/tests/test_openapi_token.py` - `69 passed`.
- `uv run pytest -q api/tests` - `5312 passed, 4 skipped`.
- `uv run ruff check api` - passed.
- `uv run python scripts/docs/check_frontmatter.py` and `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict` - passed.