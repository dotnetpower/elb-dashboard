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

// AcrPush role for the same UAMI so the postprovision hook can build/push
// images via `az acr build` using the same identity.
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

// ---------------------------------------------------------------------------
// Private endpoint + DNS zone (only created when public access is locked).
// During first deploy we keep public access enabled so `az acr build` can
// run from the operator's machine; the postprovision hook can flip the
// `allowPublicAccessForBootstrap` flag and redeploy to lock down.
// ---------------------------------------------------------------------------
resource acrPrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (!allowPublicAccessForBootstrap) {
  name: 'privatelink.azurecr.io'
  location: 'global'
  tags: moduleTags
}

resource acrPrivateDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (!allowPublicAccessForBootstrap) {
  parent: acrPrivateDnsZone
  name: 'link-${uniqueString(vnetResourceId)}'
  location: 'global'
  tags: moduleTags
  properties: {
    virtualNetwork: { id: vnetResourceId }
    registrationEnabled: false
  }
}

resource acrPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-01-01' = if (!allowPublicAccessForBootstrap) {
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

resource acrPrivateDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = if (!allowPublicAccessForBootstrap) {
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
