---
title: Cluster-independent BLAST database catalogue endpoints
description: Promote the elb-openapi /v1/databases and /v1/databases/{db_name} reads to the always-on dashboard api sidecar so they answer while the AKS cluster is stopped.
tags:
  - blast
  - operate
---

# Cluster-independent BLAST database catalogue endpoints

## Motivation

The [OpenAPI](https://www.openapis.org/) plane (`elb-openapi`) runs **inside** the
AKS cluster, so its `GET /v1/databases` (catalogue) and `GET /v1/databases/{db_name}`
(metadata) reads die whenever the cluster is stopped. A caller who wants to know
*which databases exist* / *their molecule type + snapshot* before deciding whether
to wake the cluster could not, because those endpoints went down with the cluster —
the same gap that `POST /api/aks/openapi/ensure-running` already closed for the
wake-on-request flow.

Both endpoints are pure [Azure Storage](https://learn.microsoft.com/azure/storage/blobs/)
reads (the `blast-db` container blob list + per-database metadata JSON). The
always-on `api` sidecar already reaches that account over its private endpoint, so
the data is available even while the cluster is down.

## User-facing change

Two new read-only control-plane endpoints on the always-on dashboard `api` sidecar:

```
GET /api/aks/openapi/databases
GET /api/aks/openapi/databases/{db_name}
```

- `storage_account` (or the `STORAGE_ACCOUNT_NAME` env), `resource_group`, and
  `subscription_id` are accepted as optional query params; in the deployed
  dashboard they fall back to the api sidecar's env, so a "Try it" call against
  this deployment's own workload account is one-click.
- The list response is a drop-in for `elb-openapi` `GET /v1/databases`:
  `{ databases: [{ name }], count, container }`.
- The detail response mirrors the `elb-openapi` `DatabaseMetadata` shape
  (`name`, `container`, `title`, `dbtype`, `molecule_type` in `{dna, protein}`,
  `molecule_label`, `snapshot`, `last_updated`, sequence/letter counts, byte
  sizes, `cached_at`).
- Status mapping is failure-by-design: missing Storage account → 400, invalid
  `db_name` → 400, unknown database → 404, transient Storage outage /
  network-blocked → degraded 503 (never a 500).

Both endpoints are surfaced in the API Reference **Core** section (teal accent,
same-origin host banner) so they are documented and executable from the page even
while the cluster is stopped. The detail endpoint's `db_name` path parameter is
seeded with a representative default (`core_nt`) so a one-click "Send Request"
builds a valid URL instead of a broken `/databases/`.

## API / IaC diff summary

- `api/services/openapi/databases.py` (new) — projects the dashboard's shared
  Storage catalogue cache (`list_databases_cached`) into the `elb-openapi`
  list/detail response shapes. No new blob REST; no `azure.mgmt.*`.
- `api/routes/aks/openapi_databases.py` (new) — thin `require_caller`-gated GET
  routes; Storage-scope resolution with env fallback; 400/404/503 status shaping
  via `classify_storage_failure`.
- `api/routes/aks/__init__.py` — wire + re-export the new router.
- `web/src/pages/apiReference/coreEndpoints.ts` — add the two endpoints to the
  Core section with optional Storage-scope params and a seeded `db_name` default.
- `web/src/pages/apiReference/EndpointCard.tsx` — seed path-parameter defaults
  into the initial Try-it state so one-click GETs build a valid URL (query params
  are deliberately left unseeded so empty values fall back to the backend env).
- No IaC change: the `api` sidecar already has the Storage role + private
  endpoint and `STORAGE_ACCOUNT_NAME` / `AZURE_RESOURCE_GROUP` env it needs.

### Security note

The routes accept `storage_account` as a query param and pass it to the shared
managed-identity Storage read — identical to the already-shipped
`/api/blast/databases`, `/api/blast/databases/check-updates`, and
`/api/blast/databases/{db}/shard` routes. They are `require_caller`-gated
(authenticated tenant member) and default to the deployment's own
`STORAGE_ACCOUNT_NAME` env, so this introduces no new trust-boundary risk beyond
the established dashboard pattern. The stricter `extract_trusted_storage_account`
gate remains reserved for the lower-trust external-API `db` URL path, which these
routes do not touch.

## Validation evidence

- `uv run pytest -q api/tests/test_aks_openapi_databases.py` — 12 passed
  (service projection + route 200/400/404/503 contract).
- `uv run pytest -q api/tests` — 3708 passed, 3 skipped (no route-contract /
  inspector regression from the new routes).
- `uv run ruff check api/services/openapi/databases.py api/routes/aks/openapi_databases.py api/routes/aks/__init__.py api/tests/test_aks_openapi_databases.py`
  — all checks passed.
- `cd web && npx vitest run src/pages/apiReference` — 42 passed (incl. the new
  `coreEndpoints.test.ts` assertions for both endpoints).
- `cd web && npm run build` — built successfully.
