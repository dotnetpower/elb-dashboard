---
title: Live Wall workspace ID deployment repair
description: Keep deployed Live Wall log queries bound to the Container Apps Environment workspace customer GUID.
tags: [operate, ui]
---

# Live Wall workspace ID deployment repair

## Motivation

The deployed API received an Azure Resource Manager workspace resource ID in `LOG_ANALYTICS_WORKSPACE_ID`, while `LogsQueryClient.query_workspace` requires the workspace customer GUID. Live Wall log snapshot refreshes therefore returned `PathNotFoundError` and could leave sidecar log tiles empty.

## User-facing change

Deployed Live Wall log tiles can query the six sidecars' recent Container App console logs again. Local file-tail behavior is unchanged.

## API and infrastructure summary

- `az-context.sh` now resolves the authoritative customer GUID from the Container Apps Environment instead of persisting a workspace ARM ID.
- `quick-deploy.sh` upserts the resolved GUID on every API patch so an existing malformed revision self-repairs without a full provision.
- `postprovision.sh` uses the same resolver and normalizes older azd environments that still carry a resource ID.
- No role assignment, Storage network rule, or public endpoint changed.

## Validation

- The pre-fix revision logged `live-wall LA snapshot refresh failed: PathNotFoundError`, and its API env contained the workspace ARM resource ID.
- `uv run pytest -q api/tests/test_az_context_acr_guard.py api/tests/test_control_plane_env.py -m ''`: 29 passed across Container Apps Environment discovery, legacy ARM-ID conversion, current-resource-group fallback, API-only quick-deploy wiring, and postprovision normalization.
- `uv run pytest -q api/tests`: 5,452 passed, 4 skipped. The full marker-inclusive run passed 5,549 tests before the final two additive resolver cases were added.
- `bash -n` passed for all three changed deploy scripts; Ruff, the documentation frontmatter guard, `mkdocs build --strict`, and Bicep compilation passed.
- The resolver returned the production Container Apps Environment customer GUID, and a direct Log Analytics query against it returned revision `ca-elb-dashboard--0000310` sidecar records.
- Production revision `ca-elb-dashboard--0000311` became Healthy with 100% traffic and all six sidecars Ready with zero restarts. The API env contains customer GUID `648cd0d4-a8b7-41da-a22c-050b5217b153` while retaining the `bce59ba` image digest.
- Live Wall opened on `v0.3.28 · bce59ba`, rendered six healthy sidecar tiles with real console lines, and produced no browser error or failed response. Post-fix telemetry returned zero `PathNotFoundError`, zero HTTP 5xx, and zero App Insights exceptions for the new revision window.