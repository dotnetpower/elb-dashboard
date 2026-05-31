# OpenAPI execution RBAC gate (opt-in, default-OFF)

## Motivation

The OpenAPI menu (`/docs`) and `curl` calls go through
`/api/aks/openapi/proxy`, which auto-injects the admin `X-ELB-API-Token` and is
protected by `require_caller` (tenant membership only — no per-caller Azure
RBAC). Because OBO flows are forbidden by charter §12, any authenticated tenant
member — even a subscription Reader — could drive state-changing calls
(e.g. `POST /v1/jobs`) through the admin token. The forensic audit trail
(shipped earlier this session) records *who* drove each mutating call, but does
not *block* it. This change adds an opt-in gate that actually restricts
execution to callers with a write role on the target resource group.

## User-facing change

- **Default (env unset): no behaviour change.** Any tenant member can still
  drive OpenAPI execution exactly as before.
- **When `ENFORCE_OPENAPI_EXEC_RBAC=true`:** state-changing proxy verbs
  (`POST`/`PUT`/`PATCH`/`DELETE`) are forwarded only if the caller holds a
  write role (Contributor / Owner / AKS write) on the target resource group.
  - No write role → `403 openapi_exec_forbidden` (includes the caller's
    matched roles for the tooltip).
  - RBAC lookup indeterminate → `403 openapi_exec_rbac_indeterminate`
    (fail-closed). Requires the api MI to have
    `Microsoft.Authorization/roleAssignments/read` at the subscription scope.
  - Read-only `GET`/`HEAD`/`OPTIONS` and the dev-bypass identity are never
    gated.

## API / IaC diff summary

- **New service** `api/services/openapi/exec_gate.py`:
  `evaluate_openapi_exec_gate`, `ExecGateDecision`, `is_exec_rbac_enforced`.
  Reuses `compute_caller_permissions` but **fails closed** on `degraded`
  (the UX helper fails open).
- **Route** `api/routes/aks/openapi.py` (`aks_openapi_proxy`): runs the gate
  off the event loop right after credential/subscription resolution, denying
  before the upstream is resolved or the admin token injected. Audit-trail
  comment updated to note the gate now precedes it.
- **Infra** `infra/modules/containerAppControl.bicep`: new
  `ENFORCE_OPENAPI_EXEC_RBAC` env defaulting to `'false'` (charter §12a
  Rule 4).
- **Docs** `docs/operate/openapi-exec-rbac-gate.md` (+ mkdocs nav entry):
  operator runbook with the decision table, MI prerequisite, and planned
  default-flip target (2026-07, after a dogfood cycle with the gate forced
  ON and a green Persona Matrix).

## Validation evidence

- `uv run pytest -q api/tests/test_openapi_exec_gate.py` → 7 passed (positive
  ON path + legacy OFF path per charter §12a Rule 4: enforcement disabled,
  read-only never gated, dev-bypass, write-role allow, no-write deny,
  degraded fail-closed, truthy-token parsing).
- `uv run ruff check` clean on the new/changed files.
- `uv run python scripts/docs/check_frontmatter.py` → OK (51 navigated pages).
- Persona Matrix unaffected: the gate is a runtime-conditional default-OFF
  guard, not a route-gating change, so `require_caller` and the persona
  whitelist are untouched.
