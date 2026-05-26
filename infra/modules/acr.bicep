// Azure Container Registry (Premium SKU for private endpoint support) +
// AcrPull role assignment for the shared UAMI.
//
// Premium is required because Standard does not support private endpoints
// and we need the registry to be reachable from the Container App over the
// VNet without going through the public internet.

@description('Azure region.')
param location string

@description('Globally-unique ACR name (5-50 chars, alphanumeric).')
param acrName string

@description('Principal id of the UAMI that needs AcrPull on this registry.')
param uamiPrincipalId string

@description('Resource id of the snet-private-endpoints subnet.')
param privateEndpointSubnetId string

@description('Resource id of the platform VNet (used to link the private DNS zone).')
param vnetResourceId string

@description('If true, leaves publicNetworkAccess=Enabled until the postprovision image build finishes. Set to false in steady state to lock the registry to the VNet.')
param allowPublicAccessForBootstrap bool = true

@description('Tags applied to every resource in this module.')
param tags object = {}

var moduleTags = union(tags, {
  role: 'registry'
})

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  tags: moduleTags
  sku: {
    name: 'Premium'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: allowPublicAccessForBootstrap ? 'Enabled' : 'Disabled'
    networkRuleSet: allowPublicAccessForBootstrap ? null : {
      defaultAction: 'Deny'
      ipRules: []
    }
    networkRuleBypassOptions: 'AzureServices'
    zoneRedundancy: 'Disabled'
    anonymousPullEnabled: false
  }
}

// AcrPull role for the shared UAMI so Container Apps can pull images.
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource acrPullForUami 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, uamiPrincipalId, acrPullRoleId)
  scope: acr
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

// AcrPush role for the same UAMI so runtime image builds can push images.
var acrPushRoleId = '8311e382-0749-4cb8-b61a-304f252e45ec'

resource acrPushForUami 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, uamiPrincipalId, acrPushRoleId)
  scope: acr
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPushRoleId)
  }
}

// ACR Tasks (`az acr build` / SDK scheduleRun) are management-plane actions,
// not data-plane push operations. AcrPush alone cannot call
// Microsoft.ContainerRegistry/registries/scheduleRun/action, so the worker MI
// needs Contributor at registry scope to build runtime ElasticBLAST images.
var contributorRoleId = 'b24988ac-6180-42a0-ab88-20f7382dd24c'

resource acrContributorForUami 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, uamiPrincipalId, contributorRoleId)
  scope: acr
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contributorRoleId)
  }
}

// ---------------------------------------------------------------------------
// Private endpoint + DNS zone (ALWAYS created).
//
// `allowPublicAccessForBootstrap` only controls publicNetworkAccess on the
// registry itself (above). The PE is created from day 1 so the Container
// App pulls images over the private path through both postures. See the
// matching note in storage.bicep for why decoupling these two concerns
// eliminates a class of broken middle states (public disabled + no PE).
// ---------------------------------------------------------------------------
resource acrPrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.azurecr.io'
  location: 'global'
  tags: moduleTags
}

resource acrPrivateDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: acrPrivateDnsZone
  name: 'link-${uniqueString(vnetResourceId)}'
  location: 'global'
  tags: moduleTags
  properties: {
    virtualNetwork: { id: vnetResourceId }
    registrationEnabled: false
  }
}

resource acrPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-01-01' = {
  name: 'pe-${acrName}'
  location: location
  tags: moduleTags
  properties: {
    subnet: { id: privateEndpointSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'acr-link'
        properties: {
          privateLinkServiceId: acr.id
          groupIds: [ 'registry' ]
        }
      }
    ]
  }
}

resource acrPrivateDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = {
  parent: acrPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'acr'
        properties: {
          privateDnsZoneId: acrPrivateDnsZone.id
        }
      }
    ]
  }
}

output acrLoginServer string = acr.properties.loginServer
output acrResourceId string = acr.id
output acrName string = acr.name
