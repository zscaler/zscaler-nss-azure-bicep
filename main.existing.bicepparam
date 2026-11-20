using './main.bicep'

// Existing virtual network and existing storage account. Replace every value below with the
// names used in your environment. Parameters that are not listed keep the defaults declared
// in main.bicep.

param vmName = 'Zscaler-NSS-VM'
param virtualMachineSize = 'Standard_D2_v4'

// Zscaler storage region to copy the NSS VHD from. Use the one closest to the deployment region.
param vhdRegion = 'znssprod'

// The SAS token is a secret and is read from the environment so it is never committed:
//   export NSS_SAS_TOKEN='sp=r&st=...&se=...&spr=https&sv=...&sig=...&sr=b'
param sasToken = readEnvironmentVariable('NSS_SAS_TOKEN')

// Replace with the IP address or CIDR range that administers the NSS VM over SSH.
param sshSourceAddressPrefix = '203.0.113.0/24'

// The virtual network and both subnets must already exist. The service subnet needs its own
// outbound path (for example a NAT gateway), because none is created in this mode.
param vNetNewOrExisting = 'existing'
param virtualNetworkResourceGroup = 'my-network-rg'
param virtualNetworkName = 'my-existing-vnet'
param subnetName = 'my-nss-service-subnet'
param subnetName2 = 'my-nss-management-subnet'

// The storage account must be in the deployment resource group and allow shared key access.
param stgNewOrExisting = 'existing'
param nameStorageAccount = 'myexistingstorage'

// Optional: register the VM in ZIA and install its SSL certificate at the end of the deployment.
// Set installNssCertificate to true and export the password of the zsroot account plus one set of
// Zscaler API credentials. They are secrets and are read from the environment so they are never
// committed. Variables that are not set are passed as empty values.
//   export NSS_VM_PASSWORD='...'
//   OneAPI:      ZSCALER_CLIENT_ID, ZSCALER_VANITY_DOMAIN, ZSCALER_CLIENT_SECRET or ZSCALER_PRIVATE_KEY,
//                and ZSCALER_CLOUD for a cloud other than production
//   Legacy ZIA:  ZIA_USERNAME, ZIA_PASSWORD, ZIA_API_KEY, ZIA_CLOUD
// The deployment does not activate a new NSS server object. Activate the change in ZIA yourself.
param installNssCertificate = false
param nssServerName = 'Zscaler-NSS-VM'
param nssServerType = 'NSS_FOR_WEB'

// With an existing virtual network the certificate subnet must already exist: delegated to
// Microsoft.ContainerInstance/containerGroups, with the Microsoft.Storage service endpoint and
// with outbound internet access.
param certificateSubnetName = 'my-nss-certificate-subnet'

param nssVmPassword = readEnvironmentVariable('NSS_VM_PASSWORD', '')
param zscalerClientId = readEnvironmentVariable('ZSCALER_CLIENT_ID', '')
param zscalerClientSecret = readEnvironmentVariable('ZSCALER_CLIENT_SECRET', '')
param zscalerPrivateKey = readEnvironmentVariable('ZSCALER_PRIVATE_KEY', '')
param zscalerVanityDomain = readEnvironmentVariable('ZSCALER_VANITY_DOMAIN', '')
param zscalerCloud = readEnvironmentVariable('ZSCALER_CLOUD', '')
param ziaUsername = readEnvironmentVariable('ZIA_USERNAME', '')
param ziaPassword = readEnvironmentVariable('ZIA_PASSWORD', '')
param ziaApiKey = readEnvironmentVariable('ZIA_API_KEY', '')
param ziaCloud = readEnvironmentVariable('ZIA_CLOUD', '')
