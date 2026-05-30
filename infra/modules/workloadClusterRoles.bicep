// Workload-cluster resource-group RBAC for the shared control-plane UAMI.
//
// Sibling of controlPlaneRoles.bicep. controlPlaneRoles.bicep grants the
// dashboard MI Contributor + User Access Administrator on the *dashboard's*
// RG (where the Container App lives). This module grants the same pair on
// the **AKS cluster's RG** (typically `rg-elb-cluster`), where the
// workload-side resources live:
//
//   * `id-elb-openapi` user-assigned managed identity for the OpenAPI pod.
//   * Federated Identity Credential under it (issued by the AKS OIDC issuer).
//   * Three downstream role assignments to that MI:
//     - Contributor on the cluster RG
//     - Storage Blob Data Contributor on the workload Storage account
//     - Azure Kubernetes Service Cluster User Role on the AKS cluster itself
//
// Without this pair, `api.tasks.openapi.rbac.setup_workload_identity` fails
// the moment it tries to create `id-elb-openapi`, and the SPA shows:
//   "workload identity setup failed; OpenAPI pod would have no AZURE_CLIENT_ID."
//
// Deployment requires the AKS cluster RG to already exist. Typical workflow:
//   1. First `azd up` provisions the dashboard with no AKS — leave
//      `aksClusterResourceGroup` empty so this module is skipped.
//   2. Operator creates the AKS cluster via the SPA wizard (this creates
//      `rg-elb-cluster` as a side effect).
//   3. Operator re-runs `azd provision` with `aksClusterResourceGroup`
//      set so this module grants the RBAC needed by future OpenAPI
//      deploys.
//
// Operators who skip step 3 (or who run AKS provisioning out-of-band)
// can still recover with `scripts/dev/grant-runtime-rbac.sh`, which is
// also called as a self-healing preflight by `cli-upgrade.sh` and as a
// safety net at the end of `postprovision.sh`.

targetScope = 'resourceGroup'

@description('Principal id (object id) of the shared UAMI used by the api/worker sidecars.')
param uamiPrincipalId string

var contributorRoleId = 'b24988ac-6180-42a0-ab88-20f7382dd24c'
var userAccessAdministratorRoleId = '18d7d88d-d35e-4fb5-a5c3-7773c20a72d9'
// Narrow built-in roles added in phase-1 of audit P2 #16-20. The sibling
// controlPlaneRoles.bicep keeps the symmetric set on the dashboard RG; see
// that file's header for the phase-1/phase-2 split rationale.
var managedIdentityContributorRoleId = 'e40ec5ca-96e0-45a2-b4ff-59039f2c2b59'
var networkContributorRoleId = '4d97b98b-1d4f-4787-a291-c67834d212e7'
var aksContributorRoleId = 'ed7f3fbd-7b88-4dd4-9017-9adb7ce333f8'

resource workloadRgContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, uamiPrincipalId, contributorRoleId)
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contributorRoleId)
    description: 'elb-dashboard shared UAMI — create id-elb-openapi MI + federated cred + read/list AKS in the workload RG. PHASE-1 LEGACY: kept during the soak window; scheduled for removal in phase-2 of audit P2 #16-20.'
  }
}

resource workloadRgUserAccessAdministrator 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, uamiPrincipalId, userAccessAdministratorRoleId)
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', userAccessAdministratorRoleId)
    description: 'elb-dashboard shared UAMI — assign Contributor + AKS Cluster User to id-elb-openapi in the workload RG.'
  }
}

// --- phase-1 narrow additions --------------------------------------------

resource workloadRgManagedIdentityContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, uamiPrincipalId, managedIdentityContributorRoleId)
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', managedIdentityContributorRoleId)
    description: 'elb-dashboard shared UAMI — phase-1: create/manage id-elb-openapi + federated credentials in the workload RG (replaces Contributor for MI operations).'
  }
}

resource workloadRgNetworkContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, uamiPrincipalId, networkContributorRoleId)
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', networkContributorRoleId)
    description: 'elb-dashboard shared UAMI — phase-1: AKS cluster CNI + load-balancer + NSG management in the workload RG (replaces Contributor for networking).'
  }
}

resource workloadRgAksContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, uamiPrincipalId, aksContributorRoleId)
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', aksContributorRoleId)
    description: 'elb-dashboard shared UAMI — phase-1: manage the AKS cluster resource in the workload RG (replaces Contributor for AKS lifecycle).'
  }
}

output contributorRoleAssignmentId string = workloadRgContributor.id
output userAccessAdministratorRoleAssignmentId string = workloadRgUserAccessAdministrator.id
output managedIdentityContributorRoleAssignmentId string = workloadRgManagedIdentityContributor.id
output networkContributorRoleAssignmentId string = workloadRgNetworkContributor.id
output aksContributorRoleAssignmentId string = workloadRgAksContributor.id
