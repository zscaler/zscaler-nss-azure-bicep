metadata description = 'Storage account used to stage the NSS OS VHD before it is imported as a managed disk.'

@description('Azure region for the storage account.')
param location string

@description('Globally unique name of the storage account.')
@minLength(3)
@maxLength(24)
param name string

resource storageAccount 'Microsoft.Storage/storageAccounts@2025-06-01' = {
  name: name
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

@description('Name of the storage account.')
output name string = storageAccount.name
