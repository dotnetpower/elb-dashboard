@description('Azure region for all resources in this module.')
param location string

@description('Short token derived from azd env name + subscription id, used to make resource names globally unique.')
param resourceToken string

@description('Name of the azd environment.')
param environmentName string

@description('Microsoft Entra tenant id.')
param tenantId string

@description('Application (client) id of the App Registration used by the SPA + Function App.')
param apiClientId string

@secure()
@description('Application (client) secret for OBO flow. Store in Key Vault after first deploy.')
param apiClientSecret string = ''

@description('Object id of the user running azd up. Empty when running unattended.')
param principalId string

@description('Resource tags applied to every resource.')
param tags object

// ----------------------------------------------------------------------------
// Storage Account (required by Azure Functions runtime + Durable Functions)
// ----------------------------------------------------------------------------
resource functionStorage 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name: 'stelb${resourceToken}'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    publicNetworkAccess: 'Enabled'
    supportsHttpsTrafficOnly: true
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

// ----------------------------------------------------------------------------
// Application Insights + Log Analytics
// ----------------------------------------------------------------------------
resource logWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${environmentName}-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${environmentName}-${resourceToken}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logWorkspace.id
  }
}

// ----------------------------------------------------------------------------
// Key Vault — stores Remote Terminal VM passwords + SSH host keys
// ----------------------------------------------------------------------------
resource keyVault 'Microsoft.KeyVault/vaults@2024-04-01-preview' = {
  name: 'kv-${take(resourceToken, 18)}'
  location: location
  tags: tags
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
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

// ----------------------------------------------------------------------------
// Function App (Linux, Python 3.11, Flex Consumption)
// ----------------------------------------------------------------------------
resource hostingPlan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'plan-${environmentName}-${resourceToken}'
  location: location
  tags: tags
  kind: 'linux'
  sku: {
    name: 'Y1'
    tier: 'Dynamic'
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: 'func-${environmentName}-${resourceToken}'
  location: location
  tags: union(tags, {
    'azd-service-name': 'api'
  })
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: hostingPlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'Python|3.11'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      cors: {
        allowedOrigins: [
          'https://portal.azure.com'
        ]
        supportCredentials: false
      }
      appSettings: [
        {
          name: 'AzureWebJobsStorage__accountName'
          value: functionStorage.name
        }
        {
          name: 'AzureWebJobsStorage__credential'
          value: 'managedidentity'
        }
        {
          name: 'FUNCTIONS_EXTENSION_VERSION'
          value: '~4'
        }
        {
          name: 'FUNCTIONS_WORKER_RUNTIME'
          value: 'python'
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
        {
          name: 'AZURE_TENANT_ID'
          value: tenantId
        }
        {
          name: 'API_CLIENT_ID'
          value: apiClientId
        }
        {
          name: 'KEY_VAULT_URI'
          value: keyVault.properties.vaultUri
        }
        {
          name: 'TERMINAL_DEFAULT_RG'
          value: 'rg-elb-terminal'
        }
        {
          name: 'TERMINAL_DEFAULT_REGION'
          value: location
        }
        {
          name: 'PYTHON_ENABLE_WORKER_EXTENSIONS'
          value: '1'
        }
        {
          name: 'API_CLIENT_SECRET'
          value: !empty(apiClientSecret) ? '@Microsoft.KeyVault(SecretUri=${apiClientSecretKv.properties.secretUri})' : ''
        }
      ]
    }
  }
}

// Store the client secret in Key Vault (only if provided)
resource apiClientSecretKv 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(apiClientSecret)) {
  parent: keyVault
  name: 'api-client-secret'
  properties: {
    value: apiClientSecret
    contentType: 'text/plain'
  }
}

// ----------------------------------------------------------------------------
// Auth note: Easy Auth is NOT used. Our custom token.py validates JWT tokens
// directly. Easy Auth's audience validation conflicts with MSAL.js tokens
// that carry the api://{clientId}/user_impersonation scope.
// ----------------------------------------------------------------------------

// ----------------------------------------------------------------------------
// Static Web App (hosts the SPA, proxies /api -> Function App)
// SWA is not available in all regions; use East Asia as closest to Korea Central.
// ----------------------------------------------------------------------------
var swaLocation = location == 'koreacentral' ? 'eastasia' : location

resource staticWebApp 'Microsoft.Web/staticSites@2024-04-01' = {
  name: 'stapp-${environmentName}-${resourceToken}'
  location: swaLocation
  tags: union(tags, {
    'azd-service-name': 'web'
  })
  sku: {
    name: 'Standard'
    tier: 'Standard'
  }
  properties: {
    provider: 'Custom'
  }
}

resource swaBackend 'Microsoft.Web/staticSites/linkedBackends@2024-04-01' = {
  parent: staticWebApp
  name: 'api'
  properties: {
    backendResourceId: functionApp.id
    region: location
  }
}

// ----------------------------------------------------------------------------
// RBAC — grant Function App's managed identity access to Key Vault + Storage
// ----------------------------------------------------------------------------
var keyVaultSecretsOfficerRoleId = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
var storageBlobDataOwnerRoleId = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var storageQueueDataContribRoleId = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
var storageTableDataContribRoleId = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'

resource funcKvRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, functionApp.id, keyVaultSecretsOfficerRoleId)
  scope: keyVault
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsOfficerRoleId)
  }
}

resource funcStorageBlobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(functionStorage.id, functionApp.id, storageBlobDataOwnerRoleId)
  scope: functionStorage
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataOwnerRoleId)
  }
}

resource funcStorageQueueRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(functionStorage.id, functionApp.id, storageQueueDataContribRoleId)
  scope: functionStorage
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageQueueDataContribRoleId)
  }
}

resource funcStorageTableRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(functionStorage.id, functionApp.id, storageTableDataContribRoleId)
  scope: functionStorage
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageTableDataContribRoleId)
  }
}

resource userKvRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(keyVault.id, principalId, keyVaultSecretsOfficerRoleId)
  scope: keyVault
  properties: {
    principalId: principalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsOfficerRoleId)
  }
}

// ----------------------------------------------------------------------------
// Outputs
// ----------------------------------------------------------------------------
output functionAppName string = functionApp.name
output functionAppHostname string = functionApp.properties.defaultHostName
output staticWebAppName string = staticWebApp.name
output staticWebAppHostname string = staticWebApp.properties.defaultHostname
output keyVaultName string = keyVault.name
output keyVaultUri string = keyVault.properties.vaultUri
output storageAccountName string = functionStorage.name
output appInsightsConnectionString string = appInsights.properties.ConnectionString
