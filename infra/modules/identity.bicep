// User-assigned managed identity shared by all six sidecars.
//
// Role assignments scoped to the platform RG (Storage, Key Vault, ACR roles
// are assigned in the respective resource modules). This module only creates
// the identity; resource modules use it via `principalId` / `clientId`
// outputs.

@description('Azure region.')
param location string

@description('UAMI name.')
param identityName string

@description('Tags applied to every resource in this module.')
param tags object = {}

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

output identityResourceId string = uami.id
output identityPrincipalId string = uami.properties.principalId
output identityClientId string = uami.properties.clientId
output identityName string = uami.name
