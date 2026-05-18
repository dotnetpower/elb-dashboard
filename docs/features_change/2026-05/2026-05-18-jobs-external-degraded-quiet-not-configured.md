# 2026-05-18 — Stop surfacing `openapi_not_configured` as `external_degraded`

## Motivation

Operators reported that every `/api/blast/jobs` poll (every 30 s on the
dashboard) was rendered with a red **Degraded** badge in the request
inspector, even though the dashboard itself was working correctly. The
degraded reason was always `openapi_not_configured`.

Root cause: the canonical Jobs list endpoint in
[api/routes/stubs.py](../../../api/routes/stubs.py) probes the **optional**
external ElasticBLAST OpenAPI execution plane on every call. When that plane
isn't deployed (the common case for fresh installs that only use the local
Azure Table Storage state repo), `api/services/external_blast._base_url()`
raises `HTTPException(503, code="openapi_not_configured")`. The Jobs route
then surfaced that as `external_degraded: true,
external_degraded_reason: "openapi_not_configured"` in the response payload,
which the dashboard's [HttpInspectorPanel.tsx](../../../web/src/components/cards/SidecarsCard/HttpInspectorPanel.tsx)
correctly flags as a degraded request — producing the perpetual red badge
the user was seeing.

Configuration absence is not a runtime degradation. Treating it as one
trains operators to ignore the badge, which is the opposite of what the
inspector is for.

## User-facing change

* `/api/blast/jobs` no longer reports `external_degraded` when the external
  OpenAPI plane is intentionally not configured (`openapi_not_configured`
  or `openapi_not_enabled`). The Jobs payload is just `{"jobs": [...]}` in
  that case — the request inspector renders it as a normal `200 OK`, not
  as `200 Degraded`.
* Real upstream failures (timeouts, `5xx`, network errors,
  `openapi_upstream_error`, …) still surface as `external_degraded: true`
  so genuine outages remain visible.
* The local state repo's own degraded reasons (`not_configured`,
  `state_repo_unavailable`) are unchanged — those still produce the
  `DegradedNotice` on the Jobs empty state.

## API / IaC diff summary

API:

* [api/routes/stubs.py](../../../api/routes/stubs.py)
  * Added a module-level constant
    `_EXTERNAL_NOT_ENABLED_REASONS = {"openapi_not_configured",
    "openapi_not_enabled"}` next to `_exception_reason()` so future
    consumers can share the same allow-list.
  * The `blast_jobs_list` handler now skips assigning `external_degraded`
    when `_exception_reason(exc)` is in that set; it still logs the reason
    at INFO level so the cause is searchable in App Insights if anyone
    investigates.

No IaC, no frontend changes.

## Validation evidence

```bash
# Targeted file
uv run pytest -q api/tests/test_external_blast_api.py  # 23 passed in 2.14s
uv run ruff check api/routes/stubs.py api/tests/test_external_blast_api.py
                                                       # All checks passed!

# Full regression
uv run pytest -q api/tests                              # 640 passed in 43.40s
```

New test:

* `test_canonical_jobs_list_silent_when_external_not_configured` asserts
  the new contract — when `external_blast.list_jobs()` raises
  `openapi_not_configured`, the response carries `jobs == []` and **no**
  `external_degraded` / `external_degraded_reason` / `degraded` keys.

Rewritten test (existing name kept, scenario broadened):

* `test_canonical_jobs_list_reports_external_detail_code` now uses a
  `502 openapi_upstream_error` failure to assert that real upstream
  failures still surface as `external_degraded: true,
  external_degraded_reason: "openapi_upstream_error"`. This locks in the
  intentional separation between "not enabled" and "genuinely degraded".

Deploy:

* `azd up --no-prompt` on env `elb-ca`. Wall-time 7m29s.
* New active revision `ca-elb-control--0000061` (image tag
  `20260518131330`, created `2026-05-18T13:20:06Z`, replicas = 1).
* `GET https://ca-elb-control.gentlemeadow-01289e5b.koreacentral.azurecontainerapps.io/api/health`
  → `200 OK`.
* `GET …/api/blast/jobs` anon → `401 missing bearer token` (MSAL gating
  intact).
* ACR `publicNetworkAccess` restored to `Disabled` by the EXIT trap.
