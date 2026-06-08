# 2026-06-09 — Resync infra/main.json with committed Bicep (phase-1 RBAC + STORAGE_ACCOUNT_NAME)

## Motivation

The compiled deployment template `infra/main.json` had drifted behind its Bicep
source. Two sets of source-only changes were never recompiled into the JSON:

1. **Phase-1 narrow RBAC roles** (commit `0aacd26`, "audit P2 #16-20"): the
   `controlPlaneRoles.bicep` / `workloadClusterRoles.bicep` modules added
   `managedIdentityContributorRoleId`, `networkContributorRoleId`, and
   `aksContributorRoleId` role assignments for the shared UAMI — but that commit
   did **not** regenerate `main.json`, so the phase-1 ADD never reached the
   deployable template (an `azd provision` would not have created those roles).
2. **`STORAGE_ACCOUNT_NAME` on the api/worker sidecars** (commit `b88113e`,
   prior session): the Bicep module + module JSON were updated, but `main.json`
   was intentionally left untouched at the time to avoid pulling in the stale
   RBAC drift mid-session.

This change recompiles `main.json` from the current Bicep so the deployable
template matches source again.

## Change

- `infra/main.json` regenerated from `infra/main.bicep` via `az bicep build`.
  Regeneration is idempotent (built twice → byte-identical).

## Safety audit (charter §12a — RBAC is ADD-only)

The regenerated diff was statically audited to confirm it only **adds** role
assignments and never removes one (the §12a Rule 1 ADD-then-REMOVE invariant):

- `Microsoft.Authorization/roleAssignments` resources: **+12 added, −0 removed**.
- `roleDefinitionId` references: **+6 added, −0 removed**.
- The only non-additive `−` lines are benign JSON reformatting: trailing-comma
  insertions on `userAccessAdministratorRoleId` / `platformResourceGroupName`
  (a new sibling var now follows them), two `description` text refreshes, and the
  single minified `containers` line being regenerated to include
  `STORAGE_ACCOUNT_NAME` on api/worker. No role assignment, principal, or scope
  was deleted.

This is exactly the phase-1 ADD the `0aacd26` commit intended; the broad
`Contributor` / `User Access Administrator` assignments are preserved (the
phase-2 REMOVE is a separate, later PR per Rule 1).

## Validation evidence

- `az bicep build infra/main.bicep` → exit 0; second build byte-identical (idempotent).
- `main.json` is a well-formed `subscriptionDeploymentTemplate` (14 root resources).
- `uv run pytest -q api/tests/test_check_rbac_removal.py` → 66 passed.
- `az deployment sub what-if` could not run end-to-end: it fails on a
  pre-existing `lockdownPrivateNetworking` parameter type mismatch in the
  parameters file (String vs Boolean), unrelated to this template regeneration.
  The static ADD-only audit above stands in for the automated removal check.

## Remaining / follow-up

- The `lockdownPrivateNetworking` parameter type mismatch in
  `infra/main.parameters.json` (or the azd env) blocks `az deployment sub
  what-if`; worth fixing separately so the §12a Rule 7 preflight can run clean.
- A full `azd provision` will now create the phase-1 narrow RBAC roles. The
  phase-2 REMOVE of the broad roles remains a separate future PR (Rule 1).
