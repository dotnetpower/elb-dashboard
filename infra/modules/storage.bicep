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
// Blob soft-delete safety net + cost lifecycle (issues #69 / #76).
// The dfs recursive directory delete used by job-delete + retention purge is
// irreversible at the API level; blob + container soft-delete keep deleted
// result/query blobs recoverable for 7 days — a guardrail REQUIRED before
// STORAGE_DATE_LAYOUT_ENABLED / age-based retention is enabled in any
// environment. Soft delete and lifecycle management are supported on HNS
// (ADLS Gen2) accounts; validated at `azd provision`.
// ---------------------------------------------------------------------------
resource blobServices 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

// Cost lifecycle: tier result blobs to Cool after 30 days of no modification.
// Tiering ONLY (no delete, no Archive) — actual retention/deletion is the
// app-level retention task (which also keeps the job catalog consistent), and
// Cool stays directly readable (no rehydration, unlike Archive).
resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storage
  name: 'default'
  dependsOn: [
    blobServices
  ]
  properties: {
    policy: {
      rules: [
        {
          enabled: true
          name: 'results-tier-cool-30d'
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: [
                'blockBlob'
              ]
              prefixMatch: [
                'results/'
              ]
            }
            actions: {
              baseBlob: {
                tierToCool: {
                  daysAfterModificationGreaterThan: 30
                }
              }
            }
          }
        }
      ]
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
// Private DNS zones + private endpoints (ALWAYS created).
// dfs zone added because HNS-enabled accounts expose the dfs endpoint that
// `azcopy` and the ADLS Gen2 SDK use for hierarchical-namespace operations.
// File endpoint removed: no SMB mounts, so no file private endpoint needed.
//
// `allowPublicAccessForBootstrap` only controls publicNetworkAccess /
// networkAcls above. Private endpoints are orthogonal and must exist from
// day 1 so the Container App's data-plane path keeps working through both
// the bootstrap-public posture and the locked-down posture. Pre-2026-05-27
// these resources were gated behind `if (!allowPublicAccessForBootstrap)`,
// which created a broken middle state when `publicNetworkAccess` was later
// flipped to `Disabled` (manually or by drift) without re-running provision
// with `lockdownPrivateNetworking=true`: workloads lost the public path AND
// had no PE to fall back to, surfacing as `403 AuthorizationFailure` on
// every Storage Tables call. Always creating PEs makes that state
// unreachable. (Charter §9 lockdown is still enforced by
// publicNetworkAccess / networkAcls, which remain gated above.)
// ---------------------------------------------------------------------------
var storageDnsSuffix = environment().suffixes.storage
var endpointGroups = [
  { suffix: 'blob',  zone: 'privatelink.blob.${storageDnsSuffix}'  }
  { suffix: 'dfs',   zone: 'privatelink.dfs.${storageDnsSuffix}'   }
  { suffix: 'table', zone: 'privatelink.table.${storageDnsSuffix}' }
]

resource zones 'Microsoft.Network/privateDnsZones@2024-06-01' = [for g in endpointGroups: {
  name: g.zone
  location: 'global'
  tags: moduleTags
}]

resource zoneLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (g, i) in endpointGroups: {
  name: '${g.zone}/link-${uniqueString(vnetResourceId)}'
  location: 'global'
  tags: moduleTags
  properties: {
    virtualNetwork: { id: vnetResourceId }
    registrationEnabled: false
  }
  dependsOn: [ zones[i] ]
}]

resource endpoints 'Microsoft.Network/privateEndpoints@2024-01-01' = [for g in endpointGroups: {
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

resource endpointDnsGroups 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = [for (g, i) in endpointGroups: {
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
