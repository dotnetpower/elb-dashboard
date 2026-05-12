targetScope = 'subscription'

@minLength(1)
@maxLength(20)
@description('Name of the azd environment. Used to derive a unique resource token.')
param environmentName string

@minLength(1)
@description('Primary deployment region.')
param location string

@description('Object id of the user running azd up. Granted Key Vault Secrets Officer + Storage Blob Data Contributor on the platform RG.')
param principalId string = ''

@description('Microsoft Entra tenant id used for token validation.')
param tenantId string = subscription().tenantId

@description('Application (client) id of the App Registration used by the SPA + Function App.')
param apiClientId string = ''

@secure()
@description('Application (client) secret for OBO flow. Stored in Key Vault.')
param apiClientSecret string = ''

var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = {
  'azd-env-name': environmentName
  costCenter: 'elasticblast'
}

resource platformRg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module platform 'modules/platform.bicep' = {
  name: 'platform-${resourceToken}'
  scope: platformRg
  params: {
    location: location
    resourceToken: resourceToken
    environmentName: environmentName
    tenantId: tenantId
    apiClientId: apiClientId
    apiClientSecret: apiClientSecret
    principalId: principalId
    tags: tags
  }
}

output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenantId
output AZURE_RESOURCE_GROUP string = platformRg.name
output API_FUNCTION_APP_NAME string = platform.outputs.functionAppName
output API_FUNCTION_APP_HOSTNAME string = platform.outputs.functionAppHostname
output WEB_STATIC_WEB_APP_NAME string = platform.outputs.staticWebAppName
output WEB_STATIC_WEB_APP_HOSTNAME string = platform.outputs.staticWebAppHostname
output KEY_VAULT_NAME string = platform.outputs.keyVaultName
output KEY_VAULT_URI string = platform.outputs.keyVaultUri
output API_CLIENT_ID string = apiClientId
