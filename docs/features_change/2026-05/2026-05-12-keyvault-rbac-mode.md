# Key Vault provisioning works on RBAC-mode existing vaults

**Date**: 2026-05-12
**Scope**: `api/services/keyvault.py`

## Motivation

Provision Terminal failed at step 3 ("Creating Key Vault") with:

```
HttpResponseError: (InsufficientPermissions) Caller is not allowed to
change permission model. Details: oid=ceba4774-…;
action=Microsoft.Authorization/roleAssignments/write;
resource=/subscriptions/…/vaults/kv-vm-elb-termina-013f5a;
decision=NotAllowed
```

The Key Vault `kv-vm-elb-termina-013f5a` was created by a previous
attempt with `enableRbacAuthorization=true` (subscription Azure
Policy forces this on every new vault). On the next attempt the
control plane saw the existing vault and tried to flip
`enable_rbac_authorization` back to `false` via PATCH/PUT — which
requires `Microsoft.Authorization/roleAssignments/write`, a
permission the Function App MI does not hold under its standard
`Contributor` baseline. The PATCH returned 400 and the orchestrator
stopped.

## User-facing change

- Re-running Provision Terminal against an existing RBAC-mode vault
  no longer fails at step 3. The orchestrator advances through
  password rotation, VM update, and cloud-init verification.
- When the vault is in RBAC mode, the control plane now best-effort
  assigns the MI the `Key Vault Secrets Officer` role on the vault.
  When the MI lacks `roleAssignments/write` to self-grant, a single
  `az role assignment create …` command is logged for an admin to
  run manually (and the existing demo vault has already been
  unblocked that way).

## API / IaC diff summary

`api/services/keyvault.py`:

- `_ensure_vault_config` no longer attempts to PATCH the permission
  model. When the existing vault has `enable_rbac_authorization=true`
  it returns immediately and trusts the existing config. When the
  vault is in access-policy mode it only re-enables public network
  access (if disabled) and additively adds an MI/caller access
  policy via `update_access_policy("add", …)` — a dedicated
  idempotent operation that never re-PUTs the vault.
- New `_try_assign_secrets_officer` helper assigns the
  `Key Vault Secrets Officer` (built-in role
  `b86a8fe4-44ce-4948-aee5-eccb2c155cd7`) role to the MI (and the
  caller, if provided) on the vault scope. Suppresses
  `Conflict` / `RoleAssignmentExists` and logs a one-line
  `az role assignment create` recovery command on
  `AuthorizationFailed` / `InsufficientPermissions`.
- `ensure_keyvault` invokes `_try_assign_secrets_officer` for both
  paths (existing vault in RBAC mode, freshly created vault that
  ended up in RBAC mode due to subscription policy).

## Validation evidence

- Captured the original failure from App Insights innermost
  exception (`Caller is not allowed to change permission model`,
  vault `kv-vm-elb-termina-013f5a` in `rg-elb-demo-terminal`).
- Confirmed the vault state (`enableRbacAuthorization=true`,
  `accessPolicies=[]`) via `az keyvault show`.
- Granted the MI `Key Vault Secrets Officer` on the demo vault as
  the immediate unblock; the same operation is now attempted by
  `_try_assign_secrets_officer` automatically (best effort).
- `pytest -q api/tests/` → 13 passed.
- Function App redeployed via `WEBSITE_RUN_FROM_PACKAGE` user-
  delegation SAS (`funcapp-kvfix.zip`) and restarted; `/api/health`
  returns 200.
- Pending: user re-runs Provision Terminal in `rg-elb-demo-terminal`
  and confirms the orchestrator passes step 3.

## Operational note

The MI currently holds `Contributor` at subscription scope, which
does not include `roleAssignments/write`. Until the MI is granted
`Role Based Access Control Administrator` (or `User Access
Administrator`) at subscription scope, every fresh RBAC-mode vault
will require a one-shot manual `az role assignment create` from an
admin. The warning log line emitted by `_try_assign_secrets_officer`
spells out the exact command to run.
