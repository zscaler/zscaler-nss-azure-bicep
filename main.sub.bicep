metadata name = 'Zscaler NSS on Azure, with its resource group'
metadata description = 'Deploys main.bicep into a resource group and, when no existing resource group is named, creates that resource group first. Deployed at subscription scope.'

targetScope = 'subscription'

@description('Name of an existing resource group to deploy into. When it is set, no resource group is created and newResourceGroupName is ignored. Leave empty to create a new resource group.')
@maxLength(90)
param existingResourceGroupName string = ''

@description('Name of the resource group to create. Required when existingResourceGroupName is empty.')
@maxLength(90)
param newResourceGroupName string = ''

@description('Location for all resources and for a new resource group. Defaults to the location of the deployment.')
param location string = deployment().location

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

@description('Resource group of the existing virtual network. Leave empty when it is the deployment resource group. Only used when vNetNewOrExisting is set to existing.')
param virtualNetworkResourceGroup string = ''

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

var createResourceGroup = empty(existingResourceGroupName)
var resourceGroupName = createResourceGroup
  ? (empty(newResourceGroupName)
      ? fail('Set existingResourceGroupName to deploy into an existing resource group, or newResourceGroupName to create one.')
      : newResourceGroupName)
  : existingResourceGroupName

resource newResourceGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = if (createResourceGroup) {
  name: resourceGroupName
  location: location
}

// The module is scoped by the resource group name, so that this one declaration serves a new and
// an existing resource group. A name carries no dependency on the resource group that is created
// above, hence the explicit dependsOn.
module nss 'main.bicep' = {
  scope: resourceGroup(resourceGroupName)
  params: {
    location: location
    vhdRegion: vhdRegion
    sasToken: sasToken
    vNetNewOrExisting: vNetNewOrExisting
    stgNewOrExisting: stgNewOrExisting
    nameStorageAccount: nameStorageAccount
    virtualNetworkName: virtualNetworkName
    virtualNetworkResourceGroup: empty(virtualNetworkResourceGroup)
      ? resourceGroupName
      : virtualNetworkResourceGroup
    vNetAddressRange: vNetAddressRange
    subnetName: subnetName
    subnetName2: subnetName2
    subnetAddressRange: subnetAddressRange
    subnetAddressRange2: subnetAddressRange2
    vmName: vmName
    virtualMachineSize: virtualMachineSize
    publicIpSku: publicIpSku
    sshSourceAddressPrefix: sshSourceAddressPrefix
    installNssCertificate: installNssCertificate
    nssServerName: nssServerName
    nssServerType: nssServerType
    allowHealthyNssServer: allowHealthyNssServer
    nssVmPassword: nssVmPassword
    zscalerClientId: zscalerClientId
    zscalerClientSecret: zscalerClientSecret
    zscalerPrivateKey: zscalerPrivateKey
    zscalerVanityDomain: zscalerVanityDomain
    zscalerCloud: zscalerCloud
    ziaUsername: ziaUsername
    ziaPassword: ziaPassword
    ziaApiKey: ziaApiKey
    ziaCloud: ziaCloud
    certificateSubnetName: certificateSubnetName
    certificateSubnetAddressRange: certificateSubnetAddressRange
    certificateForceUpdateTag: certificateForceUpdateTag
  }
  dependsOn: [
    newResourceGroup
  ]
}

@description('Name of the resource group the NSS VM was deployed into.')
output resourceGroupName string = resourceGroupName

@description('True when this deployment was asked to create the resource group.')
output resourceGroupCreated bool = createResourceGroup

@description('Resource ID of the deployed VM.')
output vmResourceId string = nss.outputs.vmResourceId

@description('Name of the deployed VM.')
output vmName string = nss.outputs.vmName

@description('Resource ID of the managed OS disk imported from the NSS VHD.')
output osDiskResourceId string = nss.outputs.osDiskResourceId

@description('Name of the management public IP resource.')
output managementPublicIpName string = nss.outputs.managementPublicIpName

@description('Public IP address of the NSS management interface.')
output managementPublicIpAddress string = nss.outputs.managementPublicIpAddress

@description('Private IP address of the NSS management interface.')
output managementPrivateIpAddress string = nss.outputs.managementPrivateIpAddress

@description('Static private IP address of the NSS service interface with the prefix length of its subnet. This is the answer to the smnet_dev question of nss configure.')
output serviceInterfaceAddress string = nss.outputs.serviceInterfaceAddress

@description('Default gateway of the NSS service interface. This is the answer to the smnet_dflt_gw question of nss configure.')
output serviceInterfaceGateway string = nss.outputs.serviceInterfaceGateway

@description('Public IP address of the NAT gateway. Empty when an existing virtual network is used, because no NAT gateway is created.')
output servicePublicIp string = nss.outputs.servicePublicIp

@description('Name of the storage account used to stage the NSS VHD.')
output storageAccountName string = nss.outputs.storageAccountName

@description('Name of the virtual network.')
output virtualNetworkName string = nss.outputs.virtualNetworkName

@description('ID of the NSS server object in ZIA. Empty when installNssCertificate is false.')
output nssServerId string = nss.outputs.nssServerId

@description('True when the certificate step started NSS, or found it running. False when NSS did not start, which the log of the certificate deployment script explains, and when installNssCertificate is false.')
output nssRunning bool = nss.outputs.nssRunning
