// elb-dashboard — bundled Container App architecture.
//
// One azd up provisions:
//   * Platform RG (rg-${environmentName}) with all platform resources.
//   * VNet + 3 subnets (snet-containerapps, snet-private-endpoints, snet-aks).
//   * Log Analytics + Application Insights.
//   * Shared user-assigned managed identity (id-elb-control).
//   * Premium ACR (with bootstrap public access; lock down via lockdown=true).
//   * Standard_LRS Storage account with state tables / blob containers /
//     two Azure Files shares (redis-data, terminal-home).
//   * Key Vault (RBAC mode, soft-delete + purge protection).
//   * Container Apps Environment (workload-profile, VNet-integrated).
//   * Container App `ca-elb-control` with the six-sidecar template
//     (bootstrapped with hello-world image; postprovision hook builds the
//      real images and swaps them in).

targetScope = 'subscription'

// ---------------------------------------------------------------------------
// Required parameters
// ---------------------------------------------------------------------------
@minLength(1)
@maxLength(20)
@description('Name of the azd environment. Used to derive a unique resource token.')
param environmentName string

@minLength(1)
@description('Primary deployment region (e.g. koreacentral).')
param location string

@description('Object id of the user running azd up. Empty when running unattended; required for first-time deploy so the operator can read state until the api takes over.')
param principalId string = ''

@description('Microsoft Entra tenant id used to validate MSAL bearer tokens.')
param tenantId string = subscription().tenantId

@description('Application (client) id of the App Registration used by the SPA + api. Set after the App Registration is created (or re-used from the legacy deployment).')
param apiClientId string = ''

@description('Comma-separated CORS allowed origins for the api ingress. Leave empty to allow same-origin only (recommended once the SPA is served by the frontend sidecar).')
param allowedOrigins string = ''

@description('If true, every backing resource (Storage, Key Vault, ACR) gets publicNetworkAccess=Disabled and private endpoints. The very first deploy must keep this false so the postprovision hook can push images and seed secrets; flip to true on the second azd provision.')
param lockdownPrivateNetworking bool = false

// ---------------------------------------------------------------------------
// Derived names
// ---------------------------------------------------------------------------
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = {
  'azd-env-name': environmentName
  costCenter: 'elasticblast'
  topology: 'container-app-bundle-v1'
}

var rgName               = 'rg-${environmentName}'
var vnetName             = 'vnet-${environmentName}'
var logAnalyticsName     = 'log-${environmentName}-${resourceToken}'
var appInsightsName      = 'appi-${environmentName}-${resourceToken}'
var identityName         = 'id-elb-control-${resourceToken}'
var acrName              = 'acrelb${resourceToken}'  // 5-50 alphanumeric
var storageAccountName   = 'stelb${resourceToken}'    // 3-24 lowercase
var keyVaultName         = 'kv-${take(resourceToken, 18)}'
var containerEnvName     = 'cae-elb-${resourceToken}'
var controlAppName       = 'ca-elb-control'

// Allowed-origins as an array (Bicep does not have a native string-split-by-comma;
// the helper relies on the value not containing commas inside an origin).
var allowedOriginsArray = empty(allowedOrigins) ? [] : split(allowedOrigins, ',')

// ---------------------------------------------------------------------------
// Resource group
// ---------------------------------------------------------------------------
resource platformRg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: rgName
  location: location
  tags: tags
}

// ---------------------------------------------------------------------------
// Network (must exist before identity-bearing resources reference subnets)
// ---------------------------------------------------------------------------
module network 'modules/network.bicep' = {
  name: 'network-${resourceToken}'
  scope: platformRg
  params: {
    location: location
    vnetName: vnetName
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Monitoring
// ---------------------------------------------------------------------------
module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring-${resourceToken}'
  scope: platformRg
  params: {
    location: location
    logAnalyticsWorkspaceName: logAnalyticsName
    applicationInsightsName: appInsightsName
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Shared identity
// ---------------------------------------------------------------------------
module identity 'modules/identity.bicep' = {
  name: 'identity-${resourceToken}'
  scope: platformRg
  params: {
    location: location
    identityName: identityName
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Container Registry (Premium for private endpoint capability)
// ---------------------------------------------------------------------------
module acr 'modules/acr.bicep' = {
  name: 'acr-${resourceToken}'
  scope: platformRg
  params: {
    location: location
    acrName: acrName
    uamiPrincipalId: identity.outputs.identityPrincipalId
    privateEndpointSubnetId: network.outputs.privateEndpointsSubnetId
    vnetResourceId: network.outputs.vnetResourceId
    allowPublicAccessForBootstrap: !lockdownPrivateNetworking
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Platform Storage (publicNetworkAccess Disabled in steady state)
// ---------------------------------------------------------------------------
module storage 'modules/storage.bicep' = {
  name: 'storage-${resourceToken}'
  scope: platformRg
  params: {
    location: location
    storageAccountName: storageAccountName
    privateEndpointSubnetId: network.outputs.privateEndpointsSubnetId
    vnetResourceId: network.outputs.vnetResourceId
    uamiPrincipalId: identity.outputs.identityPrincipalId
    allowPublicAccessForBootstrap: !lockdownPrivateNetworking
    tags: tags
  }
}

module storageState 'modules/storageState.bicep' = {
  name: 'storage-state-${resourceToken}'
  scope: platformRg
  params: {
    storageAccountName: storage.outputs.storageAccountName
  }
}

// ---------------------------------------------------------------------------
// Key Vault
// ---------------------------------------------------------------------------
module keyvault 'modules/keyvault.bicep' = {
  name: 'kv-${resourceToken}'
  scope: platformRg
  params: {
    location: location
    keyVaultName: keyVaultName
    tenantId: tenantId
    uamiPrincipalId: identity.outputs.identityPrincipalId
    operatorPrincipalId: principalId
    privateEndpointSubnetId: network.outputs.privateEndpointsSubnetId
    vnetResourceId: network.outputs.vnetResourceId
    allowPublicAccessForBootstrap: !lockdownPrivateNetworking
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Container Apps Environment
// ---------------------------------------------------------------------------
module containerEnv 'modules/containerAppsEnvironment.bicep' = {
  name: 'cae-${resourceToken}'
  scope: platformRg
  params: {
    location: location
    environmentName: containerEnvName
    logAnalyticsCustomerId: monitoring.outputs.workspaceCustomerId
    logAnalyticsWorkspaceResourceId: monitoring.outputs.workspaceResourceId
    infrastructureSubnetId: network.outputs.containerAppsSubnetId
    tags: tags
  }
  dependsOn: [
    storageState
  ]
}

// ---------------------------------------------------------------------------
// Container App (the bundle)
// ---------------------------------------------------------------------------
module controlApp 'modules/containerAppControl.bicep' = {
  name: 'ca-${resourceToken}'
  scope: platformRg
  params: {
    location: location
    appName: controlAppName
    environmentResourceId: containerEnv.outputs.environmentResourceId
    acrLoginServer: acr.outputs.acrLoginServer
    sharedIdentityResourceId: identity.outputs.identityResourceId
    sharedIdentityClientId: identity.outputs.identityClientId
    tenantId: tenantId
    apiClientId: apiClientId
    applicationInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    platformStorageAccountName: storage.outputs.storageAccountName
    subscriptionId: subscription().subscriptionId
    allowedOrigins: allowedOriginsArray
    // First deploy uses the bootstrap image so the Container App provisions
    // before any real image exists in ACR. The postprovision hook builds the
    // images via `az acr build` and runs `az containerapp update` to swap
    // the template to the six-sidecar layout.
    useBootstrapImage: true
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Outputs (consumed by azd hooks and the operator)
// ---------------------------------------------------------------------------
output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenantId
output AZURE_RESOURCE_GROUP string = platformRg.name
output AZURE_RESOURCE_TOKEN string = resourceToken

output ACR_NAME string = acr.outputs.acrName
output ACR_LOGIN_SERVER string = acr.outputs.acrLoginServer

output STORAGE_ACCOUNT_NAME string = storage.outputs.storageAccountName
output KEY_VAULT_NAME string = keyvault.outputs.keyVaultName
output KEY_VAULT_URI string = keyvault.outputs.keyVaultUri

output APPLICATIONINSIGHTS_CONNECTION_STRING string = monitoring.outputs.appInsightsConnectionString

output CONTAINER_ENV_NAME string = containerEnvName
output CONTAINER_APP_NAME string = controlApp.outputs.controlAppName
output CONTAINER_APP_FQDN string = controlApp.outputs.controlAppFqdn
output CONTAINER_APP_URL string = 'https://${controlApp.outputs.controlAppFqdn}'

output SHARED_IDENTITY_RESOURCE_ID string = identity.outputs.identityResourceId
output SHARED_IDENTITY_CLIENT_ID string = identity.outputs.identityClientId
output SHARED_IDENTITY_PRINCIPAL_ID string = identity.outputs.identityPrincipalId

// Used by the postprovision hook to know whether to flip useBootstrapImage.
output LOCKDOWN_PRIVATE_NETWORKING bool = lockdownPrivateNetworking
