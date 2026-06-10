# Dashboard entry RBAC gate enabled by default

## Motivation

Any authenticated member of the configured Entra tenant could load the full
dashboard — cluster names, job titles, database names, the subscription id —
even with **zero Azure RBAC** on the deployment's resource group / subscription
(Broken Access Control, OWASP A01). The `ENFORCE_DASHBOARD_RBAC` entry gate
already existed but shipped default-OFF, so the insecure behaviour was the
default. A blocked user also had no clear "how do I request access" path.

## User-facing change

- **Gate enabled by default.** `ENFORCE_DASHBOARD_RBAC` now defaults to `true`
  in `infra/modules/containerAppControl.bicep`. A signed-in tenant member must
  hold at least a read role (Reader / Contributor / Owner / AKS read / Storage
  Blob Data reader) on the platform scope before `GET /api/me` succeeds. Denied
  callers get the existing access-denied screen instead of a half-broken
  dashboard. Set the env var to `false` to restore the legacy "any tenant
  member loads the dashboard" behaviour.
- **Actionable access-denied screen.** The 403 `dashboard_access_denied` detail
  now names the concrete **subscription id**, **resource group**, and **role**
  (Reader) to request, and the `web/src/pages/AccessDenied.tsx` screen tells the
  user to forward that to a subscription owner / administrator, plus a note that
  the role can take a minute to propagate before **Retry**.

## API / IaC diff summary

- `api/services/dashboard_access.py`: the 403 detail gains a `subscription_id`
  field and the `message` now interpolates the subscription id + resource group
  + role so a blocked caller can forward an exact access request. `resource_group`
  is unchanged (additive change — existing consumers keep working).
- `infra/modules/containerAppControl.bicep`: `ENFORCE_DASHBOARD_RBAC` flipped
  `false` -> `true` with an updated comment documenting the managed-identity
  `roleAssignments/read` prerequisite and the fail-open degrade behaviour.
- `web/src/pages/AccessDenied.tsx`: renders the now-specific backend message and
  replaces the redundant static hint with a propagation/retry note.

## Prerequisite (operator action)

The gate is only effective when the shared api managed identity can read role
assignments. Grant the dashboard managed identity **Reader** at subscription
scope (built-in Reader includes `Microsoft.Authorization/roleAssignments/read`).
Without it, enumeration fails and the gate **degrades OPEN** (logs a warning,
lets everyone in) so it can never cause a tenant-wide lockout. Enabling the gate
requires a redeploy / Container App env update to take effect.

## Persona impact (charter 12a)

This is a security tightening of the **read** entry surface, gated behind the
existing `ENFORCE_DASHBOARD_RBAC` flag (no role narrowed — RBAC 2-phase rule
N/A). The gate degrades OPEN, so no persona is ever locked out by a transient
ARM hiccup or a missing MI Reader role.

| Persona | Before | After |
|---|---|---|
| `owner_caller` | loads dashboard | loads dashboard (has read role) |
| `contributor_caller` | loads dashboard | loads dashboard (has read role) |
| `reader_caller` | loads dashboard | loads dashboard (has Reader) |
| no-role tenant member | **loads full dashboard** | **403 + access-denied screen with request guidance** |
| `dev_bypass_caller` | local only | unchanged (dev bypass always allowed) |

No `Depends(require_caller)` added to an SSE stream. The Reader allowlist is
unchanged.

## Validation evidence

- `uv run pytest -q api/tests/test_dashboard_access.py api/tests/test_persona_matrix.py api/tests/test_me_route.py` -> 76 passed.
- `ENFORCE_DASHBOARD_RBAC=true uv run pytest -q api/tests/test_persona_matrix.py api/tests/test_dashboard_access.py` (gate forced ON, charter 12a Rule 4 evidence) -> 65 passed.
- `uv run ruff check api/services/dashboard_access.py api/tests/test_dashboard_access.py` -> clean.
- `cd web && npm run build` -> built successfully (AccessDenied typechecks).
</content>
