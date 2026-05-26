// Key Vault with RBAC mode + private endpoint (when locked down) + Key Vault
// Secrets User role for the shared UAMI.

@description('Azure region.')
param location string

@description('Globally-unique Key Vault name (3-24 chars).')
param keyVaultName string

@description('AAD tenant id.')
param tenantId string

@description('Principal id of the UAMI that needs Secrets User on this vault.')
param uamiPrincipalId string

@description('Object id of the operator running azd up. Granted Secrets Officer when non-empty.')
param operatorPrincipalId string = ''

@description('Resource id of the snet-private-endpoints subnet.')
param privateEndpointSubnetId string

@description('Resource id of the platform VNet (used to link the private DNS zone).')
param vnetResourceId string

@description('If true, leaves publicNetworkAccess=Enabled during first deploy so the operator can seed secrets. Steady state must be false.')
param allowPublicAccessForBootstrap bool = true

@description('Tags applied to every resource in this module.')
param tags object = {}

var moduleTags = union(tags, {
  role: 'secrets'
})

resource kv 'Microsoft.KeyVault/vaults@2024-04-01-preview' = {
  name: keyVaultName
  location: location
  tags: moduleTags
  properties: {
    tenantId: tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    enablePurgeProtection: true
    publicNetworkAccess: allowPublicAccessForBootstrap ? 'enabled' : 'disabled'
    networkAcls: allowPublicAccessForBootstrap ? {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    } : {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
      ipRules: []
      virtualNetworkRules: []
    }
  }
}

var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'
var keyVaultSecretsOfficerRoleId = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'

resource uamiSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, uamiPrincipalId, keyVaultSecretsUserRoleId)
  scope: kv
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
  }
}

resource operatorSecretsOfficer 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(operatorPrincipalId)) {
  name: guid(kv.id, operatorPrincipalId, keyVaultSecretsOfficerRoleId)
  scope: kv
  properties: {
    principalId: operatorPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsOfficerRoleId)
  }
}

// ---------------------------------------------------------------------------
// Private endpoint + DNS zone (ALWAYS created).
//
// `allowPublicAccessForBootstrap` only controls publicNetworkAccess /
// networkAcls on the vault itself (above). The PE is created from day 1
// so the Container App reads secrets over the private path through both
// postures. See the matching note in storage.bicep for why decoupling
// these two concerns eliminates a class of broken middle states.
// ---------------------------------------------------------------------------
resource kvPrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.vaultcore.azure.net'
  location: 'global'
  tags: moduleTags
}

resource kvPrivateDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: kvPrivateDnsZone
  name: 'link-${uniqueString(vnetResourceId)}'
  location: 'global'
  tags: moduleTags
  properties: {
    virtualNetwork: { id: vnetResourceId }
    registrationEnabled: false
  }
}

resource kvPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-01-01' = {
  name: 'pe-${keyVaultName}'
  location: location
  tags: moduleTags
  properties: {
    subnet: { id: privateEndpointSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'kv-link'
        properties: {
          privateLinkServiceId: kv.id
          groupIds: [ 'vault' ]
        }
      }
    ]
  }
}

resource kvPrivateDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = {
  parent: kvPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'vault'
        properties: {
          privateDnsZoneId: kvPrivateDnsZone.id
        }
      }
    ]
  }
}

output keyVaultName string = kv.name
output keyVaultUri string = kv.properties.vaultUri
output keyVaultResourceId string = kv.id
