# Upgrade actions authorized by Owner/Contributor RBAC (no allowlist needed)

## Motivation
Operators saw "You are signed in but not on the upgrade-admin allowlist —
start/rollback/escape-hatch actions are disabled. Ask an operator to add your
oid to `UPGRADE_ADMIN_OIDS`." even when they were a subscription **Owner**.
The upgrade-admin gate only recognised the `UpgradeAdmin` MSAL app role or the
`UPGRADE_ADMIN_OIDS` env allowlist — there was no path for a normal Azure RBAC
role. Maintaining a separate allowlist/group for what is effectively "can deploy
the control plane" is redundant with the Owner/Contributor a deployer already
holds.

## User-facing change
- **Owner / Contributor on the deployment (subscription or resource group) can
  now start / rollback / view the escape hatch** with no extra configuration.
  Entra **group**-inherited Owner/Contributor counts (the enumeration uses the
  `assignedTo()` OData filter).
- **Reader is still rejected** — read-only RBAC does not grant upgrade actions.
- `UPGRADE_ADMIN_OIDS` and the `UpgradeAdmin` app role still work but are now an
  optional **break-glass** override for a principal that holds neither an RBAC
  write role nor the app role.
- The SPA warning when blocked now reads "You need an Owner or Contributor role
  on the deployment …" instead of pointing at `UPGRADE_ADMIN_OIDS`.

## API / IaC diff summary
- `api/services/upgrade/auth.py`
  - New `caller_has_platform_write(caller)` — reuses
    `api.services.me_permissions.compute_caller_permissions` (the same RBAC
    enumeration that powers `/api/me/permissions`) and returns True only when
    `can_write and not degraded`.
  - `is_upgrade_admin` now grants via (1) platform write RBAC → (2) `UpgradeAdmin`
    app role → (3) `UPGRADE_ADMIN_OIDS` allowlist. The deployed dev-bypass
    refusal (Audit P1 #11) is unchanged.
  - The 403 detail message lists the RBAC option first.
- **SECURITY — fails closed.** `compute_caller_permissions` opens every
  capability when enumeration fails (a UX affordance for the SPA). The gate
  explicitly requires `not degraded`, so a caller whose enumeration failed is
  NOT auto-promoted — they fall back to the app role / allowlist.
- `api/tests/conftest.py` — `_env_baseline` now also drops ambient
  `AZURE_SUBSCRIPTION_ID` so the RBAC path stays network-free/deterministic in
  tests unless a test opts in.
- SPA: `UpgradePage.tsx` warning copy + module docstring; `upgrade.ts` route
  comments. No new endpoint, no payload change.
- **No infra change.** The shared UAMI already receives subscription-scope
  Reader (`assignSubscriptionReader=true`, default), which carries
  `Microsoft.Authorization/roleAssignments/read` — the permission the
  enumeration needs. `/api/me/permissions` already depends on it.

## Persona impact (charter §12a Rule 2)
This is an **additive** broadening (Contributor gains upgrade actions); no role
is narrowed, so the 2-phase removal rule does not apply. Persona Matrix updated:

| Persona | Before | After |
|---|---|---|
| `owner_caller` (UpgradeAdmin role) | admin ✓ | admin ✓ (unchanged) |
| `contributor_caller` (write RBAC) | **not admin** | **admin ✓ (via RBAC)** |
| `reader_caller` (read RBAC) | not admin | not admin (unchanged) |
| `dev_bypass_caller` | local: needs allowlist | unchanged |

New tests assert the RBAC promotion, the Reader rejection, and that a degraded
enumeration does NOT promote (fail-closed).

## Validation
- `uv run pytest -q api/tests` — 2899 passed, 3 skipped.
  - `test_persona_matrix.py`: `test_contributor_is_upgrade_admin_via_rbac`,
    `test_reader_is_not_upgrade_admin`, `test_degraded_rbac_enumeration_does_not_promote`,
    `test_contributor_without_rbac_scope_is_not_admin`.
  - `test_upgrade_routes.py`: `test_start_admin_via_platform_rbac_without_allowlist`
    (202), `test_start_reader_rbac_is_rejected` (403),
    `test_start_enforces_admin_role` still 403.
- `uv run ruff check` (touched) — clean; `cd web && npm run build` — OK;
  `npx eslint` (touched) — clean; docs frontmatter guard — OK.
