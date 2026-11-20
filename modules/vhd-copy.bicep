metadata description = 'Copies the Zscaler NSS OS VHD from Zscaler storage into a container of an existing storage account.'

@description('Azure region for the deployment script.')
param location string

@description('Name of the NSS virtual machine. Used to name the copied VHD and the deployment script.')
param vmName string

@description('Zscaler storage account that hosts the NSS VHD for the selected region.')
param vhdRegion string

@description('SAS token issued by Zscaler Support for the selected VHD region, with or without the leading question mark.')
@secure()
param sasToken string

@description('Name of the storage account, in this resource group, that receives the VHD. It must already exist and allow shared key access.')
param storageAccountName string

var nssImageName = 'znss_5_2_osdisk.vhd'
var containerName = 'zscalernssrprod'
var destinationBlobName = 'znss_${vmName}_osdisk.vhd'

// The image lives in Zscaler-owned storage accounts whose cloud follows the selected VHD region,
// not the cloud this deployment runs in, so environment().suffixes.storage cannot be used here.
var isGovRegion = contains(vhdRegion, 'gov')
#disable-next-line no-hardcoded-env-urls
var sourceBlobEndpoint = isGovRegion ? 'https://${vhdRegion}.blob.core.usgovcloudapi.net' : 'https://${vhdRegion}.blob.core.windows.net'
var sourceContainerName = isGovRegion ? 'nss-disk' : 'nss'
var sourceVhdUrl = '${sourceBlobEndpoint}/${sourceContainerName}/${nssImageName}'
var sasQueryString = startsWith(sasToken, '?') ? sasToken : '?${sasToken}'

resource storageAccount 'Microsoft.Storage/storageAccounts@2025-06-01' existing = {
  name: storageAccountName

  resource blobService 'blobServices' existing = {
    name: 'default'

    resource vhdContainer 'containers' = {
      name: containerName
      properties: {
        publicAccess: 'None'
      }
    }
  }
}

resource copyVhd 'Microsoft.Resources/deploymentScripts@2023-08-01' = {
  name: '${vmName}-vhd-copy'
  location: location
  kind: 'AzurePowerShell'
  properties: {
    azPowerShellVersion: '15.4'
    scriptContent: loadTextContent('../scripts/copy-vhd.ps1')
    environmentVariables: [
      {
        name: 'SOURCE_VHD_URL'
        value: sourceVhdUrl
      }
      {
        name: 'SOURCE_SAS_TOKEN'
        secureValue: sasQueryString
      }
      {
        name: 'DEST_STORAGE_ACCOUNT'
        value: storageAccount.name
      }
      {
        name: 'DEST_STORAGE_KEY'
        secureValue: storageAccount.listKeys().keys[0].value
      }
      {
        name: 'DEST_STORAGE_ENDPOINT_SUFFIX'
        value: environment().suffixes.storage
      }
      {
        name: 'DEST_CONTAINER'
        value: storageAccount::blobService::vhdContainer.name
      }
      {
        name: 'DEST_BLOB'
        value: destinationBlobName
      }
    ]
    timeout: 'PT3H'
    cleanupPreference: 'OnSuccess'
    retentionInterval: 'P1D'
  }
}

@description('Blob URI of the copied NSS OS VHD.')
output vhdUri string = '${storageAccount.properties.primaryEndpoints.blob}${containerName}/${destinationBlobName}'

@description('Resource ID of the storage account that holds the copied VHD.')
output storageAccountId string = storageAccount.id

@description('Name of the deployment script that performed the copy.')
output copyScriptName string = copyVhd.name
