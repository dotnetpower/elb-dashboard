// Workload-profile Container Apps Environment, VNet-integrated.
//
// No Azure Files storages are defined here. Earlier revisions mounted
// `redis-data` and `terminal-home` shares for sidecar persistence, but SMB
// mounts in Container Apps require a storage account key, which conflicts
// with the platform Storage account's `allowSharedKeyAccess: false`
// invariant. The control plane is designed to tolerate ephemeral sidecar
// state (queue rebuilt from `jobstate` table; terminal re-authenticates
// via MI on each session).

@description('Azure region for the environment.')
param location string

@description('Name for the Container Apps Environment.')
param environmentName string

@description('Customer id of the platform Log Analytics workspace.')
param logAnalyticsCustomerId string

@description('Resource id of the platform Log Analytics workspace.')
param logAnalyticsWorkspaceResourceId string

@description('Resource id of the snet-containerapps subnet (delegated to Microsoft.App/environments).')
param infrastructureSubnetId string

@description('If true, ingress is internal-only (no public IP). Phase 1 keeps this false.')
param internalIngress bool = false

@description('Tags applied to every resource in this module.')
param tags object = {}

var moduleTags = union(tags, {
  role: 'control-plane-env'
})

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: last(split(logAnalyticsWorkspaceResourceId, '/'))
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  tags: moduleTags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsCustomerId
        sharedKey: workspace.listKeys().primarySharedKey
      }
    }
    vnetConfiguration: {
      infrastructureSubnetId: infrastructureSubnetId
      internal: internalIngress
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    zoneRedundant: false
  }
}

output environmentResourceId string = environment.id
output environmentDefaultDomain string = environment.properties.defaultDomain
output environmentStaticIp string = environment.properties.staticIp
