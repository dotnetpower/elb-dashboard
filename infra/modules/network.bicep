// Platform VNet with three purpose-specific subnets.
//
// snet-containerapps  : /23 delegated to Microsoft.App/environments
//                       (workload-profile environment infra subnet)
// snet-private-endpoints : /27, hosts private endpoints for Storage / KV / ACR
// snet-aks            : /23, reserved for the workload AKS cluster (created
//                       elsewhere; this module just provisions the subnet)

@description('Azure region for the VNet.')
param location string

@description('Name of the VNet.')
param vnetName string

@description('CIDR for the platform VNet. Default /20 leaves room for future subnets.')
param vnetCidr string = '10.20.0.0/20'

@description('Subnet CIDR for snet-containerapps (must be /23 or larger for workload-profile env).')
param containerAppsSubnetCidr string = '10.20.0.0/23'

@description('Subnet CIDR for snet-private-endpoints.')
param privateEndpointsSubnetCidr string = '10.20.2.0/27'

@description('Subnet CIDR for snet-aks.')
param aksSubnetCidr string = '10.20.4.0/23'

@description('Tags applied to every resource in this module.')
param tags object = {}

var moduleTags = union(tags, {
  role: 'network'
})

resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: vnetName
  location: location
  tags: moduleTags
  properties: {
    addressSpace: {
      addressPrefixes: [ vnetCidr ]
    }
    subnets: [
      {
        name: 'snet-containerapps'
        properties: {
          addressPrefix: containerAppsSubnetCidr
          delegations: [
            {
              name: 'Microsoft.App.environments'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
          // The Container Apps Environment will use private endpoints to
          // reach Storage / Key Vault / ACR. No service endpoints needed.
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        name: 'snet-private-endpoints'
        properties: {
          addressPrefix: privateEndpointsSubnetCidr
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        name: 'snet-aks'
        properties: {
          addressPrefix: aksSubnetCidr
        }
      }
    ]
  }
}

output vnetResourceId string = vnet.id
output vnetName string = vnet.name
output containerAppsSubnetId string = '${vnet.id}/subnets/snet-containerapps'
output privateEndpointsSubnetId string = '${vnet.id}/subnets/snet-private-endpoints'
output aksSubnetId string = '${vnet.id}/subnets/snet-aks'
