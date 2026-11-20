metadata description = 'NSS virtual machine with its management public IP, two network interfaces and a managed OS disk imported from the staged VHD.'

@description('Azure region for the virtual machine and its resources.')
param location string

@description('Name of the virtual machine. Also used as the prefix for the disk, network interface and public IP names.')
param vmName string

@description('Size of the virtual machine.')
param virtualMachineSize string

@description('Resource ID of the subnet for the management network interface.')
param managementSubnetId string

@description('Resource ID of the subnet for the service network interface.')
param serviceSubnetId string

@description('Resource ID of the network security group to associate with both network interfaces.')
param networkSecurityGroupId string

@description('SKU of the management public IP address.')
param publicIpSku string

@description('Blob URI of the NSS OS VHD to import as the managed OS disk.')
param vhdUri string

@description('Resource ID of the storage account that holds the NSS OS VHD.')
param vhdStorageAccountId string

var osDiskSizeGB = 600

resource managementPublicIp 'Microsoft.Network/publicIPAddresses@2025-05-01' = {
  name: '${vmName}-MGMT-IP'
  location: location
  sku: {
    name: publicIpSku
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource managementNic 'Microsoft.Network/networkInterfaces@2025-05-01' = {
  name: '${vmName}-MGMT-NIC'
  location: location
  properties: {
    enableAcceleratedNetworking: false
    enableIPForwarding: false
    networkSecurityGroup: {
      id: networkSecurityGroupId
    }
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          primary: true
          privateIPAllocationMethod: 'Dynamic'
          privateIPAddressVersion: 'IPv4'
          publicIPAddress: {
            id: managementPublicIp.id
          }
          subnet: {
            id: managementSubnetId
          }
        }
      }
    ]
  }
}

// The address of the service interface is written into the NSS configuration (nss configure), so
// it must not change. A static address has to be named, and the template does not know a free
// one. The network interface is therefore deployed twice: first Azure picks the address, then
// the same address is made static. On a later deployment the first step turns the address
// dynamic for a moment; Azure keeps the address of an interface that is in use.
module serviceNicWithAzureAddress 'service-network-interface.bicep' = {
  params: {
    location: location
    vmName: vmName
    subnetId: serviceSubnetId
    networkSecurityGroupId: networkSecurityGroupId
  }
}

module serviceNic 'service-network-interface.bicep' = {
  params: {
    location: location
    vmName: vmName
    subnetId: serviceSubnetId
    networkSecurityGroupId: networkSecurityGroupId
    privateIpAddress: serviceNicWithAzureAddress.outputs.privateIpAddress
  }
}

resource osDisk 'Microsoft.Compute/disks@2025-01-02' = {
  name: '${vmName}-osdisk'
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    osType: 'Linux'
    hyperVGeneration: 'V1'
    diskSizeGB: osDiskSizeGB
    creationData: {
      createOption: 'Import'
      sourceUri: vhdUri
      storageAccountId: vhdStorageAccountId
    }
  }
}

// The NSS image is a specialized disk, so it is attached as-is and no osProfile is set.
resource virtualMachine 'Microsoft.Compute/virtualMachines@2025-04-01' = {
  name: vmName
  location: location
  properties: {
    hardwareProfile: {
      vmSize: virtualMachineSize
    }
    storageProfile: {
      osDisk: {
        osType: 'Linux'
        createOption: 'Attach'
        caching: 'ReadWrite'
        managedDisk: {
          id: osDisk.id
        }
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: managementNic.id
          properties: {
            primary: true
          }
        }
        {
          id: serviceNic.outputs.id
          properties: {
            primary: false
          }
        }
      ]
    }
  }
}

@description('Resource ID of the virtual machine.')
output vmResourceId string = virtualMachine.id

@description('Name of the virtual machine.')
output vmName string = virtualMachine.name

@description('Resource ID of the managed OS disk.')
output osDiskResourceId string = osDisk.id

@description('Name of the management public IP resource.')
output managementPublicIpName string = managementPublicIp.name

@description('Public IP address of the NSS management interface.')
output managementPublicIpAddress string = managementPublicIp.properties.ipAddress

@description('Private IP address of the NSS management interface.')
output managementPrivateIpAddress string = managementNic.properties.ipConfigurations[0].properties.privateIPAddress

@description('Static private IP address of the NSS service interface.')
output servicePrivateIpAddress string = serviceNic.outputs.privateIpAddress
