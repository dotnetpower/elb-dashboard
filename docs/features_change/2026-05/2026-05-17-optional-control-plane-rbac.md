# Optional Control Plane RBAC Assignments

## Motivation

Redeploying an existing environment can fail with `RoleAssignmentExists` when
the shared managed identity already has equivalent roles that were created by a
previous template revision or by a manual `az role assignment create` command.

## User-Facing Change

Deployers can now set `ASSIGN_CONTROL_PLANE_ROLES=false` for environments where
the shared managed identity already has the required resource-group-scope
`Contributor` and `User Access Administrator` roles. New environments keep the
default `true` behavior.

## API / IaC Diff Summary

- Added `assignControlPlaneRoles` to `infra/main.bicep`.
- Guarded the `controlPlaneRoles` module behind that parameter.

## Validation Evidence

- `azd provision --preview`
- `azd provision --no-prompt`