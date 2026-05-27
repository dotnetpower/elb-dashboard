// Log Analytics workspace + optional Application Insights.
//
// Used by Container Apps Environment for log streaming, and by the api
// sidecar for structured telemetry.

@description('Azure region.')
param location string

@description('Workspace name.')
param logAnalyticsWorkspaceName string

@description('Application Insights name.')
param applicationInsightsName string

@description('If true, create Application Insights and emit its connection string. Disabled by default to keep the baseline deployment lean.')
param enableApplicationInsights bool = false

@description('Principal id (object id) of the shared dashboard UAMI. When non-empty the workspace receives a Log Analytics Reader role assignment so the api sidecar can KQL the ContainerAppConsoleLogs_CL table for the Live Wall log tail. Pass empty to skip the assignment (e.g. tests or local-only what-if runs).')
param uamiPrincipalId string = ''

@description('Tags applied to every resource in this module.')
param tags object = {}

var moduleTags = union(tags, {
  role: 'observability'
})

// Log Analytics Reader (built-in). Grants the data-plane `…/query/read`
// permissions required by `azure-monitor-query` LogsQueryClient. RG-level
// Contributor does NOT cover the data plane for LA workspaces; this is the
// minimum viable role for the Live Wall fallback path.
var logAnalyticsReaderRoleId = '73c42c96-874c-492b-b04d-ab87d138a893'

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsWorkspaceName
  location: location
  tags: moduleTags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    workspaceCapping: {
      // Cap daily ingestion at 1 GiB to bound cost on a low-traffic workload.
      // Telemetry beyond this is dropped, not blocked.
      dailyQuotaGb: 1
    }
  }
}

// Live Wall fallback: api sidecar queries `ContainerAppConsoleLogs_CL` to
// surface per-sidecar tails inside the SPA. Without this grant the api gets
// `AuthorizationFailed` on the LogsQueryClient call and the Live Wall stays
// blank in deployment (which is what triggered this whole change).
resource uamiLogAnalyticsReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(uamiPrincipalId)) {
  name: guid(workspace.id, uamiPrincipalId, logAnalyticsReaderRoleId)
  scope: workspace
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', logAnalyticsReaderRoleId)
    description: 'elb-dashboard shared UAMI — Live Wall log tail (KQL against ContainerAppConsoleLogs_CL).'
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = if (enableApplicationInsights) {
  name: applicationInsightsName
  location: location
  tags: moduleTags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
  }
}

output workspaceResourceId string = workspace.id
output workspaceCustomerId string = workspace.properties.customerId
output appInsightsConnectionString string = enableApplicationInsights ? appInsights!.properties.ConnectionString : ''
