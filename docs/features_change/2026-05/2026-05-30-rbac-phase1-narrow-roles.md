# 2026-05-30 — Phase-1 RBAC narrowing for the shared control-plane UAMI (audit P2 #16-20)

> **phase-1 of 2 (see PR-…)** — see §12a Rule 1.

## Motivation

The shared user-assigned managed identity `id-elb-dashboard-*` historically
held **Contributor + User Access Administrator** on both
`rg-elb-dashboard` (platform) and `rg-elb-cluster` (workload AKS). Audit
items P2 #16-#20 flagged this as overbroad: the API / worker sidecars
only need a small slice of those permissions (manage the AKS cluster,
create / delete the `id-elb-openapi` UAMI plus its federated credential,
read / write VNet + subnet + NSG for AKS networking). Plain `Contributor`
also grants `Microsoft.*/write` on every other resource type in the RG,
including the dashboard's own Storage / Key Vault / Container Apps
control surface — a much larger blast radius than the code path needs.

§12a Rule 1 mandates a two-PR sequence whenever RBAC is narrowed:

1. **phase-1 (this PR)**: ADD the narrow roles. Keep the existing
   broader `Contributor` so every code path continues to work.
2. **phase-2 (separate PR, after a 7-day soak)**: REMOVE
   `Contributor` once App Insights shows zero `AuthorizationFailed`
   events that would have been served by `Contributor`.

This PR is the additive phase-1. No role is removed; the eventual
phase-2 PR will reference this PR's number in its description.

## User-facing change

* None. This is a defence-in-depth RBAC adjustment. Operators see two
  more role assignments on the shared UAMI when they run
  `az role assignment list --assignee <uami-principal-id>`.
* No UI, no API contract, no Container App template change.

## API / IaC diff summary

* [infra/modules/controlPlaneRoles.bicep](../../infra/modules/controlPlaneRoles.bicep):
    * Adds three role assignments on the dashboard RG (`rg-elb-dashboard`):
      - `Managed Identity Contributor` (`e40ec5ca-96e0-45a2-b4ff-59039f2c2b59`)
      - `Network Contributor` (`4d97b98b-1d4f-4787-a291-c67834d212e7`)
      - `Azure Kubernetes Service Contributor Role` (`ed7f3fbd-7b88-4dd4-9017-9adb7ce333f8`)
    * The existing `rgContributor` resource is kept; its `description`
      is updated to mark it as `PHASE-1 LEGACY` so the phase-2 PR's
      deletion is unambiguous.
    * New `output …RoleAssignmentId string` entries for the three
      additions (parity with the existing two outputs).
    * File-header comment block explains the phase-1 / phase-2 split.
* [infra/modules/workloadClusterRoles.bicep](../../infra/modules/workloadClusterRoles.bicep):
    * Symmetric change on the workload AKS RG (`rg-elb-cluster`):
      same three roles, same `PHASE-1 LEGACY` annotation on the
      existing `workloadRgContributor`, same three new outputs.
* **No other files touched.** No Python, no frontend, no probe, no
  postprovision change. The probe in [scripts/dev/probe_capabilities.py](../../scripts/dev/probe_capabilities.py)
  will be extended in phase-2 to verify the narrow roles cover every
  required surface (currently the existing `Contributor` makes the
  question moot, so adding a probe now would be tautological).

## Validation evidence

* **Bicep compile**: `az bicep build --file infra/main.bicep --stdout`
  → no errors.
* **Compiled-ARM role-assignment audit**:
  ```
  Total role-assignment resources in compiled main.json: 20
    control-plane-roles/.../contributorRoleId                  (existing — keep)
    control-plane-roles/.../userAccessAdministratorRoleId      (existing — keep)
    control-plane-roles/.../managedIdentityContributorRoleId   (NEW)
    control-plane-roles/.../networkContributorRoleId           (NEW)
    control-plane-roles/.../aksContributorRoleId               (NEW)
    workload-cluster-roles/.../contributorRoleId               (existing — keep)
    workload-cluster-roles/.../userAccessAdministratorRoleId   (existing — keep)
    workload-cluster-roles/.../managedIdentityContributorRoleId (NEW)
    workload-cluster-roles/.../networkContributorRoleId         (NEW)
    workload-cluster-roles/.../aksContributorRoleId             (NEW)
    [+ 10 unchanged: monitoring / sub-roles / workload-rg-creator / acr×3 / storage×2 / kv×2]
  ```
  6 net-new role assignments, 3 per RG, exactly as designed.
* **`azd provision --preview`** (run against
  `b052302c-4c8d-49a4-aa2f-9d60a7301a80 / rg-elb-dashboard / koreacentral`):
  * Baseline saved at `.tmp/pr8/preview-baseline.txt`.
  * Phase-1 saved at `.tmp/pr8/preview-phase1.txt`.
  * Diff at the top-level resource list: **identical**. azd preview
    intentionally folds nested `Microsoft.Authorization/roleAssignments`
    into the parent deployment and does not enumerate them in its
    "Resources:" table, so the absence of a Modify line is the
    expected, correct signal: no Container App template, Storage,
    Key Vault, VNet, or ACR resource changes — only additive role
    assignments inside the two RG-scope nested deployments.
  * Both runs end with `SUCCESS: Generated provisioning preview`.
* **Persona Matrix** (§12a Rule 2):
  `uv run pytest -q api/tests/test_persona_matrix.py` → **41 passed in 3.22s**.
* **Wide sweep** (§13):
  `uv run pytest -q api/tests` → **2152 passed, 3 skipped in 34.42s**.
* **Lint**: no Python changed, ruff not re-run.
* **Capability Probe** (§12a Rule 3): the existing probe (Storage Blob,
  Storage Table, ACR, Container Apps, AKS, Key Vault) is unaffected by
  phase-1 because `Contributor` is still in place. Phase-2 will need a
  probe extension that exercises the new narrow surfaces (UAMI list,
  VNet list, AKS write).

## Phase-2 hand-off (for the next PR)

When the 7-day soak completes and App Insights confirms no
`AuthorizationFailed` event is attributable to the loss of
`Contributor`, the phase-2 PR must:

1. Delete `rgContributor` (controlPlaneRoles.bicep) and
   `workloadRgContributor` (workloadClusterRoles.bicep) — the two
   resources marked `PHASE-1 LEGACY`.
2. Decide whether to also remove `acrContributorForUami` in
   [infra/modules/acr.bicep](../../infra/modules/acr.bicep) (it is
   needed for ACR Build's `scheduleRun/action` and currently has no
   narrower replacement role — keeping it may be the right call).
3. Extend [scripts/dev/probe_capabilities.py](../../scripts/dev/probe_capabilities.py)
   with three new probes that exercise the narrow surfaces directly:
   - `ManagedServiceIdentityClient.user_assigned_identities.list_by_resource_group`
   - `NetworkManagementClient.virtual_networks.list`
   - `ContainerServiceClient.managed_clusters.begin_create_or_update` (dry-run)
4. Reference this PR (`#…`) in the phase-2 description's
   `phase-2 of 2 (see PR-N)` marker.
5. Attach an App Insights KQL snapshot of the soak window with zero
   role-related authorization failures, per §12a Rule 1.

## Hardening discipline (§12a):

- [x] In scope: rbac
- [x] RBAC change is labelled `phase-1 of 2 (see PR-…)` — this PR
      ADDS the narrow roles only, the broader `Contributor` is kept
      in place for the soak window. Phase-2 PR will remove it.
- [x] Persona Matrix tests pass for owner / contributor / reader / dev_bypass
      (41 passed; no auth surface touched in Python)
- [x] Reader allowlist unchanged — no Reader-required route touched
- [x] Capability Probe passes locally — no probe change in phase-1;
      probe extension scheduled for phase-2 (documented above)
- [x] New guard ships default-OFF — N/A (no `STRICT_*` gate; the
      narrow roles are passive additions, not validation guards per
      Rule 4 scoping)
- [x] No `Depends(require_caller)` added to an SSE event stream — no SSE changes
- [x] Change note (this file) summarises persona impact: every persona
      keeps every existing capability; the shared UAMI gains the ability
      to perform AKS / UAMI / Network operations through three narrow
      roles instead of relying solely on the broader `Contributor`.
      No persona loses any capability in phase-1.
