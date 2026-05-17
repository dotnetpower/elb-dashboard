// Resource-group-scope RBAC for the shared control-plane UAMI.
//
// The API/worker sidecars use this identity for user-triggered runtime
// operations: creating AKS clusters in the platform RG, scheduling ACR Tasks,
// and assigning AcrPull / Storage Blob Data Contributor to AKS kubelet
// identities. Keep these permissions at the deployment resource-group scope;
// subscription scope stays Reader-only in subscriptionRoles.bicep.

targetScope = 'resourceGroup'

@description('Principal id (object id) of the shared UAMI used by the api/worker sidecars.')
param uamiPrincipalId string

var contributorRoleId = 'b24988ac-6180-42a0-ab88-20f7382dd24c'
var userAccessAdministratorRoleId = '18d7d88d-d35e-4fb5-a5c3-7773c20a72d9'

resource rgContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, uamiPrincipalId, contributorRoleId)
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contributorRoleId)
    description: 'elb-dashboard shared UAMI — create and manage runtime resources in this RG.'
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

output contributorRoleAssignmentId string = rgContributor.id
output userAccessAdministratorRoleAssignmentId string = rgUserAccessAdministrator.id
