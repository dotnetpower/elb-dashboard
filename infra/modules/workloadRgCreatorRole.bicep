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

targetScope = 'subscription'

@description('Principal id (object id) of the shared UAMI to grant the custom role to.')
param uamiPrincipalId string

@description('Stable token appended to the role definition name so multiple environments in the same subscription do not collide.')
param resourceToken string

var roleName = 'Elb Workload RG Creator'
var roleDescription = 'Allows the dashboard managed identity to create / read / delete subscription resource groups so AKS can auto-create its MC_* node RG without granting sub-scope Contributor.'

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
    description: 'elb-dashboard shared UAMI — sub-scope RG-write for AKS MC_* auto-create.'
  }
}

output workloadRgCreatorRoleId string = workloadRgCreatorRole.id
output workloadRgCreatorAssignmentId string = workloadRgCreatorAssignment.id
