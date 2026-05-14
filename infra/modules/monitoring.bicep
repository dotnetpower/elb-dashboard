// Log Analytics workspace + Application Insights.
//
// Used by Container Apps Environment for log streaming, and by the api
// sidecar for structured telemetry.

@description('Azure region.')
param location string

@description('Workspace name.')
param logAnalyticsWorkspaceName string

@description('Application Insights name.')
param applicationInsightsName string

@description('Tags applied to every resource in this module.')
param tags object = {}

var moduleTags = union(tags, {
  role: 'observability'
})

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

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
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
output appInsightsConnectionString string = appInsights.properties.ConnectionString
