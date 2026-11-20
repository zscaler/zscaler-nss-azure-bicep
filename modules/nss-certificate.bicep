metadata description = 'Configures the service interface of the NSS virtual machine, creates or reuses the NSS server object in ZIA, downloads its SSL certificate, installs it on the virtual machine, downloads the NSS binaries, starts NSS and enables its automatic start, over SSH from inside the virtual network.'

@description('Azure region for the deployment script.')
param location string

@description('Name of the NSS virtual machine. Used to name the deployment script.')
param vmName string

@description('Name of the user-assigned managed identity, in this resource group, the deployment script runs as.')
param identityName string

@description('Name of the storage account, in this resource group, that backs the deployment script. It must accept traffic from the script subnet.')
param storageAccountName string

@description('Resource ID of the subnet the deployment script runs in. It must be delegated to Microsoft.ContainerInstance/containerGroups, reach the NSS management interface on SSH, and reach the Zscaler API and PyPI on the internet.')
param subnetId string

@description('Private IP address of the NSS management interface.')
param vmPrivateIpAddress string

@description('Password of the zsroot account on the NSS virtual machine.')
@secure()
param vmPassword string

@description('IP address of the NSS service interface with the prefix length of its subnet, for example 192.168.100.4/25. The script answers nss configure with it.')
param serviceInterfaceAddress string

@description('Default gateway of the NSS service interface, which is the first address of its subnet. The script answers nss configure with it.')
param serviceInterfaceGateway string

@description('Name of the NSS server object in ZIA. It is created when no NSS server with this name exists.')
param nssServerName string

@description('Type of the NSS server object, used when it is created.')
param nssServerType string

@description('Install the certificate even when ZIA reports an existing NSS server object as healthy.')
param allowHealthyNssServer bool

@description('Change this value to run the deployment script again when nothing else changed.')
param forceUpdateTag string

@description('OneAPI client ID. Leave empty to use the legacy ZIA API credentials.')
@secure()
param zscalerClientId string

@description('OneAPI client secret. Leave empty when a private key is used.')
@secure()
param zscalerClientSecret string

@description('OneAPI private key in PEM format. Leave empty when a client secret is used.')
@secure()
param zscalerPrivateKey string

@description('Zidentity vanity domain for OneAPI.')
param zscalerVanityDomain string

@description('Zscaler cloud for OneAPI. Leave empty for the production cloud.')
param zscalerCloud string

@description('Admin user name for the legacy ZIA API.')
param ziaUsername string

@description('Admin password for the legacy ZIA API.')
@secure()
param ziaPassword string

@description('API key for the legacy ZIA API.')
@secure()
param ziaApiKey string

@description('ZIA cloud name for the legacy ZIA API, for example zscalertwo.')
param ziaCloud string

// The Python source is placed inside the shell script, so the deployment needs no script URL.
var scriptContent = replace(
  loadTextContent('../scripts/install-nss-certificate.sh'),
  '__INSTALL_NSS_CERTIFICATE_PY__',
  loadTextContent('../scripts/install-nss-certificate.py')
)

// Settings that are left empty are not passed on at all.
var optionalSettings = [
  {
    name: 'ZSCALER_VANITY_DOMAIN'
    value: zscalerVanityDomain
  }
  {
    name: 'ZSCALER_CLOUD'
    value: zscalerCloud
  }
  {
    name: 'ZIA_USERNAME'
    value: ziaUsername
  }
  {
    name: 'ZIA_CLOUD'
    value: ziaCloud
  }
]

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: identityName
}

resource installCertificate 'Microsoft.Resources/deploymentScripts@2023-08-01' = {
  name: '${vmName}-nss-certificate'
  location: location
  kind: 'AzureCLI'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    azCliVersion: '2.89.1'
    storageAccountSettings: {
      storageAccountName: storageAccountName
    }
    containerSettings: {
      subnetIds: [
        {
          id: subnetId
        }
      ]
    }
    scriptContent: scriptContent
    environmentVariables: concat(
      [
        {
          name: 'NSS_VM_HOST'
          value: vmPrivateIpAddress
        }
        {
          name: 'NSS_SERVER_NAME'
          value: nssServerName
        }
        {
          name: 'NSS_SERVER_TYPE'
          value: nssServerType
        }
        {
          name: 'NSS_ALLOW_HEALTHY_SERVER'
          value: string(allowHealthyNssServer)
        }
        {
          name: 'NSS_SERVICE_INTERFACE_ADDRESS'
          value: serviceInterfaceAddress
        }
        {
          name: 'NSS_SERVICE_INTERFACE_GATEWAY'
          value: serviceInterfaceGateway
        }
        {
          name: 'NSS_START'
          value: 'true'
        }
        {
          name: 'NSS_ENABLE_AUTOSTART'
          value: 'true'
        }
        {
          // The script log is kept with the deployment script resource, so the NSS configuration is not written to it.
          name: 'NSS_PRINT_DUMP_CONFIG'
          value: 'false'
        }
      ],
      filter(optionalSettings, setting => !empty(setting.value)),
      filter(
        [
          {
            name: 'NSS_VM_PASSWORD'
            secureValue: vmPassword
          }
          {
            name: 'ZSCALER_CLIENT_ID'
            secureValue: zscalerClientId
          }
          {
            name: 'ZSCALER_CLIENT_SECRET'
            secureValue: zscalerClientSecret
          }
          {
            name: 'ZSCALER_PRIVATE_KEY'
            secureValue: zscalerPrivateKey
          }
          {
            name: 'ZIA_PASSWORD'
            secureValue: ziaPassword
          }
          {
            name: 'ZIA_API_KEY'
            secureValue: ziaApiKey
          }
        ],
        setting => !empty(setting.secureValue)
      )
    )
    forceUpdateTag: empty(forceUpdateTag) ? null : forceUpdateTag
    timeout: 'PT30M'
    cleanupPreference: 'OnSuccess'
    retentionInterval: 'P1D'
  }
}

@description('ID of the NSS server object in ZIA.')
output nssServerId string = installCertificate.properties.outputs.nssServerId

@description('True when this run created the NSS server object, false when it already existed.')
output nssServerCreated bool = installCertificate.properties.outputs.nssServerCreated

@description('True when this run installed the certificate, false when the same certificate was already installed.')
output certificateInstalled bool = installCertificate.properties.outputs.certificateInstalled

@description('True when this run answered nss configure, false when the service interface was already configured with these values.')
output serviceInterfaceConfigured bool = installCertificate.properties.outputs.serviceInterfaceConfigured

@description('True when nss status showed NSS running at the end of the run. False when NSS did not start; the script log says why.')
output nssRunning bool = installCertificate.properties.outputs.nssRunning

@description('Name of the deployment script that installed the certificate.')
output scriptName string = installCertificate.name
