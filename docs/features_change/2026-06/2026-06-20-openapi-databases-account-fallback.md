---
title: openapi/databases route derives Storage account from blob endpoint
description: The cluster-independent BLAST database catalogue route now falls back to deriving the Storage account name from AZURE_BLOB_ENDPOINT / AZURE_TABLE_ENDPOINT when STORAGE_ACCOUNT_NAME is unset, so it no longer 400s on revisions that predate that env var.
tags:
  - blast
  - operate
---

# openapi/databases route derives Storage account from blob endpoint

## Motivation

On a deployed control plane the API explorer's
`GET /api/aks/openapi/databases` "Send Request" returned **HTTP 400**:

```json
{ "status": "error", "code": "missing_parameters",
  "message": "storage_account (or STORAGE_ACCOUNT_NAME env) is required.",
  "databases": [], "count": 0, "container": "blast-db" }
```

The route resolved the Storage account only from the `STORAGE_ACCOUNT_NAME`
env var. The current Container App template
([infra/modules/containerAppControl.bicep](../../../infra/modules/containerAppControl.bicep))
sets that var, but a revision deployed **before** it was added carries
`AZURE_BLOB_ENDPOINT` / `AZURE_TABLE_ENDPOINT` (e.g.
`https://<account>.blob.core.windows.net/`) yet no `STORAGE_ACCOUNT_NAME`, so
the one-click read 400'd even though the api sidecar can clearly reach its own
Storage.

## User-facing change

* `GET /api/aks/openapi/databases` and `GET /api/aks/openapi/databases/{db_name}`
  now resolve the Storage account in this precedence order:
  1. `storage_account` query param (explicit caller override),
  2. `STORAGE_ACCOUNT_NAME` env,
  3. **new fallback**: the leading host label of `AZURE_BLOB_ENDPOINT`, then
     `AZURE_TABLE_ENDPOINT` (validated against the 3–24 lowercase-alphanumeric
     Storage account name shape).
* The 400 `missing_parameters` response is now only returned when **none** of
  the three sources yields a valid account — i.e. a deployment that genuinely
  cannot name its Storage account.
* No change to auth, the 404/503 degraded paths, or the response shape.

## API/IaC diff summary

* [api/routes/aks/openapi_databases.py](../../../api/routes/aks/openapi_databases.py)
  — added `_account_from_endpoint()` and wired it into `_resolve_storage_scope()`.
* [api/tests/conftest.py](../../../api/tests/conftest.py) — the autouse
  `_env_baseline` now also `delenv`s `AZURE_BLOB_ENDPOINT` so the
  "no resolvable account → 400" tests stay deterministic.
* No IaC change. Existing deployments are fixed either by redeploying with the
  current template (which sets `STORAGE_ACCOUNT_NAME`) or by shipping this image
  (which makes the route robust without the env var).

## Validation evidence

* `uv run pytest -q api/tests/test_aks_openapi_databases.py` → 29 passed
  (new: `test_list_route_derives_account_from_blob_endpoint`,
  `test_list_route_explicit_storage_account_overrides_endpoint`,
  `test_account_from_endpoint_parsing`).
* `uv run pytest -q api/tests` → 4062 passed, 3 skipped (no regression from the
  global conftest change).
* `uv run ruff check` on the touched files → All checks passed.
