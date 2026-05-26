// Subscription-scope custom role: "Elb Workload RG Creator".
//
// Why this exists: AKS auto-creates the `MC_<rg>_<cluster>_<region>` node
// resource group at subscription scope when a managed cluster is created.
// The caller of `Microsoft.ContainerService/managedClusters/write` therefore
// needs `Microsoft.Resources/subscriptions/resourceGroups/write` at sub
// scope or the AKS resource provider returns AuthorizationFailed. The
// dashboard's shared UAMI intentionally only carries sub-scope Reader
// (see infra/modules/subscriptionRoles.bicep) so it cannot create
// arbitrary resources anywhere in the subscription.
//
// This module defines a narrow custom role that grants only the actions
// needed to bootstrap a new workload resource group (mainly the AKS node
// RG) and assigns it to the dashboard UAMI at subscription scope. Once
// the RG exists Azure RBAC still requires a Contributor/Owner assignment
// at that specific RG to do anything inside it — the custom role does not
// grant resource-create access on its own.
//
// Designed as the least-privilege alternative to granting sub-scope
// Contributor (which would let the UAMI provision any resource in any
// RG it created).
//
// 2026-05-27 (Part C of the OpenAPI-deploy RBAC-gap fix): the role now
// also includes `Microsoft.Authorization/roleAssignments/{read,write}`
// so `api.tasks.azure.provision.provision_aks` can self-grant the
// Contributor + User Access Administrator pair to the dashboard UAMI on
// the AKS cluster RG immediately after the cluster create succeeds.
// Without this, an operator who creates AKS via the SPA wizard (and
// forgets to set `AKS_CLUSTER_RESOURCE_GROUP` and re-run `azd provision`
// so `workloadClusterRoles.bicep` runs) later clicks "Deploy elb-openapi"
// and the OpenAPI deploy fails with
// "workload identity setup failed; OpenAPI pod would have no AZURE_CLIENT_ID."
//
// The `roleAssignments/write` permission is gated by an ABAC condition
// on the assignment of this custom role (see `workloadRgCreatorAssignment`
// below). The condition restricts the UAMI to assigning ONLY this curated
// 5-role whitelist with `principalType=ServicePrincipal`:
//   - Contributor                            (b24988ac-…)
//   - User Access Administrator              (18d7d88d-…)
//   - Storage Blob Data Contributor          (ba92f5b4-…)
//   - AcrPull                                (7f951dda-…)
//   - Azure Kubernetes Service Cluster User  (4abbcc35-…)
//
// All five role assignments are already created by the SPA's existing
// AKS / OpenAPI deploy flow (via `api.tasks.azure.rbac.ensure_aks_runtime_rbac`
// and `api.tasks.openapi.rbac.setup_workload_identity`). The ABAC
// whitelist closes the obvious "what if the UAMI is compromised — could
// it grant itself Owner sub-wide" question: it cannot, because the
// condition blocks any other RoleDefinitionId.

targetScope = 'subscription'

@description('Principal id (object id) of the shared UAMI to grant the custom role to.')
param uamiPrincipalId string

@description('Stable token appended to the role definition name so multiple environments in the same subscription do not collide.')
param resourceToken string

var roleName = 'Elb Workload RG Creator'
var roleDescription = 'Allows the dashboard managed identity to create / read / delete subscription resource groups so AKS can auto-create its MC_* node RG, and to assign a fixed whitelist of runtime roles (Contributor, User Access Administrator, Storage Blob Data Contributor, AcrPull, AKS Cluster User) so it can self-heal RBAC on the workload cluster RG.'

// Built-in role definition GUIDs the dashboard UAMI is allowed to
// assign via the ABAC condition below. Keep these in sync with the
// targets in `api.tasks.azure.rbac.ensure_dashboard_mi_cluster_rg_roles`
// and `api.tasks.openapi.rbac.setup_workload_identity`. New entries
// require updating both this file and the condition string.
var allowedRoleDefinitionIds = [
  'b24988ac-6180-42a0-ab88-20f7382dd24c' // Contributor
  '18d7d88d-d35e-4fb5-a5c3-7773c20a72d9' // User Access Administrator
  'ba92f5b4-2d11-453d-a403-e96b0029c9fe' // Storage Blob Data Contributor
  '7f951dda-4ed3-4680-a7ca-43fe172d538d' // AcrPull
  '4abbcc35-e782-43d8-92c5-2d3f1bd2253f' // Azure Kubernetes Service Cluster User Role
]

// Render the ABAC condition expression. Whitespace inside the string
// is significant for human readability only — the Authorization
// service parses it as a single expression.
var allowedRolesCsv = join(allowedRoleDefinitionIds, ', ')
var roleAssignmentCondition = '((!(ActionMatches{\'Microsoft.Authorization/roleAssignments/write\'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${allowedRolesCsv}} AND @Request[Microsoft.Authorization/roleAssignments:PrincipalType] StringEqualsIgnoreCase \'ServicePrincipal\'))'

// `guid()` on subscription().id keeps the role definition stable per
// subscription, so multiple environments deploying into the same
// subscription reuse the same definition (idempotent re-creation).
resource workloadRgCreatorRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, roleName)
  properties: {
    roleName: roleName
    description: roleDescription
    type: 'CustomRole'
    assignableScopes: [
      subscription().id
    ]
    permissions: [
      {
        actions: [
          // Allow the UAMI to create / read / delete subscription RGs.
          // This is the minimum AKS needs to auto-provision the MC_*
          // node RG; the UAMI still cannot put resources inside any RG
          // without a separate per-RG Contributor (or equivalent)
          // assignment.
          'Microsoft.Resources/subscriptions/resourceGroups/read'
          'Microsoft.Resources/subscriptions/resourceGroups/write'
          'Microsoft.Resources/subscriptions/resourceGroups/delete'
          // ARM deployments at sub scope (so the same UAMI can run
          // future `az deployment sub create` flows for cluster RG
          // bootstrap if we move that off the operator).
          'Microsoft.Resources/deployments/read'
          'Microsoft.Resources/deployments/write'
          'Microsoft.Resources/deployments/operations/read'
          'Microsoft.Resources/deployments/operationStatuses/read'
          // Self-heal RBAC on workload RGs (Part C). Gated by the
          // ABAC condition on the role assignment so the UAMI can
          // only assign the 5-role whitelist above; it cannot grant
          // itself Owner, sub-scope Contributor, or anything outside
          // the whitelist.
          'Microsoft.Authorization/roleAssignments/read'
          'Microsoft.Authorization/roleAssignments/write'
        ]
        notActions: []
        dataActions: []
        notDataActions: []
      }
    ]
  }
}

resource workloadRgCreatorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  // The role assignment name must vary with the principal so re-runs are
  // idempotent across multiple environments in the same subscription.
  name: guid(subscription().id, uamiPrincipalId, workloadRgCreatorRole.id, resourceToken)
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: workloadRgCreatorRole.id
    description: 'elb-dashboard shared UAMI — sub-scope RG-write for AKS MC_* auto-create + ABAC-restricted self-heal of runtime RBAC.'
    // ABAC condition (Constrained Role Assignment Delegation). The
    // condition only fires on `roleAssignments/write` requests; reads
    // are allowed unconditionally. Writes must target one of the
    // 5 whitelisted role definitions AND a ServicePrincipal principal
    // (the dashboard / kubelet / OpenAPI MIs) — never a User or Group.
    // This blocks the obvious escalation path of "MI assigns itself
    // Owner sub-wide if compromised".
    condition: roleAssignmentCondition
    conditionVersion: '2.0'
  }
}

output workloadRgCreatorRoleId string = workloadRgCreatorRole.id
output workloadRgCreatorAssignmentId string = workloadRgCreatorAssignment.id
