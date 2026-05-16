// Subscription-scope RBAC for the shared UAMI.
//
// Why this exists: every per-resource module (acr.bicep / storage.bicep /
// keyvault.bicep) only assigns roles at the resource scope. That is enough
// for data-plane operations (push image, read blob, fetch secret), but the
// SPA's discovery wizard also calls control-plane LIST operations that
// require permission at subscription / RG scope:
//
//   - SubscriptionClient.subscriptions.list()                  (Reader)
//   - ResourceManagementClient.resource_groups.list()          (Reader on sub)
//   - StorageManagementClient.storage_accounts.list_by_rg()    (Reader on sub)
//   - ContainerRegistryClient.registries.list_by_resource_grp()(Reader on sub)
//   - ComputeManagementClient.virtual_machines.list()          (Reader on sub)
//
// Without these, /api/arm/* endpoints succeed-but-empty and the SPA's
// wizard cannot discover existing infrastructure. The user-visible symptom
// is identical to the local-compose "no az login" failure that this
// module is the production counterpart of.
//
// This module deploys at subscription scope so it can grant a
// subscription-scope role assignment.

targetScope = 'subscription'

@description('Principal id (object id) of the shared UAMI to grant Reader to.')
param uamiPrincipalId string

// Built-in role definition ids (stable across tenants).
// https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles
var roles = {
  // Reader — list subscriptions, RGs, list-by-rg on every resource provider.
  // This is read-only; control-plane Contributor is intentionally NOT
  // granted at subscription scope.
  reader: 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
}

resource subReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  // guid() input must vary with both the principal and scope so re-runs
  // are idempotent across multiple environments in the same subscription.
  name: guid(subscription().id, uamiPrincipalId, roles.reader)
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.reader)
    description: 'elb-dashboard shared UAMI — sub-scope Reader for SPA discovery wizard.'
  }
}

output readerRoleAssignmentId string = subReader.id
