# OpenAPI API token status self-heals legacy deployments

## Motivation

Commit `9d4e549` (2026-05-27) made `deploy_openapi_service` auto-mint the
`ELB_OPENAPI_API_TOKEN` env entry during deploy so a fresh cluster does not
need a manual "Generate" click before any `/v1/*` call works. That fix only
covered *new* deploys — clusters whose `elb-openapi` deployment was applied
before that commit still ship without the env entry. After redeploying the
dashboard sidecars, `GET /api/aks/openapi/token` correctly reports
`configured=false` for those legacy clusters, and the SPA's API Reference
panel keeps showing "No API token generated" until the operator manually
clicks Generate.

Reproduced on the user's `elb-cluster-01` (deployment created
`2026-05-26T17:30Z`, before the auto-mint fix): the deployment had every
other `ELB_*` env entry but the token entry was missing.

## User-facing change

`GET /api/aks/openapi/token` now self-heals legacy deployments. When the
elb-openapi deployment exists but its `ELB_OPENAPI_API_TOKEN` env entry is
missing, the status call mints a fresh token and patches the deployment
in-place (same JSON-Patch path that `ensure_openapi_api_token(regenerate=False)`
uses). The response then carries `configured=true`, the new token, and
`generated=true` + `updated_at=<ts>` so the SPA renders the panel as
configured immediately — no extra click required.

If the patch fails (RBAC, admission webhook, 422), the route returns
`configured=false` AND surfaces the failure in a new `self_heal_error:
{code, message}` response field. The SPA's `ApiTokenPanel` renders this as
a red banner with the actionable reason ("Auto-recovery failed:
openapi_token_patch_failed — Kubernetes returned HTTP 403 …") instead of
the silent "No API token generated" placeholder, so the operator can
immediately distinguish "never minted" from "self-heal blocked by RBAC".

Both self-heal success and failure now write an audit row
(`openapi_token_self_healed` / `openapi_token_self_heal_failed`) so the
existing `/api/audit/log` SPA surface picks them up and silent recoveries
leave a forensic trail.

Deploy-side: `api.tasks.openapi.manifests.build_manifests` now hard-fails
with `ValueError` when called without an `api_token`, eliminating the
silent "broken manifest" path that produced this recurring bug in the
first place. The append of the `ELB_OPENAPI_API_TOKEN` env entry is now
unconditional, making the contract explicit to future readers.

## API / IaC diff summary

- `api/services/openapi/token.py` — `get_openapi_api_token_status` now mints
  + patches the deployment when the env is empty, then returns the new
  token with `generated=true` and `updated_at`. New `self_heal_error`
  response field carries `{code, message}` when the self-heal patch
  fails so the SPA can render the actionable reason. New
  `_record_self_heal_audit` helper appends a JobState row to the
  existing audit table on both success and failure events. Self-heal
  success logs at `WARNING` (was `INFO`) so App Insights alerts can
  fire. Existing fields (`configured`, `token`, `masked_token`,
  `header_name`, `env_name`, `source`) are unchanged for the happy
  path. `generated` / `updated_at` / `self_heal_error` default to
  `false` / `null` / `null` so existing consumers (SPA panel, proxy
  fallback in `api/routes/aks/openapi.py`) keep working.
- `api/tasks/openapi/manifests.py` — `build_manifests` now raises
  `ValueError` when `api_token` is empty / whitespace and the env
  entry append is unconditional. Shipping a deployment without the
  token env was the root cause of the recurring "API token not
  visible" bug; the guard now fires loudly at the manifest boundary
  instead of silently emitting a broken deployment.
- `api/tests/test_openapi_token.py` — three new tests for status:
  `test_status_returns_existing_token_without_patch`,
  `test_status_self_heals_legacy_deployment_without_token_env`,
  `test_status_self_heal_patch_failure_falls_back_to_empty`. Tests
  now assert the new `self_heal_error` field and the audit emission.
- `api/tests/test_openapi_task.py` — added
  `test_build_manifests_rejects_empty_token` (covers `""`, whitespace,
  and the default-arg case). Existing tests updated to pass a token
  through the new mandatory contract.
- `web/src/api/aks.ts` — `OpenApiTokenStatus.self_heal_error` added as
  an optional `{ code, message } | null` field.
- `web/src/pages/apiReference/ApiTokenPanel.tsx` — new red banner that
  renders when `self_heal_error` is populated. Shows the failure code,
  the K8s upstream message, and the most common remediation (api
  sidecar managed identity needs Azure Kubernetes Service RBAC
  Cluster Admin on the cluster RG).

No IaC, no Bicep.

## Validation evidence

- Focused: `uv run pytest -q api/tests/test_openapi_token.py
  api/tests/test_openapi_task.py` → 11 passed.
- Wide backend: `uv run pytest -q api/tests` → 1513 passed.
- Wide frontend: `cd web && npm test -- --run` → 383 passed.
- Lint: `uv run ruff check api/services/openapi/token.py
  api/tasks/openapi/manifests.py api/tests/test_openapi_token.py
  api/tests/test_openapi_task.py` → All checks passed.
- Frontend build + type-check: `cd web && npm run build` → built; `npx
  tsc -p tsconfig.json --noEmit` → clean.
- Live verification: while diagnosing, the legacy
  `elb-cluster-01.elb-openapi` deployment in subscription
  `b052302c-…/rg-elb-cluster` was patched with a freshly minted token to
  unblock the user immediately. With the new self-heal path shipped, any
  future cluster in the same state will resolve itself on the first
  status call without manual intervention.
