using './main.sub.bicep'

// Subscription-level deployment that also takes care of the resource group, with a new virtual
// network and a new storage account. Parameters that are not listed keep the defaults declared
// in main.sub.bicep. Deploy with:
//   az deployment sub create --location <location> --parameters main.sub.bicepparam

// Resource group. To deploy into an existing resource group, set its name in
// existingResourceGroupName; nothing is created then and newResourceGroupName is ignored.
// To create the resource group, leave existingResourceGroupName empty and set the name of the
// new resource group in newResourceGroupName. It is created in the location of the deployment.
param existingResourceGroupName = ''
param newResourceGroupName = 'Zscaler-NSS-RG'

param vmName = 'Zscaler-NSS-VM'
param virtualMachineSize = 'Standard_D2_v4'

// Zscaler storage region to copy the NSS VHD from. Use the one closest to the deployment region.
param vhdRegion = 'znssprod'

// The SAS token is a secret and is read from the environment so it is never committed:
//   export NSS_SAS_TOKEN='sp=r&st=...&se=...&spr=https&sv=...&sig=...&sr=b'
param sasToken = readEnvironmentVariable('NSS_SAS_TOKEN')

// Replace with the IP address or CIDR range that administers the NSS VM over SSH.
param sshSourceAddressPrefix = '203.0.113.0/24'

param vNetNewOrExisting = 'new'
param virtualNetworkName = 'Zscaler-NSS-VNET'
param vNetAddressRange = '192.168.100.0/24'
param subnetName = 'Zscaler-NSS-Subnet'
param subnetAddressRange = '192.168.100.0/25'
param subnetName2 = 'Zscaler-NSS-MGMT-Subnet'
param subnetAddressRange2 = '192.168.100.128/28'

param stgNewOrExisting = 'new'

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

param certificateSubnetName = 'Zscaler-NSS-Certificate-Subnet'
param certificateSubnetAddressRange = '192.168.100.144/28'

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
