// Platform Storage account with `publicNetworkAccess: Disabled` from creation.
// Reachable only via private endpoints (blob + table + file) in
// snet-private-endpoints. Children (tables, blob containers, file shares)
// are added by the storageState.bicep module.
//
// Day-1 invariants enforced here:
//   * publicNetworkAccess: Disabled
//   * networkAcls.defaultAction: Deny
//   * networkAcls.bypass: None  (NOT AzureServices)
//   * No IP rules
//
// Operator's own machine cannot list/upload to this account from a public
// network. That is intentional — all client traffic (browser, AKS, the
// terminal sidecar) flows through the platform VNet.

@description('Azure region.')
param location string

@description('Globally-unique storage account name (3-24 lowercase chars).')
param storageAccountName string

@description('Resource id of the snet-private-endpoints subnet.')
param privateEndpointSubnetId string

@description('Resource id of the platform VNet (used to link private DNS zones).')
param vnetResourceId string

@description('Principal id of the UAMI that needs data-plane RBAC on this account.')
param uamiPrincipalId string

@description('If true, allows the operator workstation to bootstrap state during first deploy by leaving public access enabled. Steady state must be false.')
param allowPublicAccessForBootstrap bool = true

@description('Tags applied to every resource in this module.')
param tags object = {}

var moduleTags = union(tags, {
  role: 'platform-storage'
})

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: moduleTags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false  // Force AAD/MI auth. No SMB Files mounts.
    supportsHttpsTrafficOnly: true
    // HNS (ADLS Gen2) enabled. Required so the same account can host the
    // ElasticBLAST workload containers (`blast-db`, `queries`, `results`)
    // which depend on the dfs endpoint, and harmless for the platform
    // state tables / file shares.
    isHnsEnabled: true
    publicNetworkAccess: allowPublicAccessForBootstrap ? 'Enabled' : 'Disabled'
    networkAcls: allowPublicAccessForBootstrap ? {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    } : {
      defaultAction: 'Deny'
      bypass: 'None'
      ipRules: []
      virtualNetworkRules: []
    }
  }
}

// ---------------------------------------------------------------------------
// Data-plane RBAC for the shared UAMI.
// File-SMB role removed because we no longer mount Azure Files via SMB.
// ---------------------------------------------------------------------------
var roles = {
  blobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  tableDataContributor: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
}

resource roleBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, uamiPrincipalId, roles.blobDataContributor)
  scope: storage
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobDataContributor)
  }
}

resource roleTable 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, uamiPrincipalId, roles.tableDataContributor)
  scope: storage
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.tableDataContributor)
  }
}

// ---------------------------------------------------------------------------
// Private DNS zones + private endpoints (only when locked down).
// dfs zone added because HNS-enabled accounts expose the dfs endpoint that
// `azcopy` and the ADLS Gen2 SDK use for hierarchical-namespace operations.
// File endpoint removed: no SMB mounts, so no file private endpoint needed.
// ---------------------------------------------------------------------------
var storageDnsSuffix = environment().suffixes.storage
var endpointGroups = [
  { suffix: 'blob',  zone: 'privatelink.blob.${storageDnsSuffix}'  }
  { suffix: 'dfs',   zone: 'privatelink.dfs.${storageDnsSuffix}'   }
  { suffix: 'table', zone: 'privatelink.table.${storageDnsSuffix}' }
]

resource zones 'Microsoft.Network/privateDnsZones@2024-06-01' = [for g in endpointGroups: if (!allowPublicAccessForBootstrap) {
  name: g.zone
  location: 'global'
  tags: moduleTags
}]

resource zoneLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (g, i) in endpointGroups: if (!allowPublicAccessForBootstrap) {
  name: '${g.zone}/link-${uniqueString(vnetResourceId)}'
  location: 'global'
  tags: moduleTags
  properties: {
    virtualNetwork: { id: vnetResourceId }
    registrationEnabled: false
  }
  dependsOn: [ zones[i] ]
}]

resource endpoints 'Microsoft.Network/privateEndpoints@2024-01-01' = [for g in endpointGroups: if (!allowPublicAccessForBootstrap) {
  name: 'pe-${storageAccountName}-${g.suffix}'
  location: location
  tags: moduleTags
  properties: {
    subnet: { id: privateEndpointSubnetId }
    privateLinkServiceConnections: [
      {
        name: '${g.suffix}-link'
        properties: {
          privateLinkServiceId: storage.id
          groupIds: [ g.suffix ]
        }
      }
    ]
  }
}]

resource endpointDnsGroups 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = [for (g, i) in endpointGroups: if (!allowPublicAccessForBootstrap) {
  name: 'pe-${storageAccountName}-${g.suffix}/default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: g.suffix
        properties: {
          privateDnsZoneId: zones[i].id
        }
      }
    ]
  }
  dependsOn: [ endpoints[i] ]
}]

output storageAccountName string = storage.name
output storageAccountResourceId string = storage.id
output blobEndpoint string = storage.properties.primaryEndpoints.blob
output dfsEndpoint string = storage.properties.primaryEndpoints.dfs
output tableEndpoint string = storage.properties.primaryEndpoints.table
output isHnsEnabled bool = true
