metadata description = 'Service network interface of the NSS virtual machine, with a private IP address that Azure picks or with a given static one.'

@description('Azure region for the network interface.')
param location string

@description('Name of the virtual machine. Used as the prefix of the network interface name.')
param vmName string

@description('Resource ID of the subnet for the service network interface.')
param subnetId string

@description('Resource ID of the network security group to associate with the network interface.')
param networkSecurityGroupId string

@description('Private IP address to assign as a static address. Leave empty to let Azure pick an address.')
param privateIpAddress string = ''

resource serviceNic 'Microsoft.Network/networkInterfaces@2025-05-01' = {
  name: '${vmName}-SRVC-NIC'
  location: location
  properties: {
    networkSecurityGroup: {
      id: networkSecurityGroupId
    }
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          primary: true
          privateIPAllocationMethod: empty(privateIpAddress) ? 'Dynamic' : 'Static'
          privateIPAddress: empty(privateIpAddress) ? null : privateIpAddress
          subnet: {
            id: subnetId
          }
        }
      }
    ]
  }
}

@description('Resource ID of the service network interface.')
output id string = serviceNic.id

@description('Private IP address of the service network interface.')
output privateIpAddress string = serviceNic.properties.ipConfigurations[0].properties.privateIPAddress
