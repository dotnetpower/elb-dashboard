// DNS Zone Contributor for the shared UAMI on an operator-owned public DNS zone.
//
// Lets `api.tasks.openapi.public_https.setup_openapi_public_https` upsert the
// CNAME / A record that points a custom domain (e.g. `api.elasticblast.com`) at
// the cluster's public ingress so the Let's Encrypt HTTP-01 challenge can
// validate it. The assignment is scoped to the single zone only (least
// privilege) — the UAMI cannot touch any other DNS zone or record.
//
// Conditional + default-OFF: main.bicep only deploys this module when
// `openApiCustomDnsZoneName` is non-empty, so a deployment without a custom
// domain grants nothing new (charter §12a Rule 4). The pipeline degrades to a
// manual "create this record" instruction when the role is absent, so adding
// the role is purely an enable-automation step, never a hard dependency.

targetScope = 'resourceGroup'

@description('Principal id of the shared user-assigned managed identity.')
param uamiPrincipalId string

@description('Name of the existing public DNS zone the UAMI may manage (e.g. `elasticblast.com`). Must already exist in this resource group.')
param dnsZoneName string

// Built-in "DNS Zone Contributor" — manage DNS zones and record sets, nothing
// else. https://learn.microsoft.com/azure/role-based-access-control/built-in-roles#dns-zone-contributor
var dnsZoneContributorRoleId = 'befefa01-2a29-4197-83a8-272ff33ce314'

resource dnsZone 'Microsoft.Network/dnsZones@2018-05-01' existing = {
  name: dnsZoneName
}

resource dnsZoneContributorForUami 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(dnsZone.id, uamiPrincipalId, dnsZoneContributorRoleId)
  scope: dnsZone
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      dnsZoneContributorRoleId
    )
  }
}
