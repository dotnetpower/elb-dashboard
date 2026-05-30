// Resource-group-scope RBAC for the shared control-plane UAMI.
//
// The API/worker sidecars use this identity for user-triggered runtime
// operations: creating AKS clusters in the platform RG, scheduling ACR Tasks,
// and assigning AcrPull / Storage Blob Data Contributor to AKS kubelet
// identities. Keep these permissions at the deployment resource-group scope;
// subscription scope stays Reader-only in subscriptionRoles.bicep.
//
// --- RBAC narrowing — phase-1 of 2 (see audit P2 #16-20) -----------------
//
// `rgContributor` is too broad: the only operations the api/worker actually
// perform in this RG are (1) manage AKS clusters, (2) create / delete the
// `id-elb-openapi` user-assigned managed identity + its federated credential,
// (3) read/write VNet + subnet + NSG. Each of those has a built-in narrow
// role. Phase-1 ADDS those three roles WITHOUT removing `rgContributor`, so
// every existing code path keeps working while we collect a 7-day soak of
// App Insights to confirm no real-traffic `AuthorizationFailed` event is
// attributable to the loss of `Contributor`.
//
// Phase-2 (separate PR, after the soak window) will DELETE the three
// resources `rgContributor`, `workloadRgContributor`, and the optional
// `acrContributorForUami` in acr.bicep, leaving only the narrow set +
// `User Access Administrator`. See §12a Rule 1 for the discipline.
// --------------------------------------------------------------------------

targetScope = 'resourceGroup'

@description('Principal id (object id) of the shared UAMI used by the api/worker sidecars.')
param uamiPrincipalId string

var contributorRoleId = 'b24988ac-6180-42a0-ab88-20f7382dd24c'
var userAccessAdministratorRoleId = '18d7d88d-d35e-4fb5-a5c3-7773c20a72d9'
// Narrow built-in roles added in phase-1. GUIDs are stable across tenants.
// https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles
var managedIdentityContributorRoleId = 'e40ec5ca-96e0-45a2-b4ff-59039f2c2b59'
var networkContributorRoleId = '4d97b98b-1d4f-4787-a291-c67834d212e7'
var aksContributorRoleId = 'ed7f3fbd-7b88-4dd4-9017-9adb7ce333f8'

resource rgContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, uamiPrincipalId, contributorRoleId)
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contributorRoleId)
    description: 'elb-dashboard shared UAMI — create and manage runtime resources in this RG. PHASE-1 LEGACY: kept during the soak window; scheduled for removal in phase-2 of audit P2 #16-20.'
  }
}

resource rgUserAccessAdministrator 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, uamiPrincipalId, userAccessAdministratorRoleId)
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', userAccessAdministratorRoleId)
    description: 'elb-dashboard shared UAMI — assign runtime RBAC for AKS kubelet/workload identities.'
  }
}

// --- phase-1 narrow additions --------------------------------------------

resource rgManagedIdentityContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, uamiPrincipalId, managedIdentityContributorRoleId)
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', managedIdentityContributorRoleId)
    description: 'elb-dashboard shared UAMI — phase-1: create/manage user-assigned identities + federated credentials in this RG (replaces Contributor for MI operations).'
  }
}

resource rgNetworkContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, uamiPrincipalId, networkContributorRoleId)
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', networkContributorRoleId)
    description: 'elb-dashboard shared UAMI — phase-1: manage VNets/subnets/NSGs/private endpoints in this RG (replaces Contributor for networking).'
  }
}

resource rgAksContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, uamiPrincipalId, aksContributorRoleId)
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', aksContributorRoleId)
    description: 'elb-dashboard shared UAMI — phase-1: create/manage AKS clusters in this RG (replaces Contributor for AKS lifecycle).'
  }
}

output contributorRoleAssignmentId string = rgContributor.id
output userAccessAdministratorRoleAssignmentId string = rgUserAccessAdministrator.id
output managedIdentityContributorRoleAssignmentId string = rgManagedIdentityContributor.id
output networkContributorRoleAssignmentId string = rgNetworkContributor.id
output aksContributorRoleAssignmentId string = rgAksContributor.id
