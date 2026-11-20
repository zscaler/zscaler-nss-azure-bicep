metadata name = 'Zscaler NSS on Azure'
metadata description = 'Deploys a single Zscaler Nanolog Streaming Service (NSS) virtual machine with its networking and a managed OS disk imported from the Zscaler NSS VHD. Optionally registers the VM in ZIA and installs its SSL certificate.'

targetScope = 'resourceGroup'

@description('Location for all resources.')
param location string = resourceGroup().location

@description('Zscaler NSS VHD source region. Options: znssprod (US Commercial), znssprodeu (Europe), znssprodau (Australia), zsgovnsstexas (US Gov Texas), zsgovnssarizona (US Gov Arizona), zsgovnssiowa (US Gov Iowa), zsgovnssvirginia (US Gov Virginia).')
@allowed([
  'znssprod'
  'znssprodeu'
  'znssprodau'
  'zsgovnsstexas'
  'zsgovnssarizona'
  'zsgovnssiowa'
  'zsgovnssvirginia'
])
param vhdRegion string

@description('SAS token for the Zscaler NSS VHD in the selected VHD region. Contact Zscaler Support to receive the token. Format: sp=r&st=<start>&se=<expiry>&spr=https&sv=<version>&sig=<signature>&sr=b')
@secure()
@minLength(1)
param sasToken string

@description('Specify whether to create a new or use an existing virtual network for the VM.')
@allowed([
  'new'
  'existing'
])
param vNetNewOrExisting string = 'new'

@description('Specify whether to create a new or use an existing storage account to stage the NSS VHD.')
@allowed([
  'new'
  'existing'
])
param stgNewOrExisting string = 'new'

@description('Name of the existing storage account to use. It must be in the deployment resource group. Only required when stgNewOrExisting is set to existing.')
@maxLength(24)
param nameStorageAccount string = ''

@description('Name of the virtual network. If using existing, specify the existing virtual network name. Must be 2-64 characters, begin with a letter or number, and end with a letter, number or underscore.')
@minLength(2)
@maxLength(64)
param virtualNetworkName string = 'Zscaler-NSS-VNET'

@description('Resource group of the existing virtual network. Only used when vNetNewOrExisting is set to existing.')
param virtualNetworkResourceGroup string = resourceGroup().name

@description('Address range of the new virtual network. Not used when vNetNewOrExisting is set to existing.')
param vNetAddressRange string = '192.168.100.0/24'

@description('Name of the service subnet. Must be 1-80 characters.')
@minLength(1)
@maxLength(80)
param subnetName string = 'Zscaler-NSS-Subnet'

@description('Name of the management subnet. Must be 1-80 characters.')
@minLength(1)
@maxLength(80)
param subnetName2 string = 'Zscaler-NSS-MGMT-Subnet'

@description('Address range of the new service subnet. Not used when vNetNewOrExisting is set to existing.')
param subnetAddressRange string = '192.168.100.0/25'

@description('Address range of the new management subnet. Not used when vNetNewOrExisting is set to existing.')
param subnetAddressRange2 string = '192.168.100.128/28'

@description('Name of the virtual machine. Must be 1-64 characters.')
@minLength(1)
@maxLength(64)
param vmName string = 'Zscaler-NSS-VM'

@description('Size of the VM.')
@allowed([
  'Standard_D2_v4'
  'Standard_D4_v4'
  'Standard_D8_v4'
  'Standard_D16_v4'
])
param virtualMachineSize string = 'Standard_D2_v4'

@description('SKU for the public IP addresses, the public IP prefix and the NAT gateway. Azure retired the Basic SKU, so Standard is the only supported value.')
@allowed([
  'Standard'
])
param publicIpSku string = 'Standard'

@description('Source IP address or CIDR range allowed for SSH access. For security, restrict this to your admin IP range (for example 203.0.113.0/24). The default * allows access from any IP and is not recommended for production.')
param sshSourceAddressPrefix string = '*'

@description('Register the VM in ZIA and install its SSL certificate after the VM is deployed: find or create the NSS server object, download its certificate, install it on the VM and start NSS, over SSH from inside the virtual network. Requires nssVmPassword, Zscaler API credentials (OneAPI or legacy ZIA) and permission to create role assignments in the resource group. When false, none of the parameters below are used.')
param installNssCertificate bool = false

@description('Name of the NSS server object in ZIA. An NSS server with this name is reused; otherwise it is created. Its certificate must be installed on only one active NSS VM. Only used when installNssCertificate is true.')
@minLength(1)
param nssServerName string = vmName

@description('Type of the NSS server object, used when it is created. The deployment does not activate the new object; activate the change in ZIA. Only used when installNssCertificate is true.')
@allowed([
  'SOFTWARE_AA_FLAG'
  'NSS_FOR_WEB'
  'NSS_FOR_FIREWALL'
  'VZEN'
  'VZEN_SME'
  'VZEN_SMLB'
  'PINNED_NSS'
  'MD5_CAPABLE'
  'ADP'
  'ZIRSVR'
  'NSS_FOR_ZPA'
])
param nssServerType string = 'NSS_FOR_WEB'

@description('Install the certificate even when ZIA reports an existing NSS server object as healthy, which means an NSS VM is already using that certificate. Only used when installNssCertificate is true.')
param allowHealthyNssServer bool = false

@description('Password of the zsroot account on the NSS VM, used to copy and install the certificate over SSH. Required when installNssCertificate is true.')
@secure()
param nssVmPassword string = ''

@description('OneAPI client ID. Set the OneAPI parameters or the legacy ZIA API parameters, not both. Only used when installNssCertificate is true.')
@secure()
param zscalerClientId string = ''

@description('OneAPI client secret. Leave empty when zscalerPrivateKey is used. Only used when installNssCertificate is true.')
@secure()
param zscalerClientSecret string = ''

@description('OneAPI private key in PEM format. Leave empty when zscalerClientSecret is used. Only used when installNssCertificate is true.')
@secure()
param zscalerPrivateKey string = ''

@description('Zidentity vanity domain for OneAPI, for example acme for acme.zslogin.net. Only used when installNssCertificate is true.')
param zscalerVanityDomain string = ''

@description('Zscaler cloud for OneAPI. Leave empty for the production cloud. Only used when installNssCertificate is true.')
param zscalerCloud string = ''

@description('Admin user name for the legacy ZIA API, for tenants that are not on Zidentity. Only used when installNssCertificate is true.')
param ziaUsername string = ''

@description('Admin password for the legacy ZIA API. Only used when installNssCertificate is true.')
@secure()
param ziaPassword string = ''

@description('API key for the legacy ZIA API. Only used when installNssCertificate is true.')
@secure()
param ziaApiKey string = ''

@description('ZIA cloud name for the legacy ZIA API, for example zscalertwo. Only used when installNssCertificate is true.')
param ziaCloud string = ''

@description('Name of the subnet the certificate deployment script runs in. With a new virtual network it is created. With an existing virtual network it must already exist, be delegated to Microsoft.ContainerInstance/containerGroups, have the Microsoft.Storage service endpoint and have outbound internet access. Only used when installNssCertificate is true.')
@minLength(1)
@maxLength(80)
param certificateSubnetName string = 'Zscaler-NSS-Certificate-Subnet'

@description('Address range of the new certificate subnet. Not used when vNetNewOrExisting is set to existing or installNssCertificate is false.')
param certificateSubnetAddressRange string = '192.168.100.144/28'

@description('Change this value to run the certificate installation again when no other parameter changed. Only used when installNssCertificate is true.')
param certificateForceUpdateTag string = ''

var createVirtualNetwork = vNetNewOrExisting == 'new'
var createStorageAccount = stgNewOrExisting == 'new'
var vhdStorageAccountName = createStorageAccount
  ? 'vhdstorage${uniqueString(subscription().id, resourceGroup().id)}'
  : nameStorageAccount
var certificateStorageAccountName = 'nsscert${uniqueString(subscription().id, resourceGroup().id, vmName)}'
var certificateIdentityName = '${vmName}-certificate-identity'

var existingServiceSubnetId = resourceId(
  virtualNetworkResourceGroup,
  'Microsoft.Network/virtualNetworks/subnets',
  virtualNetworkName,
  subnetName
)
var existingManagementSubnetId = resourceId(
  virtualNetworkResourceGroup,
  'Microsoft.Network/virtualNetworks/subnets',
  virtualNetworkName,
  subnetName2
)
var existingCertificateSubnetId = resourceId(
  virtualNetworkResourceGroup,
  'Microsoft.Network/virtualNetworks/subnets',
  virtualNetworkName,
  certificateSubnetName
)

// A subnet of an existing virtual network has its address range in addressPrefix or, when it has
// more than one, in addressPrefixes. The NSS service interface uses IPv4.
var existingServiceSubnetProperties = createVirtualNetwork
  ? {}
  : reference(existingServiceSubnetId, '2025-05-01', 'full').properties
var serviceSubnetAddressRange = createVirtualNetwork
  ? subnetAddressRange
  : existingServiceSubnetProperties.?addressPrefix ?? filter(
      existingServiceSubnetProperties.?addressPrefixes ?? [],
      addressRange => !contains(addressRange, ':')
    )[0]
// What nss configure asks for: the address of the service interface with the prefix length of its
// subnet, and the default gateway, which in Azure is the first address of the subnet.
var serviceInterfaceAddress = '${virtualMachine.outputs.servicePrivateIpAddress}/${split(serviceSubnetAddressRange, '/')[1]}'
var serviceInterfaceGateway = cidrHost(serviceSubnetAddressRange, 0)

module networkSecurityGroup 'modules/network-security-group.bicep' = {
  params: {
    location: location
    vmName: vmName
    sshSourceAddressPrefix: sshSourceAddressPrefix
  }
}

module virtualNetwork 'modules/virtual-network.bicep' = if (createVirtualNetwork) {
  params: {
    location: location
    vmName: vmName
    virtualNetworkName: virtualNetworkName
    addressPrefix: vNetAddressRange
    serviceSubnetName: subnetName
    serviceSubnetAddressPrefix: subnetAddressRange
    managementSubnetName: subnetName2
    managementSubnetAddressPrefix: subnetAddressRange2
    networkSecurityGroupId: networkSecurityGroup.outputs.id
    publicIpSku: publicIpSku
    createCertificateSubnet: installNssCertificate
    certificateSubnetName: certificateSubnetName
    certificateSubnetAddressPrefix: certificateSubnetAddressRange
  }
}

module storageAccount 'modules/storage-account.bicep' = if (createStorageAccount) {
  params: {
    location: location
    name: vhdStorageAccountName
  }
}

// The storage account name is passed as a value known at the start of the deployment, rather than
// as an output of the storageAccount module, so what-if can evaluate the resources of this module.
// That removes the implicit dependency, hence the explicit dependsOn.
module vhdCopy 'modules/vhd-copy.bicep' = {
  params: {
    location: location
    vmName: vmName
    vhdRegion: vhdRegion
    sasToken: sasToken
    storageAccountName: vhdStorageAccountName
  }
  dependsOn: [
    storageAccount
  ]
}

module virtualMachine 'modules/virtual-machine.bicep' = {
  params: {
    location: location
    vmName: vmName
    virtualMachineSize: virtualMachineSize
    managementSubnetId: createVirtualNetwork
      ? virtualNetwork!.outputs.managementSubnetId
      : existingManagementSubnetId
    serviceSubnetId: createVirtualNetwork
      ? virtualNetwork!.outputs.serviceSubnetId
      : existingServiceSubnetId
    networkSecurityGroupId: networkSecurityGroup.outputs.id
    publicIpSku: publicIpSku
    vhdUri: vhdCopy.outputs.vhdUri
    vhdStorageAccountId: vhdCopy.outputs.storageAccountId
  }
}

// Created at the start of the deployment, so the role assignment has taken effect by the time
// the certificate script runs, after the VM.
module nssCertificateEnvironment 'modules/nss-certificate-environment.bicep' = if (installNssCertificate) {
  params: {
    location: location
    identityName: certificateIdentityName
    storageAccountName: certificateStorageAccountName
    subnetId: createVirtualNetwork
      ? virtualNetwork!.outputs.certificateSubnetId
      : existingCertificateSubnetId
  }
}

// The identity and storage account names are passed as values known at the start of the
// deployment, rather than as outputs of the nssCertificateEnvironment module, so what-if can
// evaluate the resources of this module. That removes the implicit dependency, hence the
// explicit dependsOn.
module nssCertificate 'modules/nss-certificate.bicep' = if (installNssCertificate) {
  params: {
    location: location
    vmName: vmName
    identityName: certificateIdentityName
    storageAccountName: certificateStorageAccountName
    subnetId: createVirtualNetwork
      ? virtualNetwork!.outputs.certificateSubnetId
      : existingCertificateSubnetId
    vmPrivateIpAddress: virtualMachine.outputs.managementPrivateIpAddress
    vmPassword: nssVmPassword
    serviceInterfaceAddress: serviceInterfaceAddress
    serviceInterfaceGateway: serviceInterfaceGateway
    nssServerName: nssServerName
    nssServerType: nssServerType
    allowHealthyNssServer: allowHealthyNssServer
    forceUpdateTag: certificateForceUpdateTag
    zscalerClientId: zscalerClientId
    zscalerClientSecret: zscalerClientSecret
    zscalerPrivateKey: zscalerPrivateKey
    zscalerVanityDomain: zscalerVanityDomain
    zscalerCloud: zscalerCloud
    ziaUsername: ziaUsername
    ziaPassword: ziaPassword
    ziaApiKey: ziaApiKey
    ziaCloud: ziaCloud
  }
  dependsOn: [
    nssCertificateEnvironment
  ]
}

@description('Resource ID of the deployed VM.')
output vmResourceId string = virtualMachine.outputs.vmResourceId

@description('Name of the deployed VM.')
output vmName string = virtualMachine.outputs.vmName

@description('Resource ID of the managed OS disk imported from the NSS VHD.')
output osDiskResourceId string = virtualMachine.outputs.osDiskResourceId

@description('Name of the management public IP resource.')
output managementPublicIpName string = virtualMachine.outputs.managementPublicIpName

@description('Public IP address of the NSS management interface.')
output managementPublicIpAddress string = virtualMachine.outputs.managementPublicIpAddress

@description('Private IP address of the NSS management interface.')
output managementPrivateIpAddress string = virtualMachine.outputs.managementPrivateIpAddress

@description('Static private IP address of the NSS service interface with the prefix length of its subnet. This is the answer to the smnet_dev question of nss configure.')
output serviceInterfaceAddress string = serviceInterfaceAddress

@description('Default gateway of the NSS service interface. This is the answer to the smnet_dflt_gw question of nss configure.')
output serviceInterfaceGateway string = serviceInterfaceGateway

@description('Public IP address of the NAT gateway. Empty when an existing virtual network is used, because no NAT gateway is created.')
output servicePublicIp string = createVirtualNetwork ? virtualNetwork!.outputs.servicePublicIpAddress : ''

@description('Name of the storage account used to stage the NSS VHD.')
output storageAccountName string = vhdStorageAccountName

@description('Name of the virtual network.')
output virtualNetworkName string = virtualNetworkName

@description('ID of the NSS server object in ZIA. Empty when installNssCertificate is false.')
output nssServerId string = installNssCertificate ? nssCertificate!.outputs.nssServerId : ''

@description('True when the certificate step started NSS, or found it running. False when NSS did not start, which the log of the certificate deployment script explains, and when installNssCertificate is false.')
output nssRunning bool = installNssCertificate ? nssCertificate!.outputs.nssRunning : false
