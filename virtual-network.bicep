metadata description = 'New virtual network for NSS: a service subnet behind a NAT gateway, a management subnet and, optionally, a subnet for the NSS certificate deployment script.'

@description('Azure region for the network resources.')
param location string

@description('Name of the NSS virtual machine. Used as the prefix for the NAT gateway and its public IP resources.')
param vmName string

@description('Name of the virtual network to create.')
param virtualNetworkName string

@description('Address range of the virtual network, in CIDR notation.')
param addressPrefix string

@description('Name of the service subnet.')
param serviceSubnetName string

@description('Address range of the service subnet, in CIDR notation.')
param serviceSubnetAddressPrefix string

@description('Name of the management subnet.')
param managementSubnetName string

@description('Address range of the management subnet, in CIDR notation.')
param managementSubnetAddressPrefix string

@description('Resource ID of the network security group to associate with the service subnet.')
param networkSecurityGroupId string

@description('SKU of the NAT gateway, its public IP address and its public IP prefix.')
param publicIpSku string

@description('Create the subnet the NSS certificate deployment script runs in.')
param createCertificateSubnet bool = false

@description('Name of the subnet for the NSS certificate deployment script.')
param certificateSubnetName string = ''

@description('Address range of the subnet for the NSS certificate deployment script, in CIDR notation.')
param certificateSubnetAddressPrefix string = ''

resource servicePublicIp 'Microsoft.Network/publicIPAddresses@2025-05-01' = {
  name: '${vmName}-SRVC-IP'
  location: location
  sku: {
    name: publicIpSku
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource publicIpPrefix 'Microsoft.Network/publicIPPrefixes@2025-05-01' = {
  name: '${vmName}-PIP'
  location: location
  sku: {
    name: publicIpSku
  }
  properties: {
    prefixLength: 31
    publicIPAddressVersion: 'IPv4'
  }
}

resource natGateway 'Microsoft.Network/natGateways@2025-05-01' = {
  name: '${vmName}-NATGW'
  location: location
  sku: {
    name: publicIpSku
  }
  properties: {
    idleTimeoutInMinutes: 4
    publicIpAddresses: [
      {
        id: servicePublicIp.id
      }
    ]
    publicIpPrefixes: [
      {
        id: publicIpPrefix.id
      }
    ]
  }
}

// The deployment script runs as a container group, which needs a delegated subnet, the storage
// service endpoint to reach its storage account, and the NAT gateway to reach the Zscaler API.
var certificateSubnets = createCertificateSubnet
  ? [
      {
        name: certificateSubnetName
        properties: {
          addressPrefix: certificateSubnetAddressPrefix
          natGateway: {
            id: natGateway.id
          }
          serviceEndpoints: [
            {
              service: 'Microsoft.Storage'
            }
          ]
          delegations: [
            {
              name: 'Microsoft.ContainerInstance.containerGroups'
              properties: {
                serviceName: 'Microsoft.ContainerInstance/containerGroups'
              }
            }
          ]
        }
      }
    ]
  : []

// Subnets are declared inline so the virtual network and its subnets are created in a single
// operation.
resource virtualNetwork 'Microsoft.Network/virtualNetworks@2025-05-01' = {
  name: virtualNetworkName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        addressPrefix
      ]
    }
    subnets: concat(
      [
        {
          name: serviceSubnetName
          properties: {
            addressPrefix: serviceSubnetAddressPrefix
            networkSecurityGroup: {
              id: networkSecurityGroupId
            }
            natGateway: {
              id: natGateway.id
            }
          }
        }
        {
          name: managementSubnetName
          properties: {
            addressPrefix: managementSubnetAddressPrefix
            privateEndpointNetworkPolicies: 'Enabled'
            privateLinkServiceNetworkPolicies: 'Enabled'
          }
        }
      ],
      certificateSubnets
    )
  }
}

@description('Resource ID of the service subnet.')
output serviceSubnetId string = '${virtualNetwork.id}/subnets/${serviceSubnetName}'

@description('Resource ID of the management subnet.')
output managementSubnetId string = '${virtualNetwork.id}/subnets/${managementSubnetName}'

@description('Resource ID of the subnet for the NSS certificate deployment script. Empty when the subnet is not created.')
output certificateSubnetId string = createCertificateSubnet ? '${virtualNetwork.id}/subnets/${certificateSubnetName}' : ''

@description('Public IP address of the NAT gateway used by the service subnet.')
output servicePublicIpAddress string = servicePublicIp.properties.ipAddress

@description('Name of the NAT gateway.')
output natGatewayName string = natGateway.name
