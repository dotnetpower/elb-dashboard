// elb-dashboard — bundled Container App architecture.
//
// One azd up provisions:
//   * Platform RG (rg-${environmentName}) with all platform resources.
//   * VNet + 3 subnets (snet-containerapps, snet-private-endpoints, snet-aks).
//   * Log Analytics, with optional Application Insights.
//   * Shared user-assigned managed identity (id-elb-dashboard-*).
//   * Premium ACR (with bootstrap public access; lock down via lockdown=true).
//   * Standard_LRS Storage account with state tables / blob containers.
//     (No Azure Files shares — `redis` and `terminal` sidecars are
//      ephemeral; the broker queue is rebuilt from the `jobstate` table by
//      the beat reconciler on revision restart. See
//      `infra/modules/storageState.bicep` for the rationale.)
//   * Key Vault (RBAC mode, soft-delete + purge protection).
//   * Container Apps Environment (workload-profile, VNet-integrated).
//   * Container App `ca-elb-dashboard` with the six-sidecar template
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

@description('Frontend feature flag for the custom database builder. Set to false for production deployments that should hide this surface.')
param featureCustomDb string = 'true'

@description('Frontend feature flag for lab tools. Set to false for production deployments that should hide this surface.')
param featureLabTools string = 'true'

@description('Frontend feature flag for the browser terminal. Set to false for production deployments that should hide this surface.')
param featureTerminal string = 'true'

@description('Comma-separated CORS allowed origins for the api ingress. Leave empty to allow same-origin only (recommended once the SPA is served by the frontend sidecar).')
param allowedOrigins string = ''

@description('Per-deployment override for the optional Service Bus BLAST integration env gate. Empty (default) keeps the repo default from infra/control-plane-env.json (OFF, charter section 12a Rule 4); set to true (via azd env SERVICEBUS_ENABLED) to pin it ON so it survives every redeploy instead of being reset to the JSON default.')
param serviceBusEnabled string = ''

@description('Per-deployment override for the date-tiered results storage layout env gate (STORAGE_DATE_LAYOUT_ENABLED). Empty (default) keeps the repo default from infra/control-plane-env.json (OFF, charter section 12a Rule 4); set to true (via azd env STORAGE_DATE_LAYOUT_ENABLED) to pin it ON so native AND external (SB/OpenAPI) jobs write results under results/YYYY/MM/DD/<job_id>/ and the choice survives every redeploy instead of being reset to the JSON default.')
param storageDateLayoutEnabled string = ''

@description('If true, every backing resource (Storage, Key Vault, ACR) gets publicNetworkAccess=Disabled and private endpoints. The very first deploy must keep this false so the postprovision hook can push images and seed secrets; flip to true on the second azd provision.')
param lockdownPrivateNetworking bool = false

@description('If true, grants the shared UAMI subscription-scope Reader so the SPA discovery wizard can list RGs / storage accounts / ACRs / VMs across the subscription. Requires the deployer to have User Access Administrator (or Owner). Set to false in restricted tenants and run the equivalent `az role assignment create` from docs/auth.md by hand.')
param assignSubscriptionReader bool = true

@description('If true, grants the shared UAMI resource-group-scope Contributor and User Access Administrator for runtime resource orchestration. Set to false when equivalent roles already exist outside this template.')
param assignControlPlaneRoles bool = true

@description('If true, defines and assigns the project custom role `Elb Workload RG Creator` to the shared UAMI at subscription scope. The custom role grants ONLY `Microsoft.Resources/subscriptions/resourceGroups/{read,write,delete}` (plus sub-scope deployment reads) so AKS can auto-create its `MC_<rg>_<cluster>_<region>` node RG. The UAMI still cannot create resources inside any RG without a separate Contributor assignment at that RG scope — this is the least-privilege alternative to granting sub-scope Contributor. Disable when policy forbids any custom roles in the subscription.')
param assignWorkloadRgCreatorRole bool = true

@description('Optional name of the AKS workload cluster RG (e.g. `rg-elb-cluster`). When set, grants the shared UAMI Contributor + User Access Administrator on that RG so `api.tasks.openapi.rbac.setup_workload_identity` can create `id-elb-openapi` + federated credential + downstream role assignments. Leave empty on the first `azd up` (the RG does not exist yet); set it on the second `azd provision` after the SPA has created AKS. `scripts/dev/grant-runtime-rbac.sh` is the workstation safety net for deployments that pre-date this parameter.')
param aksClusterResourceGroup string = ''

@description('If true and `aksClusterResourceGroup` is non-empty, grants the shared UAMI Contributor + User Access Administrator on that RG. Set to false when equivalent roles already exist outside this template.')
param assignWorkloadClusterRoles bool = true

@description('Optional name of an existing public DNS zone (e.g. `elasticblast.com`) the shared UAMI may manage so the OpenAPI public-HTTPS task can auto-create the custom-domain CNAME/A record. Leave empty to disable DNS automation (the task then prints a manual "create this record" instruction). Charter §12a Rule 4: empty = no new role granted.')
param openApiCustomDnsZoneName string = ''

@description('Resource group of `openApiCustomDnsZoneName`. Defaults to the platform RG when empty.')
param openApiCustomDnsZoneResourceGroup string = ''

@description('If true, create Application Insights. The default deployment creates only Log Analytics; Application Insights can be enabled later with ENABLE_APPLICATION_INSIGHTS=true.')
param enableApplicationInsights bool = false

@description('Optional Application Insights connection string used when this deployment does NOT create its own App Insights (enableApplicationInsights=false) but points telemetry at an external/shared component. Set via azd env APPLICATIONINSIGHTS_CONNECTION_STRING so a full provision keeps the value on the api/worker/beat sidecars instead of wiping it to empty. Empty keeps prior behaviour.')
param applicationInsightsConnectionStringOverride string = ''

@maxLength(6)
@description('Optional generated resource name slot, for example slot01 when rg-elb-dashboard already exists and should be preserved. Bicep converts slot01 to the visible -01 resource-name suffix.')
param resourceNameSlot string = ''

// ---------------------------------------------------------------------------
// Resource tagging (CAF-aligned)
// ---------------------------------------------------------------------------
// Common tags merged into every Azure resource provisioned by this template.
// Per-module `role` tag is added inside each module via union(tags, { role: ... }).
// Conventions: lowerCamelCase keys (except azd-required `azd-env-name`).

@description('Cost-center tag value used for chargeback / cost analysis filters.')
param costCenter string = 'elasticblast'

@description('Optional contact email surfaced as the `owner` tag. Leave empty to omit the tag.')
param ownerEmail string = ''

// ---------------------------------------------------------------------------
// Derived names
// ---------------------------------------------------------------------------
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var baseTags = {
  // azd contract: marks every resource as belonging to this azd environment so
  // `azd down` finds and deletes them.
  'azd-env-name': environmentName
  app: 'elb-dashboard'
  environment: environmentName
  costCenter: costCenter
  managedBy: 'azd'
  repo: 'https://github.com/dotnetpower/elb-dashboard'
  // Topology marker: lets future agents tell at-a-glance which generation of
  // the platform a resource was provisioned by. Bump when sidecar layout changes.
  topology: 'container-app-bundle-v1'
}
var tags = empty(ownerEmail) ? baseTags : union(baseTags, {
  owner: ownerEmail
})

var resourceNameSuffix = empty(resourceNameSlot) ? '' : '-${replace(resourceNameSlot, 'slot', '')}'
var compactResourceNameSuffix = replace(resourceNameSlot, 'slot', '')
var compactResourceTokenLength = 10 - length(compactResourceNameSuffix)
var keyVaultTokenLength = 7 - length(resourceNameSuffix)
var hyphenatedNamePrefix = 'elb-dashboard${resourceNameSuffix}'
var compactNamePrefix    = 'elbdashboard${compactResourceNameSuffix}'
var rgName               = 'rg-${hyphenatedNamePrefix}'
var vnetName             = 'vnet-${hyphenatedNamePrefix}'
var logAnalyticsName     = 'log-${hyphenatedNamePrefix}-${resourceToken}'
var appInsightsName      = 'appi-${hyphenatedNamePrefix}-${resourceToken}'
var identityName         = 'id-${hyphenatedNamePrefix}-${take(resourceToken, 8)}'
var acrName              = 'acr${compactNamePrefix}${take(resourceToken, compactResourceTokenLength)}'  // 5-50 alphanumeric
var storageAccountName   = 'st${compactNamePrefix}${take(resourceToken, compactResourceTokenLength)}'   // 3-24 lowercase
var keyVaultName         = 'kv-${hyphenatedNamePrefix}-${take(resourceToken, keyVaultTokenLength)}'
var containerEnvName     = 'cae-${hyphenatedNamePrefix}-${take(resourceToken, 8)}'
var controlAppName       = 'ca-${hyphenatedNamePrefix}'
var workspaceTags = union(tags, {
  'elb-workload-rg': rgName
  'elb-acr-rg': rgName
  'elb-acr': acrName
  'elb-storage': storageAccountName
  'elb-region': location
})

// Allowed-origins as an array (Bicep does not have a native string-split-by-comma;
// the helper relies on the value not containing commas inside an origin).
var allowedOriginsArray = empty(allowedOrigins) ? [] : split(allowedOrigins, ',')

// ---------------------------------------------------------------------------
// Resource group
// ---------------------------------------------------------------------------
resource platformRg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: rgName
  location: location
  // Keep discovery tags off the resource group until postprovision has built
  // images, swapped the six-sidecar template, and observed a healthy API.
  // Child resources still receive workspaceTags for cost/ownership tracing.
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
    tags: workspaceTags
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
    enableApplicationInsights: enableApplicationInsights
    // Grant Log Analytics Reader on the workspace so the api sidecar's
    // Live Wall fallback can KQL `ContainerAppConsoleLogs_CL`. Without
    // this, LogsQueryClient returns AuthorizationFailed and the SPA tiles
    // stay blank in deployment (file tail path only exists in local dev).
    uamiPrincipalId: identity.outputs.identityPrincipalId
    tags: workspaceTags
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
    tags: workspaceTags
  }
}

// ---------------------------------------------------------------------------
// Subscription-scope Reader for the shared UAMI (SPA discovery wizard).
// See infra/modules/subscriptionRoles.bicep for the rationale — without
// this the wizard's RG / storage / ACR list calls succeed-but-empty in
// production, mirroring the local-compose "no az login" failure mode.
// ---------------------------------------------------------------------------
module subscriptionRoles 'modules/subscriptionRoles.bicep' = if (assignSubscriptionReader) {
  name: 'sub-roles-${resourceToken}'
  // Sub-scope (no `scope:` clause) — main.bicep already targets subscription.
  params: {
    uamiPrincipalId: identity.outputs.identityPrincipalId
  }
}

module controlPlaneRoles 'modules/controlPlaneRoles.bicep' = if (assignControlPlaneRoles) {
  name: 'control-plane-roles-${resourceToken}'
  scope: platformRg
  params: {
    uamiPrincipalId: identity.outputs.identityPrincipalId
  }
}

// ---------------------------------------------------------------------------
// Sub-scope custom role: "Elb Workload RG Creator".
//
// AKS auto-creates `MC_<rg>_<cluster>_<region>` at sub scope on every
// managed cluster create. Without `resourceGroups/write` at sub scope
// the AKS resource provider returns AuthorizationFailed even when the
// cluster RG itself carries Contributor. This module grants the
// narrowest permission set that satisfies that requirement instead of
// granting the UAMI sub-scope Contributor (which would let it provision
// arbitrary resources anywhere in the subscription).
// ---------------------------------------------------------------------------
module workloadRgCreatorRole 'modules/workloadRgCreatorRole.bicep' = if (assignWorkloadRgCreatorRole) {
  name: 'workload-rg-creator-${resourceToken}'
  // Sub-scope (no `scope:` clause) — main.bicep targets subscription.
  params: {
    uamiPrincipalId: identity.outputs.identityPrincipalId
    resourceToken: resourceToken
  }
}

// ---------------------------------------------------------------------------
// Workload-cluster RG roles (Contributor + UAA on the AKS cluster's RG).
//
// Required by `api.tasks.openapi.rbac.setup_workload_identity` so the
// dashboard can create `id-elb-openapi` + federated credential + downstream
// role assignments inside the AKS workload RG. Skipped when
// `aksClusterResourceGroup` is empty (first `azd up`, before the SPA has
// created AKS). `scripts/dev/grant-runtime-rbac.sh` is the workstation
// safety net for deployments that pre-date this module or that cannot run
// `azd provision` after AKS provisioning.
// ---------------------------------------------------------------------------
module workloadClusterRoles 'modules/workloadClusterRoles.bicep' = if (assignWorkloadClusterRoles && !empty(aksClusterResourceGroup)) {
  name: 'workload-cluster-roles-${resourceToken}'
  scope: resourceGroup(aksClusterResourceGroup)
  params: {
    uamiPrincipalId: identity.outputs.identityPrincipalId
  }
}

// ---------------------------------------------------------------------------
// DNS Zone Contributor (custom-domain automation for OpenAPI public HTTPS)
//
// Scoped to a single operator-owned public DNS zone so the public-HTTPS task
// can upsert the custom-domain CNAME/A record. Conditional + default-OFF: only
// deploys when `openApiCustomDnsZoneName` is set. Skipped entirely otherwise, so
// a deployment without a custom domain grants no new role (charter §12a Rule 4).
// ---------------------------------------------------------------------------
module dnsZoneRoles 'modules/dnsZoneRoles.bicep' = if (!empty(openApiCustomDnsZoneName)) {
  name: 'dns-zone-roles-${resourceToken}'
  scope: resourceGroup(empty(openApiCustomDnsZoneResourceGroup) ? rgName : openApiCustomDnsZoneResourceGroup)
  params: {
    uamiPrincipalId: identity.outputs.identityPrincipalId
    dnsZoneName: openApiCustomDnsZoneName
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
    tags: workspaceTags
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
    tags: workspaceTags
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
    tags: workspaceTags
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
    tags: workspaceTags
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
    sharedIdentityPrincipalId: identity.outputs.identityPrincipalId
    tenantId: tenantId
    apiClientId: apiClientId
    featureCustomDb: featureCustomDb
    featureLabTools: featureLabTools
    featureTerminal: featureTerminal
    serviceBusEnabled: serviceBusEnabled
    storageDateLayoutEnabled: storageDateLayoutEnabled
    applicationInsightsConnectionString: empty(monitoring.outputs.appInsightsConnectionString) ? applicationInsightsConnectionStringOverride : monitoring.outputs.appInsightsConnectionString
    logAnalyticsWorkspaceId: monitoring.outputs.workspaceCustomerId
    logAnalyticsWorkspaceResourceId: monitoring.outputs.workspaceResourceId
    platformStorageAccountName: storage.outputs.storageAccountName
    subscriptionId: subscription().subscriptionId
    allowedOrigins: allowedOriginsArray
    // First deploy uses the bootstrap image so the Container App provisions
    // before any real image exists in ACR. The postprovision hook builds the
    // images via `az acr build` and runs `az containerapp update` to swap
    // the template to the six-sidecar layout.
    useBootstrapImage: true
    tags: workspaceTags
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

output APPLICATIONINSIGHTS_CONNECTION_STRING string = empty(monitoring.outputs.appInsightsConnectionString) ? applicationInsightsConnectionStringOverride : monitoring.outputs.appInsightsConnectionString
output LOG_ANALYTICS_WORKSPACE_ID string = monitoring.outputs.workspaceCustomerId

output CONTAINER_ENV_NAME string = containerEnvName
output CONTAINER_APP_NAME string = controlApp.outputs.controlAppName
output CONTAINER_APP_FQDN string = controlApp.outputs.controlAppFqdn
output CONTAINER_APP_URL string = 'https://${controlApp.outputs.controlAppFqdn}'

output SHARED_IDENTITY_RESOURCE_ID string = identity.outputs.identityResourceId
output SHARED_IDENTITY_CLIENT_ID string = identity.outputs.identityClientId
output SHARED_IDENTITY_PRINCIPAL_ID string = identity.outputs.identityPrincipalId

// Used by the postprovision hook to know whether to flip useBootstrapImage.
output LOCKDOWN_PRIVATE_NETWORKING bool = lockdownPrivateNetworking
